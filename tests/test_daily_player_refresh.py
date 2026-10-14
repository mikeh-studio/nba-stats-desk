"""Daily maintenance must not publish stale, incomplete or unvalidated inputs."""

import json
import subprocess
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from app.agent.semantic_source import snapshot_digest
from app.agent.semantics import COMPONENTS
from scripts import refresh_player_comparisons as daily
from scripts.repair_context_snapshot import statistics_equal
from scripts.repair_game_schedule import retain_warehouse_schedule
from scripts.stage_game_log_repair import require_original_baseline


def sample():
    rows = [
        dict(
            dict.fromkeys(COMPONENTS, 0),
            season="2025-26",
            season_type="Regular Season",
            game_id="0022500001",
            game_date="2025-11-01",
            player_id=i + 1,
            player_name=f"Example {i}",
            team_abbr=team,
            opponent_abbr=opp,
            home_away="AWAY",
            matchup=f"{team} @ {opp}",
            wl="W" if i == 0 else "L",
            min=20,
            pts=2,
            fgm=1,
            fga=2,
            oreb=0,
            dreb=0,
            pf=0,
        )
        for i, (team, opp) in enumerate([("AAA", "BBB"), ("BBB", "AAA")])
    ]
    snapshot = dict(
        rows=rows,
        coverage=[
            dict(
                season="2025-26",
                phase="Regular Season",
                rows=2,
                data_through="2025-11-01",
            )
        ],
        sources=["synthetic"],
        source_timestamp="2026-10-13T00:00:00Z",
        capture={},
    )
    snapshot["sha256"] = snapshot_digest(snapshot)
    schedule = [
        dict(
            game_id=r["game_id"],
            team_abbr=r["team_abbr"],
            opponent_abbr=r["opponent_abbr"],
            schedule_date=r["game_date"],
            season=r["season"],
            game_status="final",
            home_away="HOME" if i == 0 else "AWAY",
        )
        for i, r in enumerate(rows)
    ]
    return snapshot, schedule


@pytest.mark.parametrize(
    "defect", ["date", "opponent", "venue", "duplicate", "missing"]
)
def test_retained_schedule_rejects_unverified_assignments(defect):
    snapshot, schedule = sample()
    if defect == "date":
        schedule[0]["schedule_date"] = "2025-11-02"
    if defect == "opponent":
        schedule[0]["opponent_abbr"] = "CCC"
    if defect == "venue":
        schedule[0]["home_away"] = "AWAY"
    if defect == "duplicate":
        schedule.append(deepcopy(schedule[0]))
    if defect == "missing":
        schedule.pop()
    with pytest.raises(ValueError):
        retain_warehouse_schedule(snapshot, schedule)


def test_retained_schedule_preserves_neutral_site_designation_and_source_inputs():
    snapshot, schedule = sample()
    original = deepcopy(snapshot)
    refreshed, audit = retain_warehouse_schedule(snapshot, schedule)
    assert [r["home_away"] for r in refreshed["rows"]] == ["HOME", "AWAY"]
    assert refreshed["rows"][0]["matchup"] == "AAA vs. BBB"
    assert audit["venue_basis"] == "retained_validated_warehouse_schedule"
    assert original == snapshot


def test_equal_statistics_ignore_capture_timestamp_but_detect_small_corrections():
    snapshot, _ = sample()
    other = deepcopy(snapshot)
    other["source_timestamp"] = "2026-10-14T00:00:00Z"
    assert statistics_equal(snapshot, other)
    other["rows"][0]["min"] += 0.00001
    assert not statistics_equal(snapshot, other)
    other["rows"].pop()
    assert not statistics_equal(snapshot, other)


@pytest.mark.parametrize("changed", [False, True])
def test_recheck_blocks_a_newer_gold_baseline_before_build(changed):
    original, _ = sample()
    actual = deepcopy(original["rows"])
    if changed:
        actual[0]["tov"] = 1
    client = SimpleNamespace(
        query=lambda *a, **k: SimpleNamespace(result=lambda: actual)
    )
    if changed:
        with pytest.raises(ValueError, match="baseline changed"):
            require_original_baseline(
                client,
                original,
                "test-project.nba_gold_repair_012345abcdef.fct_player_game_stats_before",
            )
    else:
        require_original_baseline(
            client,
            original,
            "test-project.nba_gold_repair_012345abcdef.fct_player_game_stats_before",
        )


