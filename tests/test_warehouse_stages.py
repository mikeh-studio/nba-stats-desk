from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
from google.cloud import bigquery

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dags"))

import nba_pipeline as pipeline
from warehouse_stages import check_staging, load_staging, merge_staging

# Expected source-to-warehouse contracts, independent of the helper registry.
CASES = [
    (
        "game_logs",
        "stg_game_logs",
        "raw_game_logs",
        "get_game_logs_schema",
        "run_data_quality_checks",
        "create_and_merge_raw_table",
    ),
    (
        "schedule",
        "stg_schedule_context",
        "raw_schedule",
        "get_schedule_schema",
        "run_schedule_quality_checks",
        "create_and_merge_schedule_table",
    ),
    (
        "game_line_scores",
        "stg_game_line_scores",
        "raw_game_line_scores",
        "get_game_line_scores_schema",
        "run_game_line_score_quality_checks",
        "create_and_merge_game_line_scores_table",
    ),
    (
        "player_shot_locations",
        "stg_player_shot_locations",
        "raw_player_shot_locations",
        "get_player_shot_locations_schema",
        "run_player_shot_location_quality_checks",
        "create_and_merge_player_shot_locations_table",
    ),
    (
        "player_reference",
        "stg_player_reference",
        "raw_player_reference",
        "get_player_reference_schema",
        "run_player_reference_quality_checks",
        "create_and_merge_player_reference_table",
    ),
    (
        "injury_reports",
        "stg_player_injury_reports",
        "raw_player_injury_reports",
        "get_injury_report_schema",
        "run_injury_report_quality_checks",
        "create_and_merge_injury_report_table",
    ),
]


@pytest.mark.parametrize("case", CASES, ids=[case[0] for case in CASES])
@pytest.mark.parametrize("row_count", [0, 3])
def test_domain_stage_chain_preserves_tables_checks_and_payloads(
    monkeypatch, case, row_count
):
    domain, stage_name, raw_name, schema_name, quality_name, merge_name = case
    client = object()
    client_factory = Mock(return_value=client)
    monkeypatch.setattr(bigquery, "Client", client_factory)
    ensure = Mock()
    load = Mock()
    monkeypatch.setattr(pipeline, "ensure_dataset", ensure)
    monkeypatch.setattr(pipeline, "load_gcs_to_bigquery", load)
    # Any accidental cross-domain dispatch must fail.
    for other in CASES:
        for name in other[3:]:
            monkeypatch.setattr(pipeline, name, Mock(side_effect=AssertionError(name)))
    schema = Mock(return_value=["expected_schema"])
    quality = Mock(return_value={"valid": True})
    merge = Mock(
        return_value={"pre_count": 10, "post_count": 11, "inserted": 1, "updated": 1}
    )
    monkeypatch.setattr(pipeline, schema_name, schema)
    monkeypatch.setattr(pipeline, quality_name, quality)
    monkeypatch.setattr(pipeline, merge_name, merge)
    reconciliation = Mock(wraps=pipeline.validate_merge_reconciliation)
    monkeypatch.setattr(pipeline, "validate_merge_reconciliation", reconciliation)
    context = {
        "gcs_uri": "gs://landing/input.csv",
        "source_contract": {"status": "passed"},
    }
    if domain != "player_reference":
        context["season"] = "2025-26"
    if domain in {"game_logs", "injury_reports"}:
        context.update(watermark_before="2026-01-01", watermark_after="2026-01-03")
    if domain == "injury_reports":
        context["candidate_count"] = 4
    original = {
        **context,
        "domain": domain,
        "row_count": row_count,
        "game_ids": ["must_not_leak"],
    }
    staged = load_staging(
        original,
        domain=domain,
        project_id="demo",
        bronze_dataset="bronze",
        location="US",
    )
    expected = {
        **context,
        "domain": domain,
        "row_count": row_count,
        "staging_table": f"demo.bronze.{stage_name}",
    }
    assert staged == expected
    assert "game_ids" in original
    ensure.assert_called_once_with(client, "demo.bronze", "US")
    resolver = Mock(return_value="demo")
    checked = check_staging(
        staged, domain=domain, resolve_project_id=resolver, season="2025-26"
    )
    assert checked is staged
    if row_count or domain != "game_logs":
        expected["dq_results"] = {"valid": True} if row_count else {}
    assert checked == expected
    merged = merge_staging(
        checked, domain=domain, project_id="demo", bronze_dataset="bronze"
    )
    expected_merged = {
        **context,
        "domain": domain,
        "raw_table": f"demo.bronze.{raw_name}",
        "rows_loaded": row_count,
        "rows_inserted": 1 if row_count else 0,
        "rows_updated": 1 if row_count else 0,
        "dq_results": {"valid": True} if row_count else {},
    }
    if row_count:
        expected_merged.update(
            rows_unchanged=1, reconciliation=merged["reconciliation"]
        )
        assert merged["reconciliation"]["unchanged"] == 1
        schema.assert_called_once_with()
        load.assert_called_once_with(
            client,
            context["gcs_uri"],
            f"demo.bronze.{stage_name}",
            ["expected_schema"],
            write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        )
        quality.assert_called_once_with(
            client,
            f"demo.bronze.{stage_name}",
            **({} if domain == "player_reference" else {"season": "2025-26"}),
        )
        merge.assert_called_once_with(
            client, f"demo.bronze.{stage_name}", f"demo.bronze.{raw_name}"
        )
        reconciliation.assert_called_once_with(
            domain=domain,
            rows_loaded=3,
            pre_count=10,
            post_count=11,
            inserted=1,
            updated=1,
        )
        resolver.assert_called_once_with()
    else:
        for operation in (schema, load, quality, merge, reconciliation, resolver):
            operation.assert_not_called()
        client_factory.assert_called_once_with(
            project="demo"
        )  # load ensures dataset even on no-op
    assert merged == expected_merged


