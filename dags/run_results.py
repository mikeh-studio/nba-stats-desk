"""Aggregation and serialization of pipeline results; no Airflow dependency."""

from __future__ import annotations

from optional_assets import publication_details

DOMAIN_PREFIXES = {
    "game_logs": "",
    "schedule": "schedule_",
    "game_line_scores": "line_score_",
    "player_shot_locations": "shot_location_",
    "player_reference": "player_reference_",
    "injury_reports": "injury_report_",
}


def combine_results(
    game_result: dict,
    schedule_result: dict,
    line_score_result: dict,
    shot_location_result: dict,
    player_reference_result: dict,
    injury_report_result: dict,
    bootstrap_result: dict | None = None,
) -> dict:
    """Combine per-domain results into a single warehouse build context."""
    import nba_pipeline as pipeline

    if bootstrap_result:
        bootstrap_domains = bootstrap_result.get("domains", {})
        schedule_result = pipeline.apply_bootstrap_domain_result(
            schedule_result, bootstrap_domains.get("schedule", {})
        )
        line_score_result = pipeline.apply_bootstrap_domain_result(
            line_score_result, bootstrap_domains.get("game_line_scores", {})
        )
        player_reference_result = pipeline.apply_bootstrap_domain_result(
            player_reference_result,
            bootstrap_domains.get("player_reference", {}),
        )

    domains = {
        "game_logs": game_result,
        "schedule": schedule_result,
        "game_line_scores": line_score_result,
        "player_shot_locations": shot_location_result,
        "player_reference": player_reference_result,
        "injury_reports": injury_report_result,
    }
    counts = {
        f"{DOMAIN_PREFIXES[domain]}{field}": (
            result.get(field, 0) if field == "rows_unchanged" else result[field]
        )
        for domain, result in domains.items()
        for field in ("rows_loaded", "rows_inserted", "rows_updated", "rows_unchanged")
    }
    bootstrap_summary = {
        domain: {
            "ran": details.get("ran"),
            "rows_loaded": details.get("rows_loaded", 0),
            "rows_inserted": details.get("rows_inserted", 0),
            "rows_updated": details.get("rows_updated", 0),
            "reason": details.get("reason"),
        }
        for domain, details in (bootstrap_result or {}).get("domains", {}).items()
    }
    all_gcs = [
        result.get("gcs_uri", "")
        for result in domains.values()
        if result.get("gcs_uri", "")
    ]
    core_warehouse_changed = any(
        result["rows_loaded"] > 0
        for domain, result in domains.items()
        if domain != "injury_reports"
    )
    return {
        "season": game_result["season"],
        "watermark_before": game_result.get("watermark_before"),
        "watermark_after": game_result.get("watermark_after"),
        "injury_status": injury_report_result.get("asset_status", "no_change"),
        "injury_watermark_before": injury_report_result.get("watermark_before"),
        "injury_watermark_after": injury_report_result.get("watermark_after"),
        "gcs_uri": ",".join(all_gcs),
        **counts,
        "injury_report_candidate_count": injury_report_result.get("candidate_count", 0),
        "dq_results": {
            domain: result.get("dq_results", {}) for domain, result in domains.items()
        },
        "source_contract_results": {
            domain: result.get("source_contract", {})
            for domain, result in domains.items()
        },
        "reconciliation": {
            domain: result.get("reconciliation", {})
            for domain, result in domains.items()
        },
        "bronze_bootstrap": bootstrap_result or {},
        "bronze_bootstrap_summary": bootstrap_summary,
        "core_warehouse_changed": core_warehouse_changed,
        "should_build": any(
            [
                core_warehouse_changed,
                injury_report_result["rows_loaded"] > 0,
            ]
        ),
    }


def format_run_details(run_result: dict, *, get_config) -> str:
    """Preserve the existing ordered run-log fields and default values."""
    return publication_details(run_result) + (
        f"dbt_status={run_result.get('dbt_status', 'unknown')};"
        f"dbt_build_scope={run_result.get('dbt_build_scope', 'unknown')};"
        f"similarity_status={run_result.get('similarity_status', 'deferred_non_blocking')};"
        f"similarity_player_count={run_result.get('similarity_player_count', 0)};"
        f"similarity_archetype_count={run_result.get('similarity_archetype_count', 0)};"
        f"similarity_error={run_result.get('similarity_error', '')};"
        f"schedule_rows_loaded={run_result.get('schedule_rows_loaded', 0)};"
        f"line_score_rows_loaded={run_result.get('line_score_rows_loaded', 0)};"
        f"shot_location_rows_loaded={run_result.get('shot_location_rows_loaded', 0)};"
        f"shot_location_rows_inserted={run_result.get('shot_location_rows_inserted', 0)};"
        f"shot_location_rows_updated={run_result.get('shot_location_rows_updated', 0)};"
        f"player_reference_rows_loaded={run_result.get('player_reference_rows_loaded', 0)};"
        f"injury_report_rows_loaded={run_result.get('injury_report_rows_loaded', 0)};"
        f"injury_report_rows_inserted={run_result.get('injury_report_rows_inserted', 0)};"
        f"injury_report_rows_updated={run_result.get('injury_report_rows_updated', 0)};"
        f"injury_report_rows_unchanged={run_result.get('injury_report_rows_unchanged', 0)};"
        f"injury_report_candidate_count={run_result.get('injury_report_candidate_count', 0)};"
        f"rows_unchanged={run_result.get('rows_unchanged', 0)};"
        f"schedule_rows_unchanged={run_result.get('schedule_rows_unchanged', 0)};"
        f"line_score_rows_unchanged={run_result.get('line_score_rows_unchanged', 0)};"
        f"shot_location_rows_unchanged={run_result.get('shot_location_rows_unchanged', 0)};"
        f"player_reference_rows_unchanged={run_result.get('player_reference_rows_unchanged', 0)};"
        f"bronze_bootstrap={run_result.get('bronze_bootstrap_summary', {})};"
        f"redshift_status={run_result.get('redshift_status', get_config('ENABLE_REDSHIFT', 'false'))};"
        f"source_contracts={run_result.get('source_contract_results', {})};"
        f"dq={run_result.get('dq_results', {})};"
        f"reconciliation={run_result.get('reconciliation', {})}"
    )
