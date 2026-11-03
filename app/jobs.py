"""Local, read-only job records. No application settings or cloud clients."""

from __future__ import annotations

import fcntl
import json
import math
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone
from ipaddress import ip_address
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from scripts.airflow_live_validate import redact_text
from scripts.weekly_refresh import due, slot

UTC = timezone.utc
ROOT = Path(__file__).resolve().parent
RUN_ID = re.compile(r"[a-f0-9]{32}")


def read_json(path, limit=262144):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise ValueError("Record unavailable or exceeds size limit")
    with path.open() as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("Expected a record object")
    return value


def scheduler_state(directory):
    """Check the installed configuration and loaded label, not just a plist draft."""
    path = Path.home() / "Library/LaunchAgents/com.nba-gcp.weekly-refresh.plist"
    if not path.exists():
        return {"installed": False, "loaded": False, "label": "Not enabled"}
    import plistlib

    try:
        agent = plistlib.loads(path.read_bytes())
        if not isinstance(agent, dict):
            raise ValueError("Invalid LaunchAgent")
        args = agent.get("ProgramArguments", [])
        index = args.index("--config")
        if Path(args[index + 1]).resolve() != directory / "config.json":
            return {
                "installed": False,
                "loaded": False,
                "label": "Different configuration installed",
            }
        loaded = subprocess.run(
            ["launchctl", "list"], capture_output=True, text=True, timeout=2, check=True
        )
        present = any(
            line.split()[-1:] == ["com.nba-gcp.weekly-refresh"]
            for line in loaded.stdout.splitlines()
        )
        disabled = subprocess.run(
            ["launchctl", "print-disabled", "gui/" + str(os.getuid())],
            capture_output=True,
            text=True,
            timeout=2,
            check=True,
        )
        is_disabled = bool(
            re.search(r'"com\.nba-gcp\.weekly-refresh"\s*=>\s*true', disabled.stdout)
        )
        return {
            "installed": True,
            "loaded": present and not is_disabled,
            "label": "Enabled"
            if present and not is_disabled
            else "Installed, not enabled",
        }
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return {
            "installed": True,
            "loaded": None,
            "label": "Scheduler status unavailable",
        }


def active_lock(directory):
    path = directory / "refresh.lock"
    if not path.exists():
        return False
    if path.is_symlink():
        raise ValueError("Invalid lock")
    with path.open("r") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(handle, fcntl.LOCK_UN)
            return False
        except BlockingIOError:
            return True


def safe_number(value):
    return (
        value
        if type(value) in (int, float) and math.isfinite(value) and value >= 0
        else None
    )


def game_mapping(raw):
    """Only bounded operational fields; never expose raw evidence or error text."""
    if not isinstance(raw, dict):
        return None
    result = {
        "status": raw.get("status")
        if raw.get("status") in ("ready", "degraded", "unavailable")
        else "unavailable"
    }
    for key in (
        "games",
        "reviewed_games",
        "resolved",
        "unresolved",
        "conflict",
        "discrepancy_count",
        "inserted",
        "duration_seconds",
    ):
        result[key] = safe_number(raw.get(key))
    for key in ("checked_at", "source_observed_at"):
        value = raw.get(key)
        try:
            result[key] = (
                value
                if isinstance(value, str)
                and datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo
                else None
            )
        except ValueError:
            result[key] = None
    result["source_status"] = (
        raw.get("source_status")
        if raw.get("source_status")
        in (
            "captured",
            "access_denied",
            "rate_limited",
            "timeout",
            "unavailable",
            "invalid_payload",
        )
        else None
    )
    discrepancies = raw.get("discrepancies")
    result["discrepancies"] = []
    for row in discrepancies[:50] if isinstance(discrepancies, list) else []:
        if not isinstance(row, dict):
            continue
        item = {}
        for key, pattern in (
            ("source_game_id", r"\d{1,20}"),
            ("nba_game_id", r"\d{10}"),
            ("date", r"\d{4}-\d{2}-\d{2}"),
            ("home", r"[A-Z]{3}"),
            ("away", r"[A-Z]{3}"),
        ):
            value = row.get(key)
            item[key] = (
                value
                if isinstance(value, str) and re.fullmatch(pattern, value)
                else None
            )
        for key in (
            "home_score",
            "away_score",
            "provider_home_score",
            "provider_away_score",
        ):
            item[key] = safe_number(row.get(key))
        item["identity_verified_separately"] = (
            row.get("identity_verified_separately") is True
        )
        item["reason"] = (
            row.get("reason")
            if row.get("reason") in ("score_conflict", "mapping_conflict")
            else "mapping_conflict"
        )
        result["discrepancies"].append(item)
    return result