@pytest.mark.parametrize("stage", ["load", "quality", "merge", "reconciliation"])
def test_stage_errors_propagate_to_airflow_retry_policy(monkeypatch, stage):
    monkeypatch.setattr(bigquery, "Client", lambda **kwargs: object())
    monkeypatch.setattr(pipeline, "ensure_dataset", lambda *args: None)
    monkeypatch.setattr(pipeline, "get_schedule_schema", lambda: [])
    monkeypatch.setattr(pipeline, "load_gcs_to_bigquery", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        pipeline, "run_schedule_quality_checks", lambda *args, **kwargs: {}
    )
    monkeypatch.setattr(
        pipeline,
        "create_and_merge_schedule_table",
        lambda *args: {"pre_count": 0, "post_count": 1, "inserted": 1, "updated": 0},
    )
    name = {
        "load": "load_gcs_to_bigquery",
        "quality": "run_schedule_quality_checks",
        "merge": "create_and_merge_schedule_table",
        "reconciliation": "validate_merge_reconciliation",
    }[stage]
    monkeypatch.setattr(pipeline, name, Mock(side_effect=RuntimeError("stage failed")))
    with pytest.raises(RuntimeError, match="stage failed"):
        loaded = load_staging(
            {"row_count": 1, "gcs_uri": "gs://input", "season": "2025-26"},
            domain="schedule",
            project_id="demo",
            bronze_dataset="bronze",
            location="US",
        )
        checked = check_staging(
            loaded,
            domain="schedule",
            resolve_project_id=lambda: "demo",
            season="2025-26",
        )
        merge_staging(
            checked, domain="schedule", project_id="demo", bronze_dataset="bronze"
        )


def test_injury_missing_optional_fields_keep_existing_defaults(monkeypatch):
    monkeypatch.setattr(bigquery, "Client", lambda **kwargs: object())
    monkeypatch.setattr(pipeline, "ensure_dataset", lambda *args: None)
    loaded = load_staging(
        {"row_count": 0, "gcs_uri": "", "season": "2025-26"},
        domain="injury_reports",
        project_id="demo",
        bronze_dataset="bronze",
        location="US",
    )
    assert loaded["candidate_count"] == 0
    assert loaded["watermark_before"] is loaded["watermark_after"] is None
    assert loaded["source_contract"] == {}
