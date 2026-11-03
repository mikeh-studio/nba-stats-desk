#!/usr/bin/env python3
"""Run the existing validated pipeline weekly without a permanent scheduler.

Configuration is explicit JSON, never dotenv. Historical base datasets must be
rolled over separately before this worker may write current-season facts.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

UTC = timezone.utc
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def slot(now, timezone_name, weekday=6, hour=8):
    """Most recent weekly slot, including a missed slot after sleep/login."""
    if now.tzinfo is None or weekday not in range(7) or hour not in range(24):
        raise ValueError("Invalid weekly schedule")
    local = now.astimezone(ZoneInfo(timezone_name))
    candidate = (local - timedelta(days=(local.weekday() - weekday) % 7)).replace(
        hour=hour, minute=0, second=0, microsecond=0
    )
    if candidate > local:
        candidate -= timedelta(days=7)
    return candidate.astimezone(UTC)


def due(state, current_slot, now):
    if state.get("last_success_slot") == current_slot.isoformat():
        return False
    if state.get("attempt_slot") != current_slot.isoformat():
        return True
    if state.get("attempt_count", 0) >= 3:
        return False
    return now - datetime.fromisoformat(state["last_attempt_at"]) >= timedelta(days=1)


def write_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def validate_config(config):
    required = {
        "project",
        "bucket",
        "repo",
        "airflow",
        "timezone",
        "weekday",
        "hour",
        "season",
        "state_dir",
    }
    if config.get("source", "nba") == "espn":
        required |= {"source", "first_date"}
        if not isinstance(config.get("first_date"), str):
            raise ValueError("ESPN requires an explicit first_date")
        first = datetime.fromisoformat(config["first_date"]).date()
        if (
            not datetime(int(config["season"][:4]), 7, 1).date()
            <= first
            <= datetime(int(config["season"][:4]) + 1, 6, 30).date()
        ):
            raise ValueError("ESPN first date outside season")
    if set(config) != required:
        raise ValueError("Configuration keys differ from the weekly contract")
    if not re.fullmatch(r"[a-z][a-z0-9-]{4,62}", config["project"]):
        raise ValueError("Invalid project")
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]", config["bucket"]):
        raise ValueError("Invalid bucket")
    if not re.fullmatch(r"20\d{2}-\d{2}", config["season"]):
        raise ValueError("Invalid season")
    year = int(config["season"][:4])
    if config["season"][-2:] != str(year + 1)[-2:]:
        raise ValueError("Invalid season suffix")
    for key in ("repo", "airflow", "state_dir"):
        if not Path(config[key]).is_absolute():
            raise ValueError("Runtime paths must be absolute")
    repo = Path(config["repo"])
    import yaml

    if (
        yaml.safe_load((repo / "season.yml").read_text())["current_season"]
        != config["season"]
    ):
        raise ValueError("Configured season differs from checkout; review rollover")
    slot(datetime.now(UTC), config["timezone"], config["weekday"], config["hour"])
    return config


class ScopeBlocked(RuntimeError):
    def __init__(self, reason, scope=None):
        super().__init__(reason)
        self.scope = scope


def assess_rows(rows, season):
    """Do not rebuild shared base tables while they hold another season."""
    scope = {"season": season, "groups": rows}
    if any(r["season"] != season and r["row_count"] for r in rows):
        raise ScopeBlocked("season_rollover_required", scope)
    return scope


def warehouse_scope(config):
    from google.cloud import bigquery

    client = bigquery.Client(project=config["project"])
    if config.get("source") == "espn":
        prefix = config["project"] + ".nba_gamebook_dev_espn_only"
        sql = f"""SELECT r.season, COUNTIF(p.played AND p.nba_player_id IS NOT NULL) row_count,