def coverage_checks(raw):
    """Expose bounded check results, never arbitrary evidence or file paths."""
    if not isinstance(raw, dict):
        return None
    result = {
        "status": raw.get("status")
        if raw.get("status") in ("passed", "blocked", "unavailable")
        else "unavailable"
    }
    for key in ("first_date", "checked_through", "games_checked_through"):
        value = raw.get(key)
        result[key] = (
            value
            if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value)
            else None
        )
    for key in (
        "dates_expected",
        "dates_checked",
        "expected_final_games",
        "accepted_games",
    ):
        result[key] = safe_number(raw.get(key))
    for key in ("missing_dates", "missing_games", "duplicate_games", "issues"):
        result[key] = (
            [
                v
                for v in raw.get(key, [])[:366]
                if isinstance(v, str)
                and re.fullmatch(
                    r"[a-z_]{1,80}"
                    if key == "issues"
                    else r"\d{4}-\d{2}-\d{2}"
                    if key == "missing_dates"
                    else r"\d{1,20}",
                    v,
                )
            ]
            if isinstance(raw.get(key), list)
            else []
        )
    players = raw.get("player_checks", {})
    result["player_checks"] = (
        {
            k: safe_number(players.get(k))
            for k in (
                "played_rows",
                "retained_rows",
                "dropped_rows",
                "unexpected_rows",
                "unresolved_rows",
                "duplicate_rows",
            )
        }
        if isinstance(players, dict)
        else {}
    )
    metrics = raw.get("metric_checks", {})
    result["metric_checks"] = (
        {
            key: {
                "coverage_ready": value.get("coverage_ready") is True,
                "missing_input_rows": safe_number(value.get("missing_input_rows")),
            }
            for key, value in list(metrics.items())[:40]
            if isinstance(key, str)
            and re.fullmatch(r"[a-z0-9_]{1,40}", key)
            and isinstance(value, dict)
        }
        if isinstance(metrics, dict)
        else {}
    )
    return result


def public_record(raw, running=False):
    record = {
        k: raw.get(k)
        for k in (
            "id",
            "kind",
            "season",
            "status",
            "reason",
            "started_at",
            "finished_at",
            "scheduled_slot",
            "source",
        )
    }
    if any(
        value is not None and not isinstance(value, str) for value in record.values()
    ):
        raise ValueError("Invalid run fields")
    if not isinstance(record["id"], str) or not RUN_ID.fullmatch(record["id"]):
        raise ValueError("Invalid record identity")
    if record["status"] == "running" and not running:
        record["status"] = "interrupted"
        record["reason"] = "worker_not_running"
    record["duration_seconds"] = safe_number(raw.get("duration_seconds"))
    record["has_log"] = raw.get("log_name") == "run-" + record["id"] + ".log"
    record["coverage_checks"] = coverage_checks(raw.get("coverage_checks"))
    record["game_mapping"] = game_mapping(raw.get("game_mapping"))
    stages = raw.get("stages") or []
    if not isinstance(stages, list):
        raise ValueError("Invalid stages")
    record["stages"] = [
        {
            "name": str(s.get("name", "Stage"))[:100],
            "status": s.get("status"),
            "duration_seconds": safe_number(s.get("duration_seconds")),
        }
        for s in stages[:20]
        if isinstance(s, dict)
    ]
    return record


