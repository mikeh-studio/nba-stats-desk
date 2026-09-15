"""NBA Analytics Pipeline DAG for self-hosted Airflow.

Target shape:
    extract_incremental -> load_staging -> dq_gate -> merge_raw
        -> dbt_build -> best-effort assets -> publish_run_metrics

Post-dbt similarity assets catch data-path failures and annotate
run metadata instead of blocking the core refresh watermark.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from airflow.decorators import dag, task
from airflow.models import Variable

from dbt_builds import run_dbt_build
from nba_pipeline_triage import (
    write_pipeline_triage_on_failure,
    write_pipeline_triage_on_success,
)
from optional_assets import optional_injury_stage
from redshift_tasks import add_redshift_branch
from run_results import combine_results, format_run_details
from source_landing import (
    _safe_storage_token,
    land_source_frame,
    skipped_source_contract_result,
)
from warehouse_stages import check_staging, load_staging, merge_staging

logger = logging.getLogger("nba_pipeline")
SUPPORTED_SEASON = "2025-26"
SCHEDULE_END_DATE = datetime(2026, 7, 1)


def get_config(key: str, default: str | None = None) -> str | None:
    """Read from Airflow Variables first, fall back to env var, then default."""
    try:
        value = Variable.get(key)
    except Exception:
        value = os.getenv(key, default)
    if isinstance(value, str):
        return value.strip().strip("\"'")
    return value


def get_project_id() -> str:
    pid = get_config("BQ_PROJECT", get_config("GCP_PROJECT_ID"))
    if not pid:
        raise ValueError("BQ_PROJECT or GCP_PROJECT_ID must be configured")
    return pid


def get_dataset(dataset_key: str, default_name: str) -> str:
    return get_config(dataset_key, get_config("BQ_DATASET", default_name))


def get_int_config(key: str, default: str) -> int:
    value = get_config(key, default)
    try:
        return int(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be an integer, got {value!r}") from exc


def get_float_config(key: str, default: str) -> float:
    value = get_config(key, default)
    try:
        return float(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be a number, got {value!r}") from exc


def get_bool_config(key: str, default: str = "false") -> bool:
    value = str(get_config(key, default) or "").strip().lower()
    return value in {"1", "true", "yes", "y", "on"}


def get_nba_api_request_config() -> dict:
    return {
        "timeout": get_float_config("NBA_API_TIMEOUT_SECONDS", "15"),
        "retries": get_int_config("NBA_API_RETRIES", "3"),
        "retry_base_delay": get_float_config("NBA_API_RETRY_BASE_DELAY_SECONDS", "1.0"),
        "retry_backoff_multiplier": get_float_config(
            "NBA_API_RETRY_BACKOFF_MULTIPLIER", "2.0"
        ),
        "retry_max_delay": get_float_config("NBA_API_RETRY_MAX_DELAY_SECONDS", "8.0"),
    }


def get_dbt_repo_root() -> Path:
    """Resolve the dbt project root in local Airflow-friendly layouts."""
    dag_file = Path(__file__).resolve()
    candidates = [dag_file.parents[1], dag_file.parent]

    for candidate in candidates:
        if (candidate / "dbt_project.yml").exists() and (
            candidate / "dbt" / "profiles"
        ).exists():
            return candidate

    raise FileNotFoundError(
        "Could not find dbt_project.yml and dbt/profiles alongside the DAG. "
        "Checked: " + ", ".join(str(path) for path in candidates)
    )


default_args = {
    "owner": "nba-analytics",
    "depends_on_past": False,
    "retries": 1,
    "retry_delay": timedelta(minutes=2),
    "execution_timeout": timedelta(minutes=45),
    "on_failure_callback": write_pipeline_triage_on_failure,
}


@dag(
    dag_id="nba_analytics_pipeline",
    description="Incremental NBA 2025-26 player stats pipeline with BigQuery + dbt",
    schedule="0 11 * * *",
    start_date=datetime(2025, 1, 1),
    end_date=SCHEDULE_END_DATE,
    catchup=False,
    max_active_runs=1,
    dagrun_timeout=timedelta(hours=2),
    default_args=default_args,
    on_success_callback=write_pipeline_triage_on_success,
    tags=["nba", "airflow", "bigquery", "dbt", "self-hosted"],
)
def nba_analytics_pipeline():
    @task(
        retries=2,
        retry_delay=timedelta(minutes=5),
        execution_timeout=timedelta(minutes=45),
    )
    def extract_incremental() -> dict:
        """Fetch player game logs, apply replay-window filtering, and land a CSV in GCS."""
        import pandas as pd
        from google.cloud import bigquery as bq

        import nba_pipeline as pipeline

        season = SUPPORTED_SEASON
        replay_days = int(get_config("NBA_REPLAY_DAYS", "3"))
        max_players = int(get_config("NBA_MAX_PLAYERS", "0"))
        season_types = pipeline.normalize_game_log_season_types(
            get_config("NBA_GAME_LOG_SEASON_TYPES", "Regular Season,Playoffs")
        )
        game_log_extract_mode = get_config("NBA_GAME_LOG_EXTRACT_MODE", "league")
        project_id = get_project_id()
        bucket_name = get_config("GCS_BUCKET_NAME")
        location = get_config("BQ_LOCATION", "US")
        bronze_dataset = get_dataset("BQ_DATASET_BRONZE", "nba_bronze")
        metadata_dataset = get_dataset("BQ_METADATA_DATASET", "nba_metadata")

        client = bq.Client(project=project_id)
        pipeline.ensure_dataset(client, f"{project_id}.{bronze_dataset}", location)
        pipeline.ensure_dataset(client, f"{project_id}.{metadata_dataset}", location)

        state_table = f"{project_id}.{metadata_dataset}.ingestion_state"
        run_table = f"{project_id}.{metadata_dataset}.pipeline_run_log"
        pipeline.create_metadata_tables(client, state_table, run_table)
        state = pipeline.get_ingestion_state(client, state_table, season=season)
        replay_start = pipeline.compute_replay_start(
            state["watermark_date"], replay_days=replay_days
        )
        cdn_fallback_enabled = get_config(
            "NBA_GAME_LOG_CDN_FALLBACK_ENABLED", "true"
        ).strip().lower() in {"1", "true", "t", "yes", "y", "on"}

        active = pipeline.get_active_players()
        selected = active if max_players <= 0 else active[:max_players]
        logger.info("Processing %s players for season %s", len(selected), season)

        df = pipeline.get_all_player_game_logs(
            selected,
            season=season,
            season_types=season_types,
            extract_mode=game_log_extract_mode,
            start_date=replay_start,
            allow_cdn_fallback=cdn_fallback_enabled,
            **get_nba_api_request_config(),
        )
        incremental_df = pipeline.filter_incremental_game_logs(
            df,
            watermark_date=state["watermark_date"],
            replay_days=replay_days,
            season=season,
        )

        if incremental_df.empty:
            logger.info("No rows remain after replay-window filtering")
            return {
                "domain": "game_logs",
                "gcs_uri": "",
                "row_count": 0,
                "game_ids": [],
                "season": season,
                "source_contract": skipped_source_contract_result(
                    "game_logs",
                    "empty_after_incremental_filter",
                    project_id=project_id,
                    metadata_dataset=metadata_dataset,
                    location=location,
                ),
                "watermark_before": state["watermark_date"].isoformat()
                if state["watermark_date"]
                else None,
                "watermark_after": state["watermark_date"].isoformat()
                if state["watermark_date"]
                else None,
            }

        def build_blob_path(incremental_df):
            run_stamp = pd.Timestamp.now(tz="UTC").strftime("%Y%m%dT%H%M%SZ")
            min_date = incremental_df["GAME_DATE"].min().strftime("%Y%m%d")
            max_date = incremental_df["GAME_DATE"].max().strftime("%Y%m%d")
            blob_path = f"nba_data/{season}/landing/{run_stamp}_{min_date}_{max_date}_game_logs.csv"
            return blob_path

        incremental_df, source_contract, gcs_uri = land_source_frame(
            "game_logs",
            incremental_df,
            project_id=project_id,
            metadata_dataset=metadata_dataset,
            location=location,
            bucket_name=bucket_name,
            season=season,
            build_blob_path=build_blob_path,
        )
        watermark_after = pipeline.coerce_to_date(incremental_df["GAME_DATE"].max())

        return {
            "domain": "game_logs",
            "gcs_uri": gcs_uri,
            "row_count": len(incremental_df),
            "game_ids": sorted(
                {
                    str(game_id)
                    for game_id in incremental_df["GAME_ID"]
                    .dropna()
                    .astype(str)
                    .tolist()
                    if game_id
                }
            ),
            "season": season,
            "source_contract": source_contract,
            "watermark_before": state["watermark_date"].isoformat()
            if state["watermark_date"]
            else None,
            "watermark_after": watermark_after.isoformat() if watermark_after else None,
        }

    @task(retries=2, retry_delay=timedelta(minutes=5))
    def extract_game_line_scores(game_log_result: dict) -> dict:
        """Fetch team line scores for the incrementally changed game set."""
        import pandas as pd

        import nba_pipeline as pipeline

        season = game_log_result["season"]
        game_ids = game_log_result.get("game_ids", [])
        project_id = get_project_id()
        bucket_name = get_config("GCS_BUCKET_NAME")
        location = get_config("BQ_LOCATION", "US")
        metadata_dataset = get_dataset("BQ_METADATA_DATASET", "nba_metadata")

        if not game_ids:
            return {
                "domain": "game_line_scores",
                "gcs_uri": "",
                "row_count": 0,
                "season": season,
                "source_contract": skipped_source_contract_result(
                    "game_line_scores",
                    "empty_changed_game_set",
                    project_id=project_id,
                    metadata_dataset=metadata_dataset,
                    location=location,
                ),
            }

        line_scores = pipeline.get_all_game_line_scores(
            game_ids,
            season=season,
            **get_nba_api_request_config(),
        )
        if line_scores.empty:
            logger.info("No line score rows returned for candidate game_ids")
            return {
                "domain": "game_line_scores",
                "gcs_uri": "",
                "row_count": 0,
                "season": season,
                "source_contract": skipped_source_contract_result(
                    "game_line_scores",
                    "empty_source_response",
                    project_id=project_id,
                    metadata_dataset=metadata_dataset,
                    location=location,
                ),
            }

        def build_blob_path(line_scores):
            run_stamp = pd.Timestamp.now(tz="UTC").strftime("%Y%m%dT%H%M%SZ")
            min_date = pd.to_datetime(line_scores["GAME_DATE"]).min().strftime("%Y%m%d")
            max_date = pd.to_datetime(line_scores["GAME_DATE"]).max().strftime("%Y%m%d")
            blob_path = f"nba_data/{season}/landing/{run_stamp}_{min_date}_{max_date}_game_line_scores.csv"
            return blob_path

        line_scores, source_contract, gcs_uri = land_source_frame(
            "game_line_scores",
            line_scores,
            project_id=project_id,
            metadata_dataset=metadata_dataset,
            location=location,
            bucket_name=bucket_name,
            season=season,
            build_blob_path=build_blob_path,
        )
        return {
            "domain": "game_line_scores",
            "gcs_uri": gcs_uri,
            "row_count": len(line_scores),
            "season": season,
            "source_contract": source_contract,
        }

    @task(retries=2, retry_delay=timedelta(minutes=5))
    def extract_player_shot_locations() -> dict:
        """Fetch aggregate player shot-location profiles for the season."""
        import pandas as pd

        import nba_pipeline as pipeline

        season = SUPPORTED_SEASON
        season_type = get_config(
            "NBA_SHOT_LOCATION_SEASON_TYPE",
            pipeline.DEFAULT_SHOT_LOCATION_SEASON_TYPE,
        )
        project_id = get_project_id()
        bucket_name = get_config("GCS_BUCKET_NAME")
        location = get_config("BQ_LOCATION", "US")
        metadata_dataset = get_dataset("BQ_METADATA_DATASET", "nba_metadata")

        shot_locations = pipeline.get_player_shot_locations(
            season=season,
            season_type=season_type,
            **get_nba_api_request_config(),
        )
        if shot_locations.empty:
            logger.info("No player shot-location rows returned")
            return {
                "domain": "player_shot_locations",
                "gcs_uri": "",
                "row_count": 0,
                "season": season,
                "source_contract": skipped_source_contract_result(
                    "player_shot_locations",
                    "empty_source_response",
                    project_id=project_id,
                    metadata_dataset=metadata_dataset,
                    location=location,
                ),
            }

        def build_blob_path(shot_locations):
            run_stamp = pd.Timestamp.now(tz="UTC").strftime("%Y%m%dT%H%M%SZ")
            safe_season_type = _safe_storage_token(str(season_type).lower())
            blob_path = (
                f"nba_data/{season}/landing/"
                f"{run_stamp}_{safe_season_type}_player_shot_locations.csv"
            )
            return blob_path

        shot_locations, source_contract, gcs_uri = land_source_frame(
            "player_shot_locations",
            shot_locations,
            project_id=project_id,
            metadata_dataset=metadata_dataset,
            location=location,
            bucket_name=bucket_name,
            season=season,
            build_blob_path=build_blob_path,
        )
        return {
            "domain": "player_shot_locations",
            "gcs_uri": gcs_uri,
            "row_count": len(shot_locations),
            "season": season,
            "source_contract": source_contract,
        }

    @task(retries=2, retry_delay=timedelta(minutes=5))
    def extract_player_reference() -> dict:
        """Fetch active-player reference attributes and roster context."""
        import pandas as pd

        import nba_pipeline as pipeline

        project_id = get_project_id()
        bucket_name = get_config("GCS_BUCKET_NAME")
        location = get_config("BQ_LOCATION", "US")
        metadata_dataset = get_dataset("BQ_METADATA_DATASET", "nba_metadata")
        max_players = int(get_config("NBA_MAX_PLAYERS", "0"))
        max_empty_profiles = get_int_config(
            "NBA_PLAYER_REFERENCE_MAX_EMPTY_PROFILES", "3"
        )

        active = pipeline.get_active_players()
        selected = active if max_players <= 0 else active[:max_players]
        reference_df = pipeline.get_all_player_references(
            selected,
            max_empty_profiles=max_empty_profiles,
            **get_nba_api_request_config(),
        )
        if reference_df.empty:
            logger.info("No player reference rows returned for active players")
            return {
                "domain": "player_reference",
                "gcs_uri": "",
                "row_count": 0,
                "source_contract": skipped_source_contract_result(
                    "player_reference",
                    "empty_source_response",
                    project_id=project_id,
                    metadata_dataset=metadata_dataset,
                    location=location,
                ),
            }

        def build_blob_path(reference_df):
            run_stamp = pd.Timestamp.now(tz="UTC").strftime("%Y%m%dT%H%M%SZ")
            blob_path = f"nba_data/reference/landing/{run_stamp}_player_reference.csv"
            return blob_path

        reference_df, source_contract, gcs_uri = land_source_frame(
            "player_reference",
            reference_df,
            project_id=project_id,
            metadata_dataset=metadata_dataset,
            location=location,
            bucket_name=bucket_name,
            season=SUPPORTED_SEASON,
            build_blob_path=build_blob_path,
        )
        return {
            "domain": "player_reference",
            "gcs_uri": gcs_uri,
            "row_count": len(reference_df),
            "source_contract": source_contract,
        }

    @task(retries=2, retry_delay=timedelta(minutes=5))
    def extract_schedule_context() -> dict:
        """Fetch the upcoming schedule window and land a CSV in GCS."""
        import pandas as pd

        import nba_pipeline as pipeline

        season = SUPPORTED_SEASON
        horizon_days = int(get_config("NBA_SCHEDULE_LOOKAHEAD_DAYS", "7"))
        project_id = get_project_id()
        bucket_name = get_config("GCS_BUCKET_NAME")
        location = get_config("BQ_LOCATION", "US")
        metadata_dataset = get_dataset("BQ_METADATA_DATASET", "nba_metadata")
        schedule_df = pipeline.get_upcoming_schedule(
            season=season,
            horizon_days=horizon_days,
            **get_nba_api_request_config(),
        )
        if schedule_df.empty:
            logger.info(
                "No schedule rows available for the configured lookahead window"
            )
            return {
                "domain": "schedule",
                "gcs_uri": "",
                "row_count": 0,
                "season": season,
                "source_contract": skipped_source_contract_result(
                    "schedule",
                    "empty_lookahead_window",
                    project_id=project_id,
                    metadata_dataset=metadata_dataset,
                    location=location,
                ),
            }

        def build_blob_path(schedule_df):
            run_stamp = pd.Timestamp.now(tz="UTC").strftime("%Y%m%dT%H%M%SZ")
            min_date = schedule_df["SCHEDULE_DATE"].min().strftime("%Y%m%d")
            max_date = schedule_df["SCHEDULE_DATE"].max().strftime("%Y%m%d")
            blob_path = f"nba_data/{season}/landing/{run_stamp}_{min_date}_{max_date}_schedule.csv"
            return blob_path

        schedule_df, source_contract, gcs_uri = land_source_frame(
            "schedule",
            schedule_df,
            project_id=project_id,
            metadata_dataset=metadata_dataset,
            location=location,
            bucket_name=bucket_name,
            season=season,
            build_blob_path=build_blob_path,
        )
        return {
            "domain": "schedule",
            "gcs_uri": gcs_uri,
            "row_count": len(schedule_df),
            "season": season,
            "source_contract": source_contract,
        }

    @task(retries=2, retry_delay=timedelta(minutes=5))
    @optional_injury_stage
    def extract_injury_reports() -> dict:
        """Fetch bounded official NBA injury report snapshots and land a CSV."""
        import pandas as pd
        from google.cloud import bigquery as bq

        import nba_pipeline as pipeline

        season = SUPPORTED_SEASON
        project_id = get_project_id()
        bucket_name = get_config("GCS_BUCKET_NAME")
        location = get_config("BQ_LOCATION", "US")
        bronze_dataset = get_dataset("BQ_DATASET_BRONZE", "nba_bronze")
        metadata_dataset = get_dataset("BQ_METADATA_DATASET", "nba_metadata")
        max_reports = get_int_config("NBA_INJURY_REPORT_MAX_REPORTS", "21")
        replay_days = get_int_config("NBA_INJURY_REPORT_REPLAY_DAYS", "2")

        client = bq.Client(project=project_id)
        pipeline.ensure_dataset(client, f"{project_id}.{bronze_dataset}", location)
        pipeline.ensure_dataset(client, f"{project_id}.{metadata_dataset}", location)
        state_table = f"{project_id}.{metadata_dataset}.ingestion_state"
        run_table = f"{project_id}.{metadata_dataset}.pipeline_run_log"
        pipeline.create_metadata_tables(client, state_table, run_table)
        state = pipeline.get_ingestion_state(
            client,
            state_table,
            source_system=pipeline.INJURY_REPORT_SOURCE_SYSTEM,
            season=season,
        )

        watermark_before = (
            state["watermark_date"].isoformat() if state["watermark_date"] else None
        )
        if not get_bool_config("NBA_ENABLE_INJURY_REPORTS", "true"):
            return {
                "domain": "injury_reports",
                "gcs_uri": "",
                "row_count": 0,
                "candidate_count": 0,
                "season": season,
                "source_contract": skipped_source_contract_result(
                    "injury_reports",
                    "disabled",
                    project_id=project_id,
                    metadata_dataset=metadata_dataset,
                    location=location,
                ),
                "watermark_before": watermark_before,
                "watermark_after": watermark_before,
            }

        end_date = pipeline.coerce_to_date(get_config("NBA_INJURY_REPORT_END_DATE"))
        if end_date is None:
            end_date = pd.Timestamp.now(tz="America/New_York").date()

        if state["watermark_date"]:
            start_date = pipeline.compute_replay_start(
                state["watermark_date"], replay_days=replay_days
            )
        else:
            start_date = pipeline.coerce_to_date(
                get_config("NBA_INJURY_REPORT_START_DATE")
            )
            if start_date is None:
                rolling_start = end_date - timedelta(days=max(max_reports, 1) - 1)
                start_date = max(pipeline.DEFAULT_PLAYOFF_BACKFILL_START, rolling_start)

        report_times = [
            value.strip()
            for value in str(
                get_config(
                    "NBA_INJURY_REPORT_TIMES_ET",
                    pipeline.DEFAULT_INJURY_REPORT_TIME_ET,
                )
            ).split(",")
            if value.strip()
        ]
        candidates = pipeline.build_injury_report_candidates(
            start_date=start_date,
            end_date=end_date,
            report_times_et=report_times,
            max_reports=max_reports,
        )
        if not candidates:
            return {
                "domain": "injury_reports",
                "gcs_uri": "",
                "row_count": 0,
                "candidate_count": 0,
                "season": season,
                "source_contract": skipped_source_contract_result(
                    "injury_reports",
                    "no_candidates",
                    project_id=project_id,
                    metadata_dataset=metadata_dataset,
                    location=location,
                ),
                "watermark_before": watermark_before,
                "watermark_after": watermark_before,
            }

        injury_df = pipeline.get_all_official_injury_reports(
            candidates,
            season=season,
            delay=get_float_config("NBA_INJURY_REPORT_DELAY_SECONDS", "0.25"),
            **get_nba_api_request_config(),
        )
        if injury_df.empty:
            logger.info("No official injury report rows returned for candidates")
            return {
                "domain": "injury_reports",
                "gcs_uri": "",
                "row_count": 0,
                "candidate_count": len(candidates),
                "season": season,
                "source_contract": skipped_source_contract_result(
                    "injury_reports",
                    "empty_source_response",
                    project_id=project_id,
                    metadata_dataset=metadata_dataset,
                    location=location,
                ),
                "watermark_before": watermark_before,
                "watermark_after": watermark_before,
            }

        def build_blob_path(injury_df):
            run_stamp = pd.Timestamp.now(tz="UTC").strftime("%Y%m%dT%H%M%SZ")
            min_date = pd.to_datetime(injury_df["REPORT_DATE"]).min().strftime("%Y%m%d")
            max_date = pd.to_datetime(injury_df["REPORT_DATE"]).max().strftime("%Y%m%d")
            blob_path = (
                f"nba_data/{season}/landing/"
                f"{run_stamp}_{min_date}_{max_date}_injury_reports.csv"
            )
            return blob_path

        injury_df, source_contract, gcs_uri = land_source_frame(
            "injury_reports",
            injury_df,
            project_id=project_id,
            metadata_dataset=metadata_dataset,
            location=location,
            bucket_name=bucket_name,
            season=season,
            build_blob_path=build_blob_path,
        )
        watermark_after = pipeline.coerce_to_date(injury_df["REPORT_DATE"].max())
        return {
            "domain": "injury_reports",
            "gcs_uri": gcs_uri,
            "row_count": len(injury_df),
            "candidate_count": len(candidates),
            "season": season,
            "source_contract": source_contract,
            "watermark_before": watermark_before,
            "watermark_after": watermark_after.isoformat()
            if watermark_after
            else watermark_before,
        }

    @task(retries=2, retry_delay=timedelta(minutes=2))
    def load_game_log_staging(extract_result: dict) -> dict:
        """Load landed game log rows to staging."""
        return load_staging(
            extract_result,
            domain="game_logs",
            project_id=get_project_id(),
            bronze_dataset=get_dataset("BQ_DATASET_BRONZE", "nba_bronze"),
            location=get_config("BQ_LOCATION", "US"),
        )

    @task(retries=2, retry_delay=timedelta(minutes=2))
    def load_schedule_staging(extract_result: dict) -> dict:
        """Load landed schedule rows to staging."""
        return load_staging(
            extract_result,
            domain="schedule",
            project_id=get_project_id(),
            bronze_dataset=get_dataset("BQ_DATASET_BRONZE", "nba_bronze"),
            location=get_config("BQ_LOCATION", "US"),
        )

    @task(retries=2, retry_delay=timedelta(minutes=2))
    def load_game_line_score_staging(extract_result: dict) -> dict:
        """Load landed game line score rows to staging."""
        return load_staging(
            extract_result,
            domain="game_line_scores",
            project_id=get_project_id(),
            bronze_dataset=get_dataset("BQ_DATASET_BRONZE", "nba_bronze"),
            location=get_config("BQ_LOCATION", "US"),
        )

    @task(retries=2, retry_delay=timedelta(minutes=2))
    def load_player_shot_location_staging(extract_result: dict) -> dict:
        """Load landed aggregate player shot-location rows to staging."""
        return load_staging(
            extract_result,
            domain="player_shot_locations",
            project_id=get_project_id(),
            bronze_dataset=get_dataset("BQ_DATASET_BRONZE", "nba_bronze"),
            location=get_config("BQ_LOCATION", "US"),
        )

    @task(retries=2, retry_delay=timedelta(minutes=2))
    def load_player_reference_staging(extract_result: dict) -> dict:
        """Load landed player reference rows to staging."""
        return load_staging(
            extract_result,
            domain="player_reference",
            project_id=get_project_id(),
            bronze_dataset=get_dataset("BQ_DATASET_BRONZE", "nba_bronze"),
            location=get_config("BQ_LOCATION", "US"),
        )

    @task(retries=2, retry_delay=timedelta(minutes=2))
    @optional_injury_stage
    def load_injury_report_staging(extract_result: dict) -> dict:
        """Load landed official injury report rows to staging."""
        return load_staging(
            extract_result,
            domain="injury_reports",
            project_id=get_project_id(),
            bronze_dataset=get_dataset("BQ_DATASET_BRONZE", "nba_bronze"),
            location=get_config("BQ_LOCATION", "US"),
        )

    @task(retries=0)
    def dq_game_log_staging(load_result: dict) -> dict:
        """Run hard DQ checks for game logs unless the run is a no-op."""
        return check_staging(
            load_result,
            domain="game_logs",
            resolve_project_id=get_project_id,
            season=SUPPORTED_SEASON,
        )

    @task(retries=0)
    def dq_schedule_staging(load_result: dict) -> dict:
        """Run DQ checks for upcoming schedule rows."""
        return check_staging(
            load_result,
            domain="schedule",
            resolve_project_id=get_project_id,
            season=SUPPORTED_SEASON,
        )

    @task(retries=0)
    def dq_game_line_score_staging(load_result: dict) -> dict:
        """Run DQ checks for game line score rows."""
        return check_staging(
            load_result,
            domain="game_line_scores",
            resolve_project_id=get_project_id,
            season=SUPPORTED_SEASON,
        )

    @task(retries=0)
    def dq_player_shot_location_staging(load_result: dict) -> dict:
        """Run DQ checks for aggregate player shot-location rows."""
        return check_staging(
            load_result,
            domain="player_shot_locations",
            resolve_project_id=get_project_id,
            season=SUPPORTED_SEASON,
        )

    @task(retries=0)
    def dq_player_reference_staging(load_result: dict) -> dict:
        """Run DQ checks for player reference rows."""
        return check_staging(
            load_result,
            domain="player_reference",
            resolve_project_id=get_project_id,
            season=SUPPORTED_SEASON,
        )

    @task(retries=0)
    @optional_injury_stage
    def dq_injury_report_staging(load_result: dict) -> dict:
        """Run DQ checks for official injury report rows."""
        return check_staging(
            load_result,
            domain="injury_reports",
            resolve_project_id=get_project_id,
            season=SUPPORTED_SEASON,
        )

    @task(retries=1, retry_delay=timedelta(minutes=2))
    def merge_game_logs(load_result: dict) -> dict:
        """Merge staged game log rows into the bronze raw table."""
        return merge_staging(
            load_result,
            domain="game_logs",
            project_id=get_project_id(),
            bronze_dataset=get_dataset("BQ_DATASET_BRONZE", "nba_bronze"),
        )

    @task(retries=1, retry_delay=timedelta(minutes=2))
    def merge_schedule_context(load_result: dict) -> dict:
        """Merge staged schedule rows into the bronze raw table."""
        return merge_staging(
            load_result,
            domain="schedule",
            project_id=get_project_id(),
            bronze_dataset=get_dataset("BQ_DATASET_BRONZE", "nba_bronze"),
        )

    @task(retries=1, retry_delay=timedelta(minutes=2))
    def merge_game_line_scores(load_result: dict) -> dict:
        """Merge staged game line score rows into the bronze raw table."""
        return merge_staging(
            load_result,
            domain="game_line_scores",
            project_id=get_project_id(),
            bronze_dataset=get_dataset("BQ_DATASET_BRONZE", "nba_bronze"),
        )

    @task(retries=1, retry_delay=timedelta(minutes=2))
    def merge_player_shot_locations(load_result: dict) -> dict:
        """Merge staged aggregate player shot locations into the bronze raw table."""
        return merge_staging(
            load_result,
            domain="player_shot_locations",
            project_id=get_project_id(),
            bronze_dataset=get_dataset("BQ_DATASET_BRONZE", "nba_bronze"),
        )

    @task(retries=1, retry_delay=timedelta(minutes=2))
    def merge_player_reference(load_result: dict) -> dict:
        """Merge staged player reference rows into the bronze raw table."""
        return merge_staging(
            load_result,
            domain="player_reference",
            project_id=get_project_id(),
            bronze_dataset=get_dataset("BQ_DATASET_BRONZE", "nba_bronze"),
        )

    @task(retries=1, retry_delay=timedelta(minutes=2))
    @optional_injury_stage
    def merge_injury_reports(load_result: dict) -> dict:
        """Merge staged official injury report rows into the bronze raw table."""
        return merge_staging(
            load_result,
            domain="injury_reports",
            project_id=get_project_id(),
            bronze_dataset=get_dataset("BQ_DATASET_BRONZE", "nba_bronze"),
        )

    @task(retries=0)
    def combine_pipeline_results(
        game_result: dict,
        schedule_result: dict,
        line_score_result: dict,
        shot_location_result: dict,
        player_reference_result: dict,
        injury_report_result: dict,
        bootstrap_result: dict | None = None,
    ) -> dict:
        """Combine per-domain results into a single warehouse build context."""
        return combine_results(
            game_result,
            schedule_result,
            line_score_result,
            shot_location_result,
            player_reference_result,
            injury_report_result,
            bootstrap_result,
        )

    @task(retries=1, retry_delay=timedelta(minutes=2))
    def bootstrap_bronze_contract(
        game_result: dict,
        schedule_result: dict,
        line_score_result: dict,
        player_reference_result: dict,
    ) -> dict:
        """Derive missing auxiliary bronze tables from raw game logs when needed."""
        from google.cloud import bigquery as bq

        import nba_pipeline as pipeline

        project_id = get_project_id()
        bronze_dataset = get_dataset("BQ_DATASET_BRONZE", "nba_bronze")
        mode = get_config("NBA_BRONZE_BOOTSTRAP_MODE", "auto")
        client = bq.Client(project=project_id)
        result = pipeline.run_bronze_contract_bootstrap(
            client,
            project_id=project_id,
            bronze_dataset=bronze_dataset,
            season=SUPPORTED_SEASON,
            mode=mode,
        )
        logger.info(
            "Bronze bootstrap result: %s",
            {
                domain: {
                    "ran": details.get("ran"),
                    "rows_loaded": details.get("rows_loaded"),
                    "rows_inserted": details.get("rows_inserted"),
                    "rows_updated": details.get("rows_updated"),
                    "reason": details.get("reason"),
                }
                for domain, details in result.get("domains", {}).items()
            },
        )
        return result

    @task(retries=1, retry_delay=timedelta(minutes=2))
    def dbt_build(merge_result: dict) -> dict:
        """Run dbt models and tests after the bronze merges."""
        return run_dbt_build(
            merge_result,
            get_config=get_config,
            get_project_id=get_project_id,
            get_dataset=get_dataset,
            get_dbt_repo_root=get_dbt_repo_root,
            season=SUPPORTED_SEASON,
            new_candidate_id=uuid.uuid4,
        )

    @task(retries=1, retry_delay=timedelta(minutes=2))
    def build_player_similarity_assets(merge_result: dict) -> dict:
        """Train archetype clusters and publish normalized similarity vectors."""
        from google.cloud import bigquery as bq

        import nba_pipeline as pipeline

        merge_result["similarity_status"] = "skipped"
        merge_result["similarity_player_count"] = 0
        merge_result["similarity_archetype_count"] = 0

        if (
            not merge_result["should_build"]
            or merge_result.get("dbt_status") != "success"
        ):
            logger.info(
                "Skipping player similarity publish because dbt did not complete"
            )
            return merge_result

        try:
            project_id = get_project_id()
            gold_dataset = get_dataset("BQ_DATASET_GOLD", "nba_gold")
            location = get_config("BQ_LOCATION", "US")
            cluster_count = int(get_config("NBA_ARCHETYPE_CLUSTERS", "10"))
            client = bq.Client(project=project_id)
            pipeline.ensure_dataset(client, f"{project_id}.{gold_dataset}", location)

            feature_input_table = (
                f"{project_id}.{gold_dataset}.player_similarity_feature_input"
            )
            feature_output_table = (
                f"{project_id}.{gold_dataset}.player_similarity_features"
            )
            archetype_table = f"{project_id}.{gold_dataset}.player_archetypes"

            feature_input = client.query(
                f"""
                SELECT *
                FROM `{feature_input_table}`
                WHERE season = @season
                """,
                job_config=bq.QueryJobConfig(
                    query_parameters=[
                        bq.ScalarQueryParameter(
                            "season", "STRING", merge_result["season"]
                        ),
                    ]
                ),
            ).to_dataframe()
            if feature_input.empty:
                logger.info(
                    "No player similarity feature rows were available after dbt build"
                )
                return merge_result

            outputs = pipeline.build_player_similarity_outputs(
                feature_input,
                cluster_count=cluster_count,
            )
            pipeline.write_player_similarity_tables(
                client,
                features_table_id=feature_output_table,
                archetypes_table_id=archetype_table,
                features_df=outputs["features"],
                archetypes_df=outputs["archetypes"],
            )
            merge_result["similarity_status"] = "success"
            merge_result["similarity_player_count"] = len(outputs["features"])
            merge_result["similarity_archetype_count"] = outputs["archetypes"][
                "archetype_label"
            ].nunique()
        except Exception as exc:
            logger.exception(
                "Player similarity publish failed; continuing core refresh"
            )
            merge_result["similarity_status"] = "failed_non_blocking"
            merge_result["similarity_error"] = f"{type(exc).__name__}: {exc}"
        return merge_result

    @task(retries=0)
    def publish_run_metrics(run_result: dict) -> dict:
        """Persist watermark state and run-level metadata."""
        from airflow.operators.python import get_current_context
        from google.cloud import bigquery as bq

        import nba_pipeline as pipeline

        project_id = get_project_id()
        metadata_dataset = get_dataset("BQ_METADATA_DATASET", "nba_metadata")
        location = get_config("BQ_LOCATION", "US")
        client = bq.Client(project=project_id)
        pipeline.ensure_dataset(client, f"{project_id}.{metadata_dataset}", location)

        state_table = f"{project_id}.{metadata_dataset}.ingestion_state"
        run_table = f"{project_id}.{metadata_dataset}.pipeline_run_log"
        pipeline.create_metadata_tables(client, state_table, run_table)

        context = get_current_context()
        if run_result["rows_loaded"] > 0 and run_result["watermark_after"]:
            pipeline.upsert_ingestion_state(
                client,
                state_table,
                season=run_result["season"],
                watermark_date=run_result["watermark_after"],
            )
        if (
            run_result.get("injury_report_rows_loaded", 0) > 0
            and run_result.get("injury_status") != "failed_non_blocking"
            and run_result.get("injury_dbt_status") == "success"
            and run_result.get("injury_watermark_after")
        ):
            pipeline.upsert_ingestion_state(
                client,
                state_table,
                season=run_result["season"],
                watermark_date=run_result["injury_watermark_after"],
                source_system=pipeline.INJURY_REPORT_SOURCE_SYSTEM,
            )

        record = pipeline.build_run_metadata_record(
            dag_run_id=context["run_id"],
            season=run_result["season"],
            status="success",
            gcs_uri=run_result["gcs_uri"],
            rows_extracted=(
                run_result["rows_loaded"]
                + run_result.get("schedule_rows_loaded", 0)
                + run_result.get("line_score_rows_loaded", 0)
                + run_result.get("shot_location_rows_loaded", 0)
                + run_result.get("player_reference_rows_loaded", 0)
                + run_result.get("injury_report_rows_loaded", 0)
            ),
            rows_loaded=run_result["rows_loaded"],
            rows_inserted=run_result["rows_inserted"],
            rows_updated=run_result["rows_updated"],
            watermark_before=run_result["watermark_before"],
            watermark_after=run_result["watermark_after"],
            started_at_utc=context["data_interval_start"],
            finished_at_utc=datetime.now(tz=context["data_interval_start"].tzinfo),
            details=format_run_details(run_result, get_config=get_config),
        )
        pipeline.record_pipeline_run(client, run_table, record)
        return run_result

    extracted = extract_incremental()
    extracted_schedule = extract_schedule_context()
    extracted_line_scores = extract_game_line_scores(extracted)
    extracted_shot_locations = extract_player_shot_locations()
    extracted_player_reference = extract_player_reference()
    extracted_injury_reports = extract_injury_reports()

    staged = load_game_log_staging(extracted)
    staged_schedule = load_schedule_staging(extracted_schedule)
    staged_line_scores = load_game_line_score_staging(extracted_line_scores)
    staged_shot_locations = load_player_shot_location_staging(extracted_shot_locations)
    staged_player_reference = load_player_reference_staging(extracted_player_reference)
    staged_injury_reports = load_injury_report_staging(extracted_injury_reports)

    checked = dq_game_log_staging(staged)
    checked_schedule = dq_schedule_staging(staged_schedule)
    checked_line_scores = dq_game_line_score_staging(staged_line_scores)
    checked_shot_locations = dq_player_shot_location_staging(staged_shot_locations)
    checked_player_reference = dq_player_reference_staging(staged_player_reference)
    checked_injury_reports = dq_injury_report_staging(staged_injury_reports)

    merged = merge_game_logs(checked)
    merged_schedule = merge_schedule_context(checked_schedule)
    merged_line_scores = merge_game_line_scores(checked_line_scores)
    merged_shot_locations = merge_player_shot_locations(checked_shot_locations)
    merged_player_reference = merge_player_reference(checked_player_reference)
    merged_injury_reports = merge_injury_reports(checked_injury_reports)

    bootstrapped_bronze = bootstrap_bronze_contract(
        merged,
        merged_schedule,
        merged_line_scores,
        merged_player_reference,
    )

    combined = combine_pipeline_results(
        merged,
        merged_schedule,
        merged_line_scores,
        merged_shot_locations,
        merged_player_reference,
        merged_injury_reports,
        bootstrapped_bronze,
    )

    # Optional Redshift tasks run independently of core publication.
    add_redshift_branch(
        combined,
        get_config=get_config,
        get_project_id=get_project_id,
        get_dataset=get_dataset,
        get_dbt_repo_root=get_dbt_repo_root,
        season=SUPPORTED_SEASON,
    )

    # Asset builders catch and report their own non-critical failures so the
    # run metadata can record their status without turning those failures into
    # core refresh failures.
    modeled = dbt_build(combined)
    similarity_built = build_player_similarity_assets(modeled)
    publish_run_metrics(similarity_built)


nba_analytics_pipeline()
