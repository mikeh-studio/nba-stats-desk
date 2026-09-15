"""Optional Redshift task construction; BigQuery core publication runs independently.

The branch retains its existing failure policy: a Redshift task failure may
fail the overall DAG even when core publication completes successfully.
"""

from __future__ import annotations

import logging
import os
from datetime import timedelta

from airflow.decorators import task

from dbt_builds import run_dbt_command

logger = logging.getLogger("nba_pipeline")
REDSHIFT_TABLES = (
    ("raw_game_logs", ("player_id", "game_date", "matchup")),
    ("raw_schedule", ("schedule_date", "team_abbr", "opponent_abbr")),
    ("raw_game_line_scores", ("game_id", "team_id")),
    ("raw_player_reference", ("player_id",)),
)


def add_redshift_branch(
    combined,
    *,
    get_config,
    get_project_id,
    get_dataset,
    get_dbt_repo_root,
    season: str,
) -> None:
    """Attach the existing six named tasks without adding a task-group prefix."""

    @task.branch(retries=0)
    def check_redshift_enabled(combined_result: dict) -> str:
        """Branch: run Redshift sync only when ENABLE_REDSHIFT=true."""
        enabled = get_config("ENABLE_REDSHIFT", "false").lower() == "true"
        if enabled and combined_result["should_build"]:
            return "export_bigquery_bronze"
        return "skip_redshift_sync"

    @task(retries=1, retry_delay=timedelta(minutes=2))
    def export_bigquery_bronze(combined_result: dict) -> dict:
        """Export bronze tables from BigQuery to GCS as Parquet."""
        import nba_redshift_sync as sync

        project_id = get_project_id()
        gcs_bucket = get_config("GCS_BUCKET_NAME")
        bronze_dataset = get_dataset("BQ_DATASET_BRONZE", "nba_bronze")
        import pandas as pd

        run_stamp = pd.Timestamp.now(tz="UTC").strftime("%Y%m%dT%H%M%SZ")
        gcs_prefix = f"redshift_sync/{run_stamp}"

        for table, _ in REDSHIFT_TABLES:
            sync.export_bq_to_gcs_parquet(
                project_id,
                bronze_dataset,
                table,
                gcs_bucket,
                gcs_prefix,
            )

        combined_result["redshift_gcs_prefix"] = gcs_prefix
        return combined_result

    @task(retries=1, retry_delay=timedelta(minutes=2))
    def sync_to_s3(combined_result: dict) -> dict:
        """Copy Parquet files from GCS to S3."""
        import nba_redshift_sync as sync

        gcs_bucket = get_config("GCS_BUCKET_NAME")
        s3_bucket = get_config("AWS_S3_BUCKET_NAME")
        gcs_prefix = combined_result["redshift_gcs_prefix"]

        for table, _ in REDSHIFT_TABLES:
            sync.copy_gcs_to_s3(
                gcs_bucket,
                f"{gcs_prefix}/{table}/",
                s3_bucket,
                f"{gcs_prefix}/{table}",
            )

        combined_result["redshift_s3_prefix"] = gcs_prefix
        return combined_result

    @task(retries=1, retry_delay=timedelta(minutes=2))
    def load_redshift_bronze(combined_result: dict) -> dict:
        """Load S3 Parquet into Redshift and merge."""
        import nba_redshift_sync as sync

        s3_bucket = get_config("AWS_S3_BUCKET_NAME")
        iam_role = get_config("REDSHIFT_IAM_ROLE_ARN")
        schema = get_config("REDSHIFT_SCHEMA_BRONZE", "nba_bronze")
        s3_prefix = combined_result["redshift_s3_prefix"]

        sync.create_redshift_schemas_and_tables()

        for name, keys in REDSHIFT_TABLES:
            sync.load_s3_to_redshift(
                s3_bucket,
                f"{s3_prefix}/{name}/",
                schema,
                name,
                iam_role,
            )
            sync.merge_redshift_staging(schema, name, list(keys))
            sync.run_redshift_dq_checks(schema, name, list(keys))

        combined_result["redshift_load_status"] = "success"
        return combined_result

    @task(retries=1, retry_delay=timedelta(minutes=5))
    def dbt_build_redshift(combined_result: dict) -> dict:
        """Run dbt build targeting Redshift."""
        repo_root = get_dbt_repo_root()
        profiles_dir = repo_root / "dbt" / "profiles"
        command = [
            "dbt",
            "build",
            "--project-dir",
            str(repo_root),
            "--profiles-dir",
            str(profiles_dir),
            "--target",
            "redshift",
            "--exclude",
        ]

        env = os.environ.copy()
        env.setdefault("BQ_PROJECT", get_project_id())
        env.setdefault("NBA_SEASON", season)
        run_dbt_command(command, repo_root=repo_root, env=env)
        combined_result["redshift_dbt_status"] = "success"
        return combined_result

    @task(retries=0)
    def skip_redshift_sync(combined_result: dict) -> dict:
        """No-op when Redshift sync is disabled."""
        logger.info("Redshift sync is disabled, skipping")
        combined_result["redshift_status"] = "skipped"
        return combined_result

    redshift_check = check_redshift_enabled(combined)
    redshift_exported = export_bigquery_bronze(combined)
    redshift_s3 = sync_to_s3(redshift_exported)
    redshift_loaded = load_redshift_bronze(redshift_s3)
    dbt_build_redshift(redshift_loaded)
    redshift_skipped = skip_redshift_sync(combined)
    redshift_check >> [redshift_exported, redshift_skipped]
