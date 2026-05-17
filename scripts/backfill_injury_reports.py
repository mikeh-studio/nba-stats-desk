"""Backfill official NBA injury reports into bronze and injury gold models."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import uuid
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
from google.cloud import bigquery

ROOT = Path(__file__).resolve().parents[1]
DAGS_DIR = ROOT / "dags"
if str(DAGS_DIR) not in sys.path:
    sys.path.insert(0, str(DAGS_DIR))

import nba_pipeline as pipeline  # noqa: E402
import nba_source_contracts as source_contracts  # noqa: E402


def _env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    return value if value not in {"", None} else default


def _int_env(name: str, default: int) -> int:
    value = _env(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {value!r}") from exc


def _float_env(name: str, default: float) -> float:
    value = _env(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {value!r}") from exc


def _date_arg(value: str | None) -> date | None:
    if not value:
        return None
    parsed = pipeline.coerce_to_date(value)
    if parsed is None:
        raise argparse.ArgumentTypeError(f"Invalid date: {value!r}")
    return parsed


def _default_start_date(
    client: bigquery.Client,
    *,
    project_id: str,
    bronze_dataset: str,
    season: str,
) -> date:
    query = f"""
    SELECT MIN(GAME_DATE) AS start_date
    FROM `{project_id}.{bronze_dataset}.raw_game_logs`
    WHERE SEASON = @season
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("season", "STRING", season),
        ]
    )
    rows = list(client.query(query, job_config=job_config).result())
    start = pipeline.coerce_to_date(rows[0]["start_date"] if rows else None)
    if start is None:
        start = pipeline.get_season_date_bounds(season)[0]
    return start


def _upload_and_load_staging(
    client: bigquery.Client,
    frame: pd.DataFrame,
    *,
    project_id: str,
    bucket_name: str,
    bronze_dataset: str,
    season: str,
    run_id: str,
) -> tuple[str, str]:
    run_stamp = pd.Timestamp.now(tz="UTC").strftime("%Y%m%dT%H%M%SZ")
    min_date = pd.to_datetime(frame["REPORT_DATE"]).min().strftime("%Y%m%d")
    max_date = pd.to_datetime(frame["REPORT_DATE"]).max().strftime("%Y%m%d")
    blob_path = (
        f"nba_data/{season}/landing/backfill/injury_reports/"
        f"run_id={run_id}/{run_stamp}_{min_date}_{max_date}_injury_reports.csv"
    )
    gcs_uri = pipeline.upload_df_to_gcs(frame, project_id, bucket_name, blob_path)
    staging_table = f"{project_id}.{bronze_dataset}.stg_player_injury_reports"
    pipeline.load_gcs_to_bigquery(
        client,
        gcs_uri,
        staging_table,
        pipeline.get_injury_report_schema(),
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
    )
    return gcs_uri, staging_table