def snapshot(directory, scheduler=scheduler_state, now=None):
    now = now or datetime.now(UTC)
    issues = []

    def optional(name):
        path = directory / name
        if not path.exists():
            return {}
        try:
            return read_json(path)
        except (OSError, ValueError):
            issues.append(name + " could not be read; shown values may be incomplete.")
            return {}

    config, state = optional("config.json"), optional("status.json")
    try:
        running = active_lock(directory)
    except (OSError, ValueError):
        running = False
        issues.append("Worker lock could not be checked.")
    history = []
    folder = directory / "runs"
    if folder.is_symlink():
        issues.append("Run history directory is unavailable.")
    elif folder.exists():
        import itertools

        paths = list(itertools.islice(folder.glob("*.json"), 1001))
        if len(paths) > 1000:
            issues.append("History is limited to 1,000 records.")
        for path in paths[:1000]:
            try:
                raw = read_json(path)
                if path.stem != raw.get("id"):
                    raise ValueError("Record identity differs")
                history.append(public_record(raw))
            except (OSError, ValueError, TypeError):
                issues.append("A run record could not be read.")
    history.sort(key=lambda r: r.get("started_at") or "", reverse=True)
    if history and running and history[0]["reason"] == "worker_not_running":
        history[0].update(status="running", reason=None)
    scheduled = scheduler(directory)
    plan = None
    attempts = 0
    if config:
        try:
            zone = config["timezone"]
            current = slot(now, zone, config["weekday"], config["hour"])
            if state.get("attempt_slot") == current.isoformat():
                attempts = state.get("attempt_count", 0)
            future = (
                current.astimezone(ZoneInfo(zone)) + timedelta(days=7)
            ).astimezone(UTC)
            retry_at = None
            if scheduled["loaded"] and not running and due(state, current, now):
                retry_at = now.isoformat()
            elif (
                scheduled["loaded"]
                and not running
                and state.get("attempt_slot") == current.isoformat()
                and state.get("attempt_count", 0) < 3
                and state.get("last_success_slot") != current.isoformat()
            ):
                retry = datetime.fromisoformat(state["last_attempt_at"]) + timedelta(
                    days=1
                )
                if now < retry < future:
                    retry_at = retry.isoformat()
            plan = {
                "timezone": zone,
                "weekday": config["weekday"],
                "hour": config["hour"],
                "next_weekly_at": future.isoformat(),
                "next_scheduled_run": (retry_at or future.isoformat())
                if scheduled["loaded"]
                else None,
                "retry_at": retry_at,
                "max_attempts": 3,
            }
        except (KeyError, ValueError, TypeError):
            issues.append("The schedule configuration could not be read.")
    scope = state.get("observed_scope")
    groups = scope.get("groups", []) if isinstance(scope, dict) else []
    if not isinstance(groups, list):
        groups = []
        issues.append("Observed coverage could not be read.")
    if config.get("source") == "espn":
        excluded = sum(
            g.get("unmapped_rows", 0)
            for g in groups
            if isinstance(g, dict) and type(g.get("unmapped_rows", 0)) is int
        )
        if excluded:
            issues.append(
                f"{excluded} ESPN player appearances have unresolved NBA identity and are excluded from NBA-mapped queries."
            )
        missing = sum(
            g.get("missing_minutes", 0)
            for g in groups
            if isinstance(g, dict) and type(g.get("missing_minutes", 0)) is int
        )
        if missing:
            issues.append(
                f"{missing} ESPN appearances have unreported minutes; these remain unknown rather than zero."
            )
    target = config.get("season")
    observed = [
        {
            "season": g.get("season") if isinstance(g.get("season"), str) else None,
            "row_count": safe_number(g.get("row_count")),
            "accepted_games": safe_number(g.get("accepted_games")),
            "unmapped_rows": safe_number(g.get("unmapped_rows")),
            "data_through": g.get("data_through")
            if isinstance(g.get("data_through"), str)
            else None,
        }
        for g in groups
        if isinstance(g, dict)
    ]
    target_dates = [
        g["data_through"]
        for g in observed
        if g["season"] == target and g.get("data_through")
    ]
    through = max(target_dates, default=None)
    age = None
    if through:
        try:
            age = (
                now.astimezone(ZoneInfo(config["timezone"])).date()
                - datetime.fromisoformat(through).date()
            ).days
        except (ValueError, KeyError):
            issues.append("Freshness date is invalid.")
    status = state.get("status", "not_run")
    reason = state.get("reason")
    if history and history[0]["status"] in ("running", "interrupted"):
        status = history[0]["status"]
        reason = history[0]["reason"]
    return {
        "checked_at": now.isoformat(),
        "job": {
            "id": "weekly-espn" if config.get("source") == "espn" else "weekly-core",
            "name": "Weekly ESPN box-score refresh"
            if config.get("source") == "espn"
            else "Weekly NBA refresh",
            "source": config.get("source", "nba"),
            "description": "ESPN-reported results and validated player box scores, including defensive rebounds. Coverage is an observed sample; play-by-play and deflections are outside this job."
            if config.get("source") == "espn"
            else "Core box scores and defensive rebounds, with the pipeline’s existing validation and publication checks.",
            "season": target,
            "status": status,
            "reason": reason,
            "last_checked_at": state.get("last_checked_at"),
            "last_attempt_at": state.get("last_attempt_at"),
            "last_success_at": state.get("last_success_at"),
            "last_duration_seconds": safe_number(state.get("last_duration_seconds")),
            "attempt_count": attempts,
            "scheduler": scheduled,
            "schedule": plan,
            "freshness": {
                "data_through": through,
                "age_days": age,
                "state": "unavailable"
                if through is None
                else "stale"
                if age is None or age < 0 or age > 10
                else "within_threshold",
                "observed": observed,
                "observed_at": state.get("scope_observed_at"),
            },
            "history": history,
            "coverage_checks": coverage_checks(state.get("coverage_checks")),
            "game_mapping": game_mapping(state.get("game_mapping")),
            "issues": issues,
        },
    }