def prepare_refresh(monkeypatch, tmp_path, *, changed=False, publish=False):
    original, schedule = sample()
    original, _ = retain_warehouse_schedule(original, schedule)
    candidate = deepcopy(original)
    if changed:
        candidate["rows"][0]["pts"] = 3
        candidate["rows"][0]["ftm"] = candidate["rows"][0]["fta"] = 1
    monkeypatch.setattr(daily, "capture_sources", lambda *a, **k: None)
    monkeypatch.setattr(
        daily,
        "BigQuerySemanticSource",
        lambda *a, **k: SimpleNamespace(capture=lambda seasons: original),
    )
    monkeypatch.setattr(
        daily,
        "repair",
        lambda *a, **k: (
            candidate,
            {"phases": {"Regular Season": {"status": "passed"}}},
        ),
    )
    client = SimpleNamespace(
        query=lambda *a, **k: SimpleNamespace(result=lambda: schedule)
    )
    args = SimpleNamespace(
        source_timeout=3, publish=publish, dbt=Path("/example/dbt"), directory=tmp_path
    )
    return args, client


@pytest.mark.parametrize("publish", [False, True])
def test_unchanged_capture_does_not_build_or_publish(monkeypatch, tmp_path, publish):
    args, client = prepare_refresh(monkeypatch, tmp_path, publish=publish)
    monkeypatch.setattr(
        daily.subprocess,
        "run",
        lambda *a, **k: pytest.fail("Unnecessary warehouse build"),
    )
    result = daily.refresh(args, client, "test-project", tmp_path)
    assert result["status"] == "unchanged"
    assert result["warehouse_mutations"] == 0
    assert (tmp_path / "source-contract.json").exists()


def test_preview_never_stages_or_publishes(monkeypatch, tmp_path):
    args, client = prepare_refresh(monkeypatch, tmp_path, changed=True)
    monkeypatch.setattr(
        daily.subprocess,
        "run",
        lambda *a, **k: pytest.fail("Preview mutated warehouse"),
    )
    assert (
        daily.refresh(args, client, "test-project", tmp_path)["status"]
        == "validated_changes"
    )


def test_failed_candidate_build_never_promotes(monkeypatch, tmp_path):
    args, client = prepare_refresh(monkeypatch, tmp_path, changed=True, publish=True)

    def fail(*a, **k):
        raise subprocess.CalledProcessError(1, ["dbt"])

    monkeypatch.setattr(daily.subprocess, "run", fail)
    monkeypatch.setattr(
        daily, "promote", lambda *a, **k: pytest.fail("Failed candidate promoted")
    )
    with pytest.raises(subprocess.CalledProcessError):
        daily.refresh(args, client, "test-project", tmp_path)
    assert not (tmp_path / "publication-attempt.json").exists()


def test_successful_refresh_requires_guarded_stage_then_promotion(
    monkeypatch, tmp_path
):
    args, client = prepare_refresh(monkeypatch, tmp_path, changed=True, publish=True)

    def stage(command, **kwargs):
        assert (
            "--retain-schedule" in command and "--require-original-baseline" in command
        )
        folder = tmp_path / "stage"
        folder.mkdir()
        (folder / "validated.json").write_text(json.dumps({"sha256": "fixture"}))

    monkeypatch.setattr(daily.subprocess, "run", stage)
    monkeypatch.setattr(
        daily, "promote", lambda plan, sha, client: {"tables": 12, "job_id": "test-job"}
    )
    result = daily.refresh(args, client, "test-project", tmp_path)
    assert result["status"] == "published"
    assert (tmp_path / "publication.json").exists()


@pytest.mark.parametrize("component,value", [("min", 90), ("pts", None)])
def test_quarantine_or_missing_component_does_not_become_success(
    tmp_path, component, value
):
    snapshot, _ = sample()
    snapshot["rows"][0][component] = value
    with pytest.raises(ValueError):
        daily.validate_comparison_contract(snapshot, tmp_path)
    assert (tmp_path / "source-contract.json").exists()
    assert (tmp_path / "quarantine.csv").exists() or (
        tmp_path / "rejected-comparison-inputs.csv"
    ).exists()


def test_source_outage_retains_data_and_records_failure(monkeypatch, tmp_path):
    from google.cloud import bigquery

    monkeypatch.setattr(
        daily,
        "get_settings",
        lambda: SimpleNamespace(
            project_id="test-project",
            gold_dataset="nba_gold",
            agent_dataset="nba_agent",
        ),
    )
    monkeypatch.setattr(bigquery, "Client", lambda **k: object())

    def fail(*a, **k):
        raise TimeoutError("provider-specific details must remain private")

    monkeypatch.setattr(daily, "capture_sources", fail)
    args = SimpleNamespace(
        directory=tmp_path, publish=True, source_timeout=3, dbt=Path("/example/dbt")
    )
    assert daily.execute(args) == 1
    receipts = list(tmp_path.glob("*/result.json"))
    result = json.loads(receipts[0].read_text())
    assert result["serving_data_retained"] is True
    assert result["publication_state"] == "not_attempted"
    assert "provider-specific" not in receipts[0].read_text()