CAST(MAX(IF(p.played AND p.nba_player_id IS NOT NULL,r.game_date,NULL)) AS STRING) data_through,
COUNTIF(p.played AND p.nba_player_id IS NULL) unmapped_rows,
COUNTIF(p.played AND JSON_VALUE(p.stats,'$.MIN')='--') missing_minutes,
COUNT(DISTINCT r.source_game_id) accepted_games
FROM `{prefix}.espn_layer_readiness` r JOIN `{prefix}.espn_layer_player_box_scores` p USING(source_game_id)
WHERE r.season=@season AND r.result_status='accepted' AND r.box_status='accepted' GROUP BY r.season"""
        rows = [
            dict(row)
            for row in client.query(
                sql,
                job_config=bigquery.QueryJobConfig(
                    maximum_bytes_billed=100_000_000,
                    query_parameters=[
                        bigquery.ScalarQueryParameter(
                            "season", "STRING", config["season"]
                        )
                    ],
                ),
                timeout=15,
                retry=None,
            ).result(timeout=30, retry=None)
        ]
        from google.api_core.exceptions import GoogleAPICallError
        from scripts.espn_game_mapping_weekly import observation_sql, summarize

        try:
            mapped = client.query(
                observation_sql(prefix),
                job_config=bigquery.QueryJobConfig(
                    maximum_bytes_billed=100_000_000,
                    query_parameters=[
                        bigquery.ScalarQueryParameter(
                            "season", "STRING", config["season"]
                        )
                    ],
                ),
                timeout=15,
                retry=None,
            ).result(timeout=30, retry=None)
            mapping = summarize([dict(r) for r in mapped])
        except (GoogleAPICallError, ValueError, KeyError, TypeError):
            mapping = {"status": "unavailable"}
        return {
            "season": config["season"],
            "provider": "espn",
            "groups": rows,
            "game_mapping": mapping,
        }
    sql = (
        "SELECT season, COUNT(*) AS row_count, CAST(MAX(game_date) AS STRING) AS data_through "
        f"FROM `{config['project']}.nba_gold.fct_player_game_stats` GROUP BY season"
    )
    dry = client.query(
        sql, job_config=bigquery.QueryJobConfig(dry_run=True), timeout=15, retry=None
    )
    if dry.total_bytes_processed > 100_000_000:
        raise ScopeBlocked("scope_query_over_budget")
    job = client.query(
        sql,
        job_config=bigquery.QueryJobConfig(maximum_bytes_billed=100_000_000),
        timeout=15,
        retry=None,
    )
    rows = [dict(row) for row in job.result(timeout=30, retry=None)]
    return assess_rows(rows, config["season"])


def environment(config):
    """Allow only runtime necessities; no model keys or inherited Airflow settings."""
    allowed = (
        "HOME",
        "USER",
        "LOGNAME",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "GOOGLE_APPLICATION_CREDENTIALS",
    )
    env = {k: os.environ[k] for k in allowed if k in os.environ}
    repo = Path(config["repo"])
    env.update(
        PATH=os.pathsep.join(
            (
                str(Path(config["airflow"]).parent),
                "/usr/bin",
                "/bin",
                "/usr/sbin",
                "/sbin",
            )
        ),
        PYTHON_DOTENV_DISABLED="1",
        PYTHONDONTWRITEBYTECODE="1",
        AIRFLOW_HOME=str(Path(config["state_dir"]) / "airflow"),
        AIRFLOW__CORE__DAGS_FOLDER=str(repo / "dags"),
        AIRFLOW__CORE__LOAD_EXAMPLES="False",
        AIRFLOW__CORE__DEFAULT_TIMEZONE=config["timezone"],
        AIRFLOW__CORE__EXECUTOR="SequentialExecutor",
        AIRFLOW__DATABASE__SQL_ALCHEMY_CONN="sqlite:///"
        + str(Path(config["state_dir"]) / "airflow" / "airflow.db"),
        BQ_PROJECT=config["project"],
        GCP_PROJECT_ID=config["project"],
        GCS_BUCKET_NAME=config["bucket"],
        BQ_LOCATION="US",
        NBA_GAME_LOG_EXTRACT_MODE="league",
        NBA_REPLAY_DAYS="14",
        NBA_API_TIMEOUT_SECONDS="10",
        NBA_API_RETRIES="2",
        NBA_GAME_LOG_CDN_FALLBACK_ENABLED="true",
    )
    return env


def command(args, *, config, log, timeout):
    with log.open("ab") as stream:
        process = subprocess.Popen(
            args,
            cwd=config["repo"],
            env=environment(config),
            stdout=stream,
            stderr=stream,
            start_new_session=True,
        )
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            raise RuntimeError("pipeline_timeout") from None
    if code:
        raise RuntimeError("pipeline_command_failed")


def record_coverage(config, directory, record, state):
    if config.get("source") != "espn" or record["kind"] != "refresh":
        return
    from scripts.espn_coverage import audit, summary

    report_path = directory / "evidence" / record["id"] / "result.json"
    if not report_path.exists():
        record["coverage_checks"] = {
            "status": "unavailable",
            "issues": ["coverage_report_unavailable"],
        }
        state["coverage_checks"] = record["coverage_checks"]
        return
    previous_path = directory / "coverage.json"
    previous = json.loads(previous_path.read_text()) if previous_path.exists() else None
    if previous is not None and not isinstance(previous, dict):
        raise ValueError("Invalid coverage ledger")
    ledger = audit(json.loads(report_path.read_text()), config["first_date"], previous)
    groups = record.get("observed_scope", {}).get("groups", [])
    if ledger["status"] == "passed" and groups:
        expected_rows = ledger["player_checks"].get("retained_rows", 0) - ledger[
            "player_checks"
        ].get("unresolved_rows", 0)
        actual_rows = sum(
            g.get("row_count", 0) for g in groups if g.get("season") == config["season"]
        )
        if expected_rows != actual_rows:
            ledger["issues"].append("published_row_count_mismatch")
            ledger["status"] = "blocked"
            for metric in ledger["metric_checks"].values():
                metric["coverage_ready"] = False
    write_json(previous_path, ledger)
    record["coverage_checks"] = summary(ledger)
    state["coverage_checks"] = record["coverage_checks"]


def record_mapping(config, directory, record, state):
    if config.get("source") != "espn":
        return
    mapping = dict(
        record.get("observed_scope", {}).get("game_mapping")
        or {"status": "unavailable"}
    )
    path = directory / "evidence" / record["id"] / "game-mapping" / "result.json"
    if record["kind"] == "refresh":
        try:
            raw = json.loads(path.read_text())
            if not isinstance(raw, dict):
                raise ValueError("Invalid mapping report")
            for key in (
                "source_status",
                "source_observed_at",
                "inserted",
                "duration_seconds",
            ):
                if key in raw:
                    mapping[key] = raw[key]
            if raw.get("status") != "ready":
                mapping["status"] = "degraded"
        except (OSError, ValueError, TypeError):
            mapping.update(status="degraded", source_status="unavailable")
    else:
        # Observing warehouse state is not a new discovery attempt.
        previous = state.get("game_mapping") or {}
        try:
            observation = json.loads((directory / "game-mapping.json").read_text())
            if (
                isinstance(observation, dict)
                and observation.get("season") == config["season"]
            ):
                previous = observation
        except (OSError, ValueError, TypeError):
            pass
        for key in (
            "source_status",
            "source_observed_at",
            "inserted",
            "duration_seconds",
        ):
            if key in previous:
                mapping[key] = previous[key]
    if mapping.get("source_status") not in (None, "captured"):
        mapping["status"] = "degraded"
    record["game_mapping"] = state["game_mapping"] = mapping


def refresh(config, *, force=False, check_only=False, now=None):
    config = validate_config(config)
    now = now or datetime.now(UTC)
    directory = Path(config["state_dir"])
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    status_path = directory / "status.json"
    with (directory / "refresh.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "already_running"}
        state = json.loads(status_path.read_text()) if status_path.exists() else {}
        current_slot = slot(now, config["timezone"], config["weekday"], config["hour"])
        if not force and not check_only and not due(state, current_slot, now):
            return {**state, "action": "not_due"}
        started = datetime.now(UTC)
        timer = time.monotonic()
        history = directory / "runs"
        history.mkdir(mode=0o700, exist_ok=True)
        record = {
            "version": "weekly_run/1",
            "id": uuid4().hex,
            "kind": "preflight" if check_only else "refresh",
            "season": config["season"],
            "source": config.get("source", "nba"),
            "trigger": "preflight"
            if check_only
            else "manual"
            if force
            else "scheduled_due_check",
            "scheduled_slot": current_slot.isoformat(),
            "started_at": started.isoformat(),
            "status": "running",
            "stages": [],
        }
        record_path = history / (record["id"] + ".json")
        write_json(record_path, record)
        if not check_only:
            state.pop("reason", None)
            if state.get("attempt_slot") != current_slot.isoformat():
                state["attempt_count"] = 0
            state.update(
                status="running",
                attempt_slot=current_slot.isoformat(),
                attempt_count=state.get("attempt_count", 0) + 1,
                last_attempt_at=now.isoformat(),
            )
            write_json(status_path, state)
        try:
            scope = warehouse_scope(config)
            record["observed_scope"] = scope
            state["scope_observed_at"] = datetime.now(UTC).isoformat()
            if check_only:
                state.update(status="preflight_passed", scope=scope)
                state.pop("reason", None)
            else:
                log = directory / ("run-" + record["id"] + ".log")
                record["log_name"] = log.name
                airflow = config["airflow"]
                commands = [
                    ("Prepare Airflow", [airflow, "db", "migrate"], 120),
                    (
                        "Refresh core pipeline",
                        [
                            airflow,
                            "dags",
                            "test",
                            "nba_analytics_pipeline",
                            now.isoformat(),
                            "--subdir",
                            str(Path(config["repo"]) / "dags" / "nba_analytics_dag.py"),
                        ],
                        7200,
                    ),
                ]
                if config.get("source") == "espn":
                    from scripts.espn_box_refresh import dates_for_refresh

                    first, last = dates_for_refresh(
                        datetime.fromisoformat(config["first_date"]).date(),
                        state.get("data_through"),
                        now,
                    )
                    commands = [
                        (
                            "Refresh ESPN result and box scores",
                            [
                                sys.executable,
                                str(
                                    Path(config["repo"]) / "scripts/espn_box_refresh.py"
                                ),
                                "--config",
                                str(directory / "config.json"),
                                "--first",
                                str(first),
                                "--last",
                                str(last),
                                "--output",
                                str(directory / "evidence" / record["id"]),
                            ],
                            7200,
                        )
                    ]
                    commands.append(
                        (
                            "Discover NBA game IDs",
                            [
                                sys.executable,
                                str(
                                    Path(config["repo"])
                                    / "scripts/espn_game_mapping_weekly.py"
                                ),
                                "--config",
                                str(directory / "config.json"),
                                "--output",
                                str(
                                    directory
                                    / "evidence"
                                    / record["id"]
                                    / "game-mapping"
                                ),
                            ],
                            600,
                        )
                    )
                for label, args, limit in commands:
                    stage = {
                        "name": label,
                        "status": "running",
                        "started_at": datetime.now(UTC).isoformat(),
                    }
                    record["stages"].append(stage)
                    write_json(record_path, record)
                    stage_timer = time.monotonic()
                    try:
                        command(args, config=config, log=log, timeout=limit)
                        stage["status"] = "success"
                        if label == "Discover NBA game IDs":
                            try:
                                mapping_result = json.loads(
                                    (
                                        directory
                                        / "evidence"
                                        / record["id"]
                                        / "game-mapping"
                                        / "result.json"
                                    ).read_text()
                                )
                                if mapping_result.get("status") != "ready":
                                    stage["status"] = "degraded"
                            except (OSError, ValueError, TypeError):
                                stage["status"] = "degraded"
                    except Exception:
                        stage["status"] = "failed"
                        raise
                    finally:
                        stage.update(
                            finished_at=datetime.now(UTC).isoformat(),
                            duration_seconds=round(time.monotonic() - stage_timer, 3),
                        )
                        write_json(record_path, record)
                scope = warehouse_scope(config)
                record["observed_scope"] = scope
                state["scope_observed_at"] = datetime.now(UTC).isoformat()
                dates = [r["data_through"] for r in scope["groups"] if r["row_count"]]
                if (
                    not dates
                    or (now.date() - datetime.fromisoformat(max(dates)).date()).days
                    > 10
                ):
                    raise RuntimeError("stale_or_empty_publication")
                state.update(
                    status="success",
                    last_success_slot=current_slot.isoformat(),
                    last_success_at=datetime.now(UTC).isoformat(),
                    data_through=max(dates),
                    log=str(log),
                )
        except ScopeBlocked as exc:
            state.update(
                status="blocked", reason=str(exc), last_checked_at=now.isoformat()
            )
            if exc.scope is not None:
                record["observed_scope"] = exc.scope
                state["scope_observed_at"] = datetime.now(UTC).isoformat()
        except Exception as exc:
            # Raw diagnostics remain in the private command log. Never serialize credentials.
            known = {
                "pipeline_timeout",
                "pipeline_command_failed",
                "stale_or_empty_publication",
            }
            state.update(
                status="failed",
                reason=str(exc) if str(exc) in known else type(exc).__name__,
                last_checked_at=now.isoformat(),
            )
        record_mapping(config, directory, record, state)
        try:
            record_coverage(config, directory, record, state)
        except (OSError, ValueError, KeyError, TypeError):
            record["coverage_checks"] = {
                "status": "unavailable",
                "issues": ["coverage_report_invalid"],
            }
            state["coverage_checks"] = record["coverage_checks"]
        record.update(
            status=state["status"],
            finished_at=datetime.now(UTC).isoformat(),
            duration_seconds=round(time.monotonic() - timer, 3),
        )
        if "reason" in state:
            record["reason"] = state["reason"]
        if "observed_scope" in record:
            state["observed_scope"] = record["observed_scope"]
        write_json(record_path, record)
        state.update(
            last_run_id=record["id"],
            last_duration_seconds=record["duration_seconds"],
            last_checked_at=record["finished_at"],
        )
        write_json(status_path, state)
        return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    result = refresh(
        json.loads(args.config.read_text()), force=args.force, check_only=args.check
    )
    print(json.dumps(result))
    return (
        0
        if result.get("status") in {"success", "preflight_passed", "already_running"}
        or result.get("action") == "not_due"
        else 2
    )


if __name__ == "__main__":
    raise SystemExit(main())