def require_local(request):
    try:
        peer = request.client and ip_address(request.client.host).is_loopback
    except ValueError:
        peer = False
    forwarded = any(
        k
        in {
            "forwarded",
            "x-real-ip",
            "via",
            "true-client-ip",
            "cf-connecting-ip",
            "x-client-ip",
        }
        or k.startswith(("x-forwarded-", "x-original-forwarded-"))
        for k in request.headers
    )
    origin = request.headers.get("origin")
    if (
        not peer
        or request.url.hostname not in {"127.0.0.1", "localhost", "::1"}
        or forwarded
        or len(request.headers.getlist("host")) != 1
        or len(request.headers.getlist("origin")) > 1
        or (origin is not None and origin != str(request.base_url).rstrip("/"))
        or request.headers.get("sec-fetch-site") == "cross-site"
    ):
        raise HTTPException(
            403, "Jobs are available through direct localhost access only"
        )


def create_jobs_app(state_dir, *, scheduler=scheduler_state, also_state_dirs=()):
    directory = Path(state_dir).resolve()
    directories = list(
        dict.fromkeys([directory, *[Path(p).resolve() for p in also_state_dirs]])
    )
    app = FastAPI(
        title="NBA Stats Desk · Jobs", docs_url=None, redoc_url=None, openapi_url=None
    )

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        try:
            require_local(request)
        except HTTPException as exc:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        response = await call_next(request)
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
                "Referrer-Policy": "no-referrer",
            }
        )
        return response

    @app.get("/")
    @app.get("/jobs")
    def page():
        return FileResponse(ROOT / "templates/jobs.html")

    @app.get("/api/jobs")
    def jobs():
        result = snapshot(directory, scheduler)
        result["jobs"] = [
            result["job"],
            *[snapshot(p, scheduler)["job"] for p in directories[1:]],
        ]
        return result

    @app.get("/api/jobs/runs/{run_id}/log")
    def log(run_id: str):
        if not RUN_ID.fullmatch(run_id):
            raise HTTPException(404, "Unknown run")
        try:
            candidates = [
                p for p in directories if (p / "runs" / (run_id + ".json")).is_file()
            ]
            if len(candidates) != 1:
                raise ValueError("Unknown or ambiguous run")
            selected = candidates[0]
            record_path = selected / "runs" / (run_id + ".json")
            if record_path.parent.is_symlink():
                raise ValueError("Invalid history directory")
            record = read_json(record_path)
            if (
                record.get("id") != run_id
                or record.get("log_name") != "run-" + run_id + ".log"
            ):
                raise ValueError("No command log for this run")
            path = selected / record["log_name"]
            if path.is_symlink():
                raise ValueError("Invalid log")
            with path.open("rb") as handle:
                handle.seek(max(0, path.stat().st_size - 65536))
                content = handle.read(65536).decode("utf-8", errors="replace")
            return {
                "text": redact_text(content),
                "truncated": path.stat().st_size > 65536,
            }
        except (OSError, ValueError):
            raise HTTPException(404, "No command log available") from None

    @app.get("/assets/{name}")
    def assets(name: str):
        if name not in {"jobs.css", "jobs.js"}:
            raise HTTPException(404)
        return FileResponse(ROOT / "static" / name)

    @app.get("/fonts/{name}")
    def fonts(name: str):
        if name not in {
            "barlow.css",
            "barlow-400.ttf",
            "barlow-500.ttf",
            "barlow-600.ttf",
            "barlow-700.ttf",
            "barlow-condensed-600.ttf",
            "barlow-condensed-700.ttf",
        }:
            raise HTTPException(404)
        return FileResponse(ROOT / "static/fonts" / name)

    return app
