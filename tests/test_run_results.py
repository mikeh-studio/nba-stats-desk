from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dags"))

from run_results import combine_results, format_run_details


def domain_result(count=0, **extra):
    return {
        "season": "2025-26",
        "rows_loaded": count,
        "rows_inserted": count,
        "rows_updated": 0,
        **extra,
    }


@pytest.mark.parametrize("changed", [None, 0, 1, 2, 3, 4, 5])
def test_build_decision_distinguishes_core_injury_only_and_noop(changed):
    sources = [domain_result(int(index == changed)) for index in range(6)]
    before = deepcopy(sources)
    result = combine_results(*sources)
    assert result["should_build"] is (changed is not None)
    assert result["core_warehouse_changed"] is (changed is not None and changed != 5)
    assert result["rows_loaded"] == int(changed == 0)
    assert result["injury_report_rows_loaded"] == int(changed == 5)
    assert result["gcs_uri"] == ""
    assert result["injury_status"] == "no_change"
    assert sources == before


def test_domain_accounting_and_evidence_keep_existing_names_and_order():
    names = [
        "game_logs",
        "schedule",
        "game_line_scores",
        "player_shot_locations",
        "player_reference",
        "injury_reports",
    ]
    prefixes = [
        "",
        "schedule_",
        "line_score_",
        "shot_location_",
        "player_reference_",
        "injury_report_",
    ]
    sources = [
        domain_result(
            index + 1,
            gcs_uri=f"gs://{name}",
            dq_results={name: "valid"},
            source_contract={"source": name},
            reconciliation={"source": name},
        )
        for index, name in enumerate(names)
    ]
    sources[0].update(watermark_before="2026-01-01", watermark_after="2026-01-02")
    sources[-1].update(
        watermark_before="2026-01-03",
        watermark_after="2026-01-04",
        asset_status="failed_non_blocking",
        candidate_count=7,
    )
    result = combine_results(*sources)
    assert result["gcs_uri"] == ",".join(f"gs://{name}" for name in names)
    for index, (name, prefix) in enumerate(zip(names, prefixes)):
        assert (
            result[f"{prefix}rows_loaded"]
            == result[f"{prefix}rows_inserted"]
            == index + 1
        )
        assert result[f"{prefix}rows_updated"] == result[f"{prefix}rows_unchanged"] == 0
        assert result["dq_results"][name] == {name: "valid"}
        assert result["source_contract_results"][name] == {"source": name}
        assert result["reconciliation"][name] == {"source": name}
    assert list(result["dq_results"]) == names
    assert result["watermark_after"] == "2026-01-02"
    assert result["injury_watermark_after"] == "2026-01-04"
    assert result["injury_status"] == "failed_non_blocking"
    assert result["injury_report_candidate_count"] == 7


def test_bootstrap_rows_contribute_to_build_decision_and_accounting():
    bootstrap = {
        "domains": {
            "schedule": {
                "ran": True,
                "rows_loaded": 2,
                "rows_inserted": 2,
                "rows_updated": 0,
                "reason": "missing",
            }
        }
    }
    result = combine_results(
        *(domain_result() for _ in range(6)), bootstrap_result=bootstrap
    )
    assert result["rows_loaded"] == 0
    assert result["schedule_rows_loaded"] == result["schedule_rows_inserted"] == 2
    assert result["should_build"] is result["core_warehouse_changed"] is True
    assert result["bronze_bootstrap_summary"]["schedule"]["reason"] == "missing"


def test_persisted_details_match_pre_refactor_examples_byte_for_byte():
    examples = json.loads(
        (Path(__file__).parent / "fixtures" / "pipeline_run_details.json").read_text()
    )
    for example in examples:
        assert (
            format_run_details(
                example["result"],
                get_config=lambda key, default: example["redshift_enabled"],
            )
            == example["details"]
        )
