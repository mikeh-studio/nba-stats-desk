"""Shared warehouse stage mechanics; domain policies stay explicit.

These helpers do not depend on Airflow. Thin DAG tasks own retries, optional
failure handling, configuration lookup, and dependency wiring.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

logger = logging.getLogger("nba_pipeline")


@dataclass(frozen=True)
class DomainSpec:
    staging_table: str
    raw_table: str
    schema_function: str
    quality_function: str
    merge_function: str
    required_fields: tuple[str, ...] = ("season",)
    optional_fields: tuple[tuple[str, object], ...] = ()
    quality_uses_season: bool = True
    empty_quality_result: bool = True


DOMAINS = {
    "game_logs": DomainSpec(
        staging_table="stg_game_logs",
        raw_table="raw_game_logs",
        schema_function="get_game_logs_schema",
        quality_function="run_data_quality_checks",
        merge_function="create_and_merge_raw_table",
        required_fields=("season", "watermark_before", "watermark_after"),
        empty_quality_result=False,
    ),
    "schedule": DomainSpec(
        staging_table="stg_schedule_context",
        raw_table="raw_schedule",
        schema_function="get_schedule_schema",
        quality_function="run_schedule_quality_checks",
        merge_function="create_and_merge_schedule_table",
    ),
    "game_line_scores": DomainSpec(
        staging_table="stg_game_line_scores",
        raw_table="raw_game_line_scores",
        schema_function="get_game_line_scores_schema",
        quality_function="run_game_line_score_quality_checks",
        merge_function="create_and_merge_game_line_scores_table",
    ),
    "player_shot_locations": DomainSpec(
        staging_table="stg_player_shot_locations",
        raw_table="raw_player_shot_locations",
        schema_function="get_player_shot_locations_schema",
        quality_function="run_player_shot_location_quality_checks",
        merge_function="create_and_merge_player_shot_locations_table",
    ),
    "player_reference": DomainSpec(
        staging_table="stg_player_reference",
        raw_table="raw_player_reference",
        schema_function="get_player_reference_schema",
        quality_function="run_player_reference_quality_checks",
        merge_function="create_and_merge_player_reference_table",
        required_fields=(),
        quality_uses_season=False,
    ),
    "injury_reports": DomainSpec(
        staging_table="stg_player_injury_reports",
        raw_table="raw_player_injury_reports",
        schema_function="get_injury_report_schema",
        quality_function="run_injury_report_quality_checks",
        merge_function="create_and_merge_injury_report_table",
        optional_fields=(
            ("candidate_count", 0),
            ("watermark_before", None),
            ("watermark_after", None),
        ),
    ),
}


def _result_context(result: dict, spec: DomainSpec) -> dict:
    return {
        **{key: result[key] for key in spec.required_fields},
        **{key: result.get(key, default) for key, default in spec.optional_fields},
        "gcs_uri": result["gcs_uri"],
        "source_contract": result.get("source_contract", {}),
    }


def load_staging(
    extract_result: dict,
    *,
    domain: str,
    project_id: str,
    bronze_dataset: str,
    location: str,
) -> dict:
    """Load non-empty extracts, preserving each domain's stage payload."""
    from google.cloud import bigquery as bq

    import nba_pipeline as pipeline

    spec = DOMAINS[domain]
    client = bq.Client(project=project_id)
    pipeline.ensure_dataset(client, f"{project_id}.{bronze_dataset}", location)
    staging_table = f"{project_id}.{bronze_dataset}.{spec.staging_table}"
    if extract_result["row_count"] != 0:
        pipeline.load_gcs_to_bigquery(
            client,
            extract_result["gcs_uri"],
            staging_table,
            getattr(pipeline, spec.schema_function)(),
            write_disposition=bq.WriteDisposition.WRITE_TRUNCATE,
        )
    elif domain == "game_logs":
        logger.info("Skipping game log staging load because extract produced no rows")
    return {
        "domain": domain,
        "staging_table": staging_table,
        "row_count": extract_result["row_count"],
        **_result_context(extract_result, spec),
    }


def check_staging(
    load_result: dict,
    *,
    domain: str,
    resolve_project_id: Callable[[], str],
    season: str,
) -> dict:
    """Run the domain's quality gate; empty batches never query staging."""
    from google.cloud import bigquery as bq

    import nba_pipeline as pipeline

    spec = DOMAINS[domain]
    if load_result["row_count"] == 0:
        if spec.empty_quality_result:
            load_result["dq_results"] = {}
        return load_result

    client = bq.Client(project=resolve_project_id())
    kwargs = {"season": season} if spec.quality_uses_season else {}
    load_result["dq_results"] = getattr(pipeline, spec.quality_function)(
        client, load_result["staging_table"], **kwargs
    )
    return load_result


def merge_staging(
    load_result: dict, *, domain: str, project_id: str, bronze_dataset: str
) -> dict:
    """Merge with the existing domain routine and reconcile every non-empty batch."""
    from google.cloud import bigquery as bq

    import nba_pipeline as pipeline

    spec = DOMAINS[domain]
    raw_table = f"{project_id}.{bronze_dataset}.{spec.raw_table}"
    merged_counts = {"rows_inserted": 0, "rows_updated": 0}
    if load_result["row_count"] != 0:
        client = bq.Client(project=project_id)
        result = getattr(pipeline, spec.merge_function)(
            client, load_result["staging_table"], raw_table
        )
        reconciliation = pipeline.validate_merge_reconciliation(
            domain=domain,
            rows_loaded=load_result["row_count"],
            pre_count=result["pre_count"],
            post_count=result["post_count"],
            inserted=result["inserted"],
            updated=result["updated"],
        )
        merged_counts = {
            "rows_inserted": result["inserted"],
            "rows_updated": result["updated"],
            "rows_unchanged": reconciliation["unchanged"],
            "reconciliation": reconciliation,
        }
    return {
        "domain": domain,
        "raw_table": raw_table,
        "rows_loaded": load_result["row_count"],
        **merged_counts,
        **_result_context(load_result, spec),
        "dq_results": load_result.get("dq_results", {}),
    }