def _run_dbt_injury_build(args: argparse.Namespace, *, project_id: str) -> None:
    if args.skip_dbt:
        return

    dbt_bin = Path(args.dbt_bin)
    command = [
        str(dbt_bin) if dbt_bin.exists() else args.dbt_bin,
        "build",
        "--project-dir",
        str(ROOT),
        "--profiles-dir",
        str(ROOT / "dbt" / "profiles"),
        "--target",
        args.dbt_target,
        "--exclude",
        "source:gold_runtime.analysis_snapshots",
        "path:dbt/tests/no_duplicate_analysis_snapshots.sql",
        "--select",
        "stg_player_injury_reports_clean",
        "player_availability_current",
    ]
    env = os.environ.copy()
    env.setdefault("BQ_PROJECT", project_id)
    env.setdefault("BQ_DATASET_BRONZE", args.bronze_dataset)
    env.setdefault("BQ_DATASET_SILVER", args.silver_dataset)
    env.setdefault("BQ_DATASET_GOLD", args.gold_dataset)
    env.setdefault("NBA_SEASON", args.season)
    completed = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.stdout:
        print(completed.stdout)
    if completed.stderr:
        print(completed.stderr, file=sys.stderr)
    if completed.returncode != 0:
        raise RuntimeError(f"dbt injury build failed with code {completed.returncode}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Backfill official NBA injury reports for a date window."
    )
    parser.add_argument("--project-id", default=_env("BQ_PROJECT", _env("GCP_PROJECT_ID")))
    parser.add_argument("--bucket", default=_env("GCS_BUCKET_NAME"))
    parser.add_argument("--season", default=_env("NBA_SEASON", pipeline.SUPPORTED_SEASON))
    parser.add_argument(
        "--start-date",
        type=_date_arg,
        default=None,
        help="Inclusive report-date start. Defaults to min raw_game_logs GAME_DATE.",
    )
    parser.add_argument(
        "--end-date",
        type=_date_arg,
        default=_date_arg(_env("NBA_INJURY_REPORT_END_DATE"))
        or pd.Timestamp.now(tz="America/New_York").date(),
    )
    parser.add_argument(
        "--report-times",
        default=_env(
            "NBA_INJURY_REPORT_TIMES_ET", pipeline.DEFAULT_INJURY_REPORT_TIME_ET
        ),
        help="Comma-separated official report times, for example 05_00PM.",
    )
    parser.add_argument(
        "--max-reports",
        type=int,
        default=0,
        help="Maximum reports to fetch. Use 0 for no cap.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=_float_env("NBA_INJURY_REPORT_DELAY_SECONDS", 0.25),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=_float_env("NBA_API_TIMEOUT_SECONDS", pipeline.NBA_API_TIMEOUT_SECONDS),
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=_int_env("NBA_API_RETRIES", pipeline.NBA_API_RETRIES),
    )
    parser.add_argument(
        "--retry-base-delay",
        type=float,
        default=_float_env(
            "NBA_API_RETRY_BASE_DELAY_SECONDS",
            pipeline.NBA_API_RETRY_BASE_DELAY_SECONDS,
        ),
    )
    parser.add_argument(
        "--retry-backoff-multiplier",
        type=float,
        default=_float_env(
            "NBA_API_RETRY_BACKOFF_MULTIPLIER",
            pipeline.NBA_API_RETRY_BACKOFF_MULTIPLIER,
        ),
    )
    parser.add_argument(
        "--retry-max-delay",
        type=float,
        default=_float_env(
            "NBA_API_RETRY_MAX_DELAY_SECONDS",
            pipeline.NBA_API_RETRY_MAX_DELAY_SECONDS,
        ),
    )
    parser.add_argument("--bronze-dataset", default=_env("BQ_DATASET_BRONZE", "nba_bronze"))
    parser.add_argument("--silver-dataset", default=_env("BQ_DATASET_SILVER", "nba_silver"))
    parser.add_argument("--gold-dataset", default=_env("BQ_DATASET_GOLD", "nba_gold"))
    parser.add_argument(
        "--metadata-dataset", default=_env("BQ_METADATA_DATASET", "nba_metadata")
    )
    parser.add_argument("--location", default=_env("BQ_LOCATION", "US"))
    parser.add_argument("--dbt-target", default=_env("DBT_TARGET", "dev"))
    parser.add_argument(
        "--dbt-bin",
        default=str(ROOT / ".venv-airflow" / "bin" / "dbt"),
    )
    parser.add_argument("--skip-dbt", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.project_id:
        raise ValueError("Set BQ_PROJECT or pass --project-id")
    if not args.bucket:
        raise ValueError("Set GCS_BUCKET_NAME or pass --bucket")

    started_at = pd.Timestamp.now(tz="UTC")
    client = bigquery.Client(project=args.project_id)
    pipeline.ensure_dataset(
        client, f"{args.project_id}.{args.bronze_dataset}", args.location
    )
    pipeline.ensure_dataset(
        client, f"{args.project_id}.{args.metadata_dataset}", args.location
    )
    state_table = f"{args.project_id}.{args.metadata_dataset}.ingestion_state"
    run_table = f"{args.project_id}.{args.metadata_dataset}.pipeline_run_log"
    pipeline.create_metadata_tables(client, state_table, run_table)

    start_date = args.start_date or _default_start_date(
        client,
        project_id=args.project_id,
        bronze_dataset=args.bronze_dataset,
        season=args.season,
    )
    end_date = args.end_date
    report_times = [value.strip() for value in args.report_times.split(",") if value.strip()]
    candidates = pipeline.build_injury_report_candidates(
        start_date=start_date,
        end_date=end_date,
        report_times_et=report_times,
        max_reports=args.max_reports,
    )
    print(
        "Fetching injury reports "
        f"season={args.season} start={start_date} end={end_date} "
        f"times={','.join(report_times)} candidates={len(candidates)}"
    )

    state = pipeline.get_ingestion_state(
        client,
        state_table,
        source_system=pipeline.INJURY_REPORT_SOURCE_SYSTEM,
        season=args.season,
    )
    watermark_before = state["watermark_date"]
    injury_df = pipeline.get_all_official_injury_reports(
        candidates,
        season=args.season,
        delay=args.delay,
        timeout=args.timeout,
        retries=args.retries,
        retry_base_delay=args.retry_base_delay,
        retry_backoff_multiplier=args.retry_backoff_multiplier,
        retry_max_delay=args.retry_max_delay,
    )
    if injury_df.empty:
        print("No injury report rows returned; nothing loaded.")
        return 0

    validation = source_contracts.validate_source_contract("injury_reports", injury_df)
    valid_df = validation.frame
    if valid_df.empty:
        raise RuntimeError("Source contract validation left no valid injury rows")
    if not validation.quarantine_frame.empty:
        print(f"Quarantined {len(validation.quarantine_frame)} injury rows")

    gcs_uri, staging_table = _upload_and_load_staging(
        client,
        valid_df,
        project_id=args.project_id,
        bucket_name=args.bucket,
        bronze_dataset=args.bronze_dataset,
        season=args.season,
        run_id=uuid.uuid4().hex,
    )
    dq = pipeline.run_injury_report_quality_checks(
        client, staging_table, season=args.season
    )
    raw_table = f"{args.project_id}.{args.bronze_dataset}.raw_player_injury_reports"
    merge_stats = pipeline.create_and_merge_injury_report_table(
        client, staging_table, raw_table
    )
    watermark_after = pipeline.coerce_to_date(valid_df["REPORT_DATE"].max())
    pipeline.upsert_ingestion_state(
        client,
        state_table,
        season=args.season,
        watermark_date=watermark_after,
        source_system=pipeline.INJURY_REPORT_SOURCE_SYSTEM,
    )
    _run_dbt_injury_build(args, project_id=args.project_id)

    finished_at = pd.Timestamp.now(tz="UTC")
    details = (
        f"injury_backfill=true;candidate_count={len(candidates)};"
        f"dq_total_rows={int(dq['total_rows'])};"
        f"rows_unchanged={len(valid_df) - merge_stats['inserted'] - merge_stats['updated']}"
    )
    record = pipeline.build_run_metadata_record(
        dag_run_id="manual_injury_backfill",
        season=args.season,
        status="success",
        source_system=pipeline.INJURY_REPORT_SOURCE_SYSTEM,
        gcs_uri=gcs_uri,
        rows_extracted=len(valid_df),
        rows_loaded=len(valid_df),
        rows_inserted=merge_stats["inserted"],
        rows_updated=merge_stats["updated"],
        watermark_before=watermark_before,
        watermark_after=watermark_after,
        started_at_utc=started_at,
        finished_at_utc=finished_at,
        details=details,
    )
    pipeline.record_pipeline_run(client, run_table, record)
    print(
        "Backfill complete: "
        f"loaded={len(valid_df)} inserted={merge_stats['inserted']} "
        f"updated={merge_stats['updated']} post_count={merge_stats['post_count']} "
        f"watermark_after={watermark_after}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
