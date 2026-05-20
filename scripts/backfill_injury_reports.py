#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dags"))

import nba_pipeline as pipeline  # noqa: E402
import nba_source_contracts as source_contracts  # noqa: E402


DEFAULT_MAX_CANDIDATES = 240
DEFAULT_DBT_SELECTOR = [
    "stg_player_injury_reports_clean",
    "player_availability_current",
]
DEFAULT_DBT_EXCLUDES = [
    "source:gold_runtime.analysis_snapshots",
    "path:dbt/tests/no_duplicate_analysis_snapshots.sql",
]


@dataclass(frozen=True)
class BackfillConfig:
    project_id: str
    bucket_name: str
    bronze_dataset: str
    metadata_dataset: str
    location: str
    dbt_target: str


class BackfillError(RuntimeError):
    pass


def repo_root() -> Path:
    return ROOT


def load_dotenv_values(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        from dotenv import dotenv_values

        return {
            key: value
            for key, value in dotenv_values(path).items()
            if key and value is not None
        }
    except Exception:
        values: dict[str, str] = {}
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("'\"")
        return values


def prepare_environment(
    root: Path,
    *,
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    env = dict(base_env if base_env is not None else os.environ)
    for key, value in load_dotenv_values(root / ".env").items():
        env.setdefault(key, value)
    if env.get("GCP_PROJECT_ID"):
        env.setdefault("BQ_PROJECT", env["GCP_PROJECT_ID"])
    env.setdefault("BQ_DATASET_BRONZE", "nba_bronze")
    env.setdefault("BQ_DATASET_SILVER", "nba_silver")
    env.setdefault("BQ_DATASET_GOLD", "nba_gold")
    env.setdefault("BQ_METADATA_DATASET", "nba_metadata")
    env.setdefault("BQ_LOCATION", "US")
    env.setdefault("DBT_TARGET", "dev")
    airflow_bin = root / ".venv-airflow" / "bin"
    if airflow_bin.exists():
        env["PATH"] = f"{airflow_bin}{os.pathsep}{env.get('PATH', '')}"
    env.setdefault("PYTHONUNBUFFERED", "1")
    return env


def env_int(env: Mapping[str, str], key: str, default: int) -> int:
    raw_value = env.get(key)
    if raw_value is None:
        return default
    try:
        return int(raw_value)
    except ValueError:
        return default


def env_float(env: Mapping[str, str], key: str, default: float) -> float:
    raw_value = env.get(key)
    if raw_value is None:
        return default
    try:
        return float(raw_value)
    except ValueError:
        return default


def load_backfill_config(env: Mapping[str, str], *, dbt_target: str | None) -> BackfillConfig:
    project_id = env.get("BQ_PROJECT") or env.get("GCP_PROJECT_ID") or ""
    bucket_name = env.get("GCS_BUCKET_NAME") or ""
    missing = [
        name
        for name, value in [
            ("BQ_PROJECT or GCP_PROJECT_ID", project_id),
            ("GCS_BUCKET_NAME", bucket_name),
        ]
        if not value
    ]
    if missing:
        raise BackfillError(
            "Missing required environment value(s): " + ", ".join(missing)
        )
    return BackfillConfig(
        project_id=project_id,
        bucket_name=bucket_name,
        bronze_dataset=env.get("BQ_DATASET_BRONZE", "nba_bronze"),
        metadata_dataset=env.get("BQ_METADATA_DATASET", "nba_metadata"),
        location=env.get("BQ_LOCATION", "US"),
        dbt_target=dbt_target or env.get("DBT_TARGET", "dev"),
    )


def parse_report_times(value: str | None) -> list[str]:
    raw = value or pipeline.DEFAULT_INJURY_REPORT_TIME_ET
    normalized = [
        pipeline.normalize_injury_report_time_et(item.strip())
        for item in raw.split(",")
        if item.strip()
    ]
    return normalized or [pipeline.DEFAULT_INJURY_REPORT_TIME_ET]


def build_candidate_plan(
    *,
    start_date: str,
    end_date: str,
    report_times_et: list[str],
    max_candidates: int,
    allow_large_window: bool,
) -> list[dict[str, Any]]:
    candidates = pipeline.build_injury_report_candidates(
        start_date=start_date,
        end_date=end_date,
        report_times_et=report_times_et,
        max_reports=0,
    )
    limit = max(int(max_candidates), 0)
    if limit and len(candidates) > limit and not allow_large_window:
        raise BackfillError(
            f"Refusing to process {len(candidates)} injury-report candidates; "
            f"raise --max-candidates or pass --allow-large-window."
        )
    return candidates


def candidate_summary(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    if not candidates:
        return {
            "candidate_count": 0,
            "first_report_date": None,
            "last_report_date": None,
            "report_times_et": [],
        }
    return {
        "candidate_count": len(candidates),
        "first_report_date": candidates[0]["report_date"].isoformat(),
        "last_report_date": candidates[-1]["report_date"].isoformat(),
        "report_times_et": sorted(
            {str(candidate["report_time_et"]) for candidate in candidates}
        ),
        "first_source_url": candidates[0]["source_url"],
        "last_source_url": candidates[-1]["source_url"],
    }


def utc_timestamp_for_path() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def utc_timestamp_for_run_id() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def report_path(root: Path, timestamp: str) -> Path:
    output_dir = root / "reports" / "pipeline_triage"
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir / f"injury_backfill_{timestamp}.json"


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n")


def format_command(args: list[str]) -> str:
    return " ".join(shlex.quote(arg) for arg in args)


def resolve_dbt_cmd(root: Path) -> list[str]:
    local_dbt = root / ".venv-airflow" / "bin" / "dbt"
    if local_dbt.exists():
        return [str(local_dbt)]
    dbt = shutil.which("dbt")
    if dbt:
        return [dbt]
    raise BackfillError("Could not find dbt on PATH or in .venv-airflow.")


def run_dbt_injury_build(
    *,
    root: Path,
    env: Mapping[str, str],
    target: str,
) -> dict[str, Any]:
    command = [
        *resolve_dbt_cmd(root),
        "build",
        "--project-dir",
        str(root),
        "--profiles-dir",
        str(root / "dbt" / "profiles"),
        "--target",
        target,
        "--exclude",
        *DEFAULT_DBT_EXCLUDES,
        "--select",
        *DEFAULT_DBT_SELECTOR,
    ]
    started = time.monotonic()
    completed = subprocess.run(
        command,
        cwd=root,
        env=dict(env),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    result = {
        "command": format_command(command),
        "returncode": completed.returncode,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "stdout_tail": completed.stdout[-3000:],
        "stderr_tail": completed.stderr[-3000:],
    }
    if completed.returncode != 0:
        raise BackfillError(
            "Targeted dbt injury build failed with exit code "
            f"{completed.returncode}: {format_command(command)}"
        )
    return result


def run_backfill(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    root = repo_root()
    env = prepare_environment(root)
    if args.season != pipeline.SUPPORTED_SEASON:
        raise BackfillError(
            f"Unsupported season {args.season!r}; this repo supports "
            f"{pipeline.SUPPORTED_SEASON!r}."
        )

    report_times = parse_report_times(args.report_times_et)
    candidates = build_candidate_plan(
        start_date=args.start_date,
        end_date=args.end_date,
        report_times_et=report_times,
        max_candidates=args.max_candidates,
        allow_large_window=args.allow_large_window,
    )
    run_id = args.run_id or f"manual__injury_backfill_{utc_timestamp_for_run_id()}"
    report: dict[str, Any] = {
        "status": "running",
        "run_id": run_id,
        "season": args.season,
        "started_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "dry_run": args.dry_run,
        "candidate_summary": candidate_summary(candidates),
        "config": {
            "start_date": args.start_date,
            "end_date": args.end_date,
            "report_times_et": report_times,
            "max_candidates": args.max_candidates,
            "delay_seconds": args.delay_seconds,
            "timeout_seconds": args.timeout_seconds,
            "retries": args.retries,
            "skip_dbt": args.skip_dbt,
            "advance_empty_watermark": args.advance_empty_watermark,
        },
    }

    if args.dry_run:
        report["status"] = "dry_run"
        report["completed_at_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
        return 0, report

    from google.cloud import bigquery

    config = load_backfill_config(env, dbt_target=args.dbt_target)
    report["warehouse"] = {
        "project_id": config.project_id,
        "bronze_dataset": config.bronze_dataset,
        "metadata_dataset": config.metadata_dataset,
        "location": config.location,
    }

    client = bigquery.Client(project=config.project_id, location=config.location)
    pipeline.ensure_dataset(
        client, f"{config.project_id}.{config.bronze_dataset}", config.location
    )
    pipeline.ensure_dataset(
        client, f"{config.project_id}.{config.metadata_dataset}", config.location
    )
    state_table = f"{config.project_id}.{config.metadata_dataset}.ingestion_state"
    run_table = f"{config.project_id}.{config.metadata_dataset}.pipeline_run_log"
    pipeline.create_metadata_tables(client, state_table, run_table)
    state = pipeline.get_ingestion_state(
        client,
        state_table,
        source_system=pipeline.INJURY_REPORT_SOURCE_SYSTEM,
        season=args.season,
    )
    report["watermark_before"] = (
        state["watermark_date"].isoformat() if state["watermark_date"] else None
    )

    injury_df = pipeline.get_all_official_injury_reports(
        candidates,
        season=args.season,
        delay=args.delay_seconds,
        timeout=args.timeout_seconds,
        retries=args.retries,
        retry_base_delay=args.retry_base_delay_seconds,
        retry_backoff_multiplier=args.retry_backoff_multiplier,
        retry_max_delay=args.retry_max_delay_seconds,
    )
    report["rows_extracted_raw"] = int(len(injury_df))

    candidate_watermark = max(
        (candidate["report_date"] for candidate in candidates), default=None
    )
    if injury_df.empty:
        report["status"] = "empty_source_response"
        if args.advance_empty_watermark and candidate_watermark is not None:
            pipeline.upsert_ingestion_state(
                client,
                state_table,
                season=args.season,
                watermark_date=candidate_watermark,
                source_system=pipeline.INJURY_REPORT_SOURCE_SYSTEM,
            )
            report["watermark_after"] = candidate_watermark.isoformat()
        record = pipeline.build_run_metadata_record(
            dag_run_id=run_id,
            source_system=pipeline.INJURY_REPORT_SOURCE_SYSTEM,
            season=args.season,
            status=report["status"],
            rows_extracted=0,
            rows_loaded=0,
            rows_inserted=0,
            rows_updated=0,
            watermark_before=state["watermark_date"],
            watermark_after=report.get("watermark_after"),
            details=json.dumps(report["candidate_summary"], sort_keys=True),
        )
        pipeline.record_pipeline_run(client, run_table, record)
        report["completed_at_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
        return 0, report

    validation = source_contracts.validate_source_contract("injury_reports", injury_df)
    injury_df = validation.frame
    report["source_contract"] = validation.result
    report["rows_quarantined"] = int(len(validation.quarantine_frame))
    report["rows_after_contract"] = int(len(injury_df))
    if injury_df.empty:
        raise BackfillError("Source contract quarantined every injury report row.")

    run_stamp = utc_timestamp_for_path()
    min_date = pd.to_datetime(injury_df["REPORT_DATE"]).min().strftime("%Y%m%d")
    max_date = pd.to_datetime(injury_df["REPORT_DATE"]).max().strftime("%Y%m%d")
    blob_path = (
        f"nba_data/{args.season}/landing/backfill/"
        f"{run_stamp}_{min_date}_{max_date}_injury_reports.csv"
    )
    gcs_uri = pipeline.upload_df_to_gcs(
        injury_df,
        config.project_id,
        config.bucket_name,
        blob_path,
        if_generation_match=0,
    )
    report["gcs_uri"] = gcs_uri

    staging_table = f"{config.project_id}.{config.bronze_dataset}.stg_player_injury_reports"
    raw_table = f"{config.project_id}.{config.bronze_dataset}.raw_player_injury_reports"
    pipeline.load_gcs_to_bigquery(
        client,
        gcs_uri,
        staging_table,
        pipeline.get_injury_report_schema(),
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
    )
    report["staging_table"] = staging_table

    dq = pipeline.run_injury_report_quality_checks(
        client, staging_table, season=args.season
    )
    merge_result = pipeline.create_and_merge_injury_report_table(
        client, staging_table, raw_table
    )
    reconciliation = pipeline.validate_merge_reconciliation(
        domain="injury_reports",
        rows_loaded=len(injury_df),
        pre_count=merge_result["pre_count"],
        post_count=merge_result["post_count"],
        inserted=merge_result["inserted"],
        updated=merge_result["updated"],
    )
    watermark_after = pipeline.coerce_to_date(injury_df["REPORT_DATE"].max())
    pipeline.upsert_ingestion_state(
        client,
        state_table,
        season=args.season,
        watermark_date=watermark_after,
        source_system=pipeline.INJURY_REPORT_SOURCE_SYSTEM,
    )

    report.update(
        {
            "status": "bronze_loaded",
            "raw_table": raw_table,
            "dq_results": dq,
            "merge_result": merge_result,
            "reconciliation": reconciliation,
            "watermark_after": watermark_after.isoformat()
            if watermark_after is not None
            else None,
        }
    )

    if not args.skip_dbt:
        dbt_env = dict(env)
        dbt_env.setdefault("BQ_PROJECT", config.project_id)
        dbt_env.setdefault("BQ_DATASET_BRONZE", config.bronze_dataset)
        dbt_env.setdefault("BQ_METADATA_DATASET", config.metadata_dataset)
        dbt_env.setdefault("NBA_SEASON", args.season)
        report["dbt"] = run_dbt_injury_build(
            root=root,
            env=dbt_env,
            target=config.dbt_target,
        )
        report["status"] = "success"
    else:
        report["dbt"] = {"status": "skipped"}
        report["status"] = "success_without_dbt"

    record = pipeline.build_run_metadata_record(
        dag_run_id=run_id,
        source_system=pipeline.INJURY_REPORT_SOURCE_SYSTEM,
        season=args.season,
        status=report["status"],
        gcs_uri=gcs_uri,
        rows_extracted=len(injury_df),
        rows_loaded=len(injury_df),
        rows_inserted=merge_result["inserted"],
        rows_updated=merge_result["updated"],
        watermark_before=state["watermark_date"],
        watermark_after=watermark_after,
        details=(
            f"candidate_count={len(candidates)};"
            f"rows_unchanged={reconciliation['unchanged']};"
            f"dbt_status={report['dbt'].get('status', 'success')}"
        ),
    )
    pipeline.record_pipeline_run(client, run_table, record)
    report["completed_at_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
    return 0, report


def build_arg_parser(env: Mapping[str, str]) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill official NBA injury reports into bronze and rebuild the "
            "targeted injury availability dbt models."
        )
    )
    parser.add_argument("--start-date", required=True, help="Inclusive report start date.")
    parser.add_argument("--end-date", required=True, help="Inclusive report end date.")
    parser.add_argument(
        "--season",
        default=pipeline.SUPPORTED_SEASON,
        help=f"Supported season. Default: {pipeline.SUPPORTED_SEASON}.",
    )
    parser.add_argument(
        "--report-times-et",
        default=env.get(
            "NBA_INJURY_REPORT_TIMES_ET", pipeline.DEFAULT_INJURY_REPORT_TIME_ET
        ),
        help="Comma-separated official report time tokens, e.g. 05_00PM.",
    )
    parser.add_argument(
        "--max-candidates",
        type=int,
        default=env_int(env, "NBA_INJURY_REPORT_BACKFILL_MAX_CANDIDATES", DEFAULT_MAX_CANDIDATES),
        help="Safety cap for date/time candidates before network or warehouse work.",
    )
    parser.add_argument(
        "--allow-large-window",
        action="store_true",
        help="Allow candidate windows larger than --max-candidates.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build and report the candidate plan without network or GCP calls.",
    )
    parser.add_argument(
        "--delay-seconds",
        type=float,
        default=env_float(env, "NBA_INJURY_REPORT_DELAY_SECONDS", 0.25),
        help="Delay between official PDF fetches.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=env_float(env, "NBA_API_TIMEOUT_SECONDS", 15.0),
        help="HTTP timeout for official PDF fetches.",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=env_int(env, "NBA_API_RETRIES", 3),
        help="Bounded retry count for each official PDF fetch.",
    )
    parser.add_argument(
        "--retry-base-delay-seconds",
        type=float,
        default=env_float(env, "NBA_API_RETRY_BASE_DELAY_SECONDS", 1.0),
    )
    parser.add_argument(
        "--retry-backoff-multiplier",
        type=float,
        default=env_float(env, "NBA_API_RETRY_BACKOFF_MULTIPLIER", 2.0),
    )
    parser.add_argument(
        "--retry-max-delay-seconds",
        type=float,
        default=env_float(env, "NBA_API_RETRY_MAX_DELAY_SECONDS", 8.0),
    )
    parser.add_argument(
        "--skip-dbt",
        action="store_true",
        help="Load and merge bronze injury rows without rebuilding dbt models.",
    )
    parser.add_argument(
        "--dbt-target",
        default=env.get("DBT_TARGET", "dev"),
        help="dbt target used for the targeted injury model build.",
    )
    parser.add_argument(
        "--advance-empty-watermark",
        action="store_true",
        help="Advance injury watermark even when the candidate window returns no rows.",
    )
    parser.add_argument("--run-id", default="", help="Optional metadata run id override.")
    parser.add_argument(
        "--report-path",
        default="",
        help="Optional JSON report path. Defaults under reports/pipeline_triage/.",
    )
    return parser


def main() -> int:
    root = repo_root()
    env = prepare_environment(root)
    parser = build_arg_parser(env)
    args = parser.parse_args()
    timestamp = utc_timestamp_for_path()
    output_path = Path(args.report_path) if args.report_path else report_path(root, timestamp)
    try:
        exit_code, report = run_backfill(args)
    except Exception as exc:
        report = {
            "status": "failed",
            "error": str(exc),
            "completed_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        write_report(output_path, report)
        print(f"Injury backfill report: {output_path}", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        return 1
    write_report(output_path, report)
    print(f"Injury backfill report: {output_path}")
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
