"""Capture keeps failed source evidence, and next-season validation is isolated."""

import json

import pandas as pd
import pytest
from app.research_snapshots import read_snapshot
from scripts.capture_research_context import capture
from tests.test_source_contracts import _schedule_rows


def test_next_season_contract_and_legacy_missing_tipoff():
    from nba_source_contracts import SourceContractError, validate_source_contract

    frame = pd.DataFrame(_schedule_rows())
    old = validate_source_contract("schedule", frame)
    assert old.frame.GAME_TIME_UTC.isna().all()
    frame["SEASON"] = "2026-27"
    frame["SCHEDULE_DATE"] = "2026-11-01"
    assert len(validate_source_contract("schedule", frame, season="2026-27").frame) == 2
    with pytest.raises(SourceContractError):
        validate_source_contract("schedule", frame)


def test_capture_failure_is_saved_and_raised(monkeypatch, tmp_path):
    import nba_pipeline
    import nba_source_contracts
    from nba_source_contracts import SourceContractError

    memberships = tmp_path / "memberships.json"
    memberships.write_text("[]")
    monkeypatch.setattr(
        nba_pipeline,
        "get_upcoming_schedule",
        lambda **kwargs: pd.DataFrame(_schedule_rows()),
    )
    monkeypatch.setattr(
        nba_pipeline,
        "get_all_official_injury_reports",
        lambda *args, **kwargs: pd.DataFrame(),
    )
    result = {"source_name": "schedule", "fatal_count": 2, "rows_failed": 101}

    def fail(*args, **kwargs):
        raise SourceContractError(result, quarantine_frame=pd.DataFrame([{"bad": 1}]))

    monkeypatch.setattr(nba_source_contracts, "validate_source_contract", fail)
    with pytest.raises(RuntimeError, match="retained evidence"):
        capture(tmp_path / "captures", memberships, "2025-26")
    saved = list((tmp_path / "captures" / "2025-26").glob("*.json"))
    assert len(saved) == 1
    doc = read_snapshot(saved[0])
    assert doc["source_status"]["schedule"]["contract"]["rows_failed"] == 101
    assert doc["source_status"]["schedule"]["quarantine"] == [{"bad": 1}]
    assert doc["source_status"]["injury_reports"]["status"] == "unavailable"
    assert len(doc["inputs"]["raw_extracts"]["schedule"]) == 2
    assert json.loads(saved[0].read_text())["sha256"] == doc["sha256"]
