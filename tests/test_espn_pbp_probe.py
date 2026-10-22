"""Synthetic checks for the separate, non-publishing ESPN PBP experiment."""

import copy
import json
from datetime import date

import pytest
from scripts import espn_pbp_probe as probe
from scripts import espn_sources as source

from test_espn_pilot import DAY, SEASON, fixtures


def sample():
    schedule, payload = fixtures()
    return payload, source.scoreboard(schedule, SEASON, DAY)[0]


def test_assist_roles_do_not_depend_on_participant_order_or_nba_ids():
    payload, game = sample()
    result = probe.normalize(payload, game)
    assert result["nba_game_id"] is None
    assert all(p["nba_player_id"] is None for p in result["players"])
    assert result["validation"]["cross_provider"] == "not_validated"
    assert result["validation"]["pdf"] == "not_attempted"
    assert probe.pairs(result)[0]["assister_source_id"] == "100"
    assert probe.pairs(result)[0]["scorer_source_id"] == "101"
    payload["plays"][0]["participants"].reverse()
    assert probe.revision(probe.normalize(payload, game)) == probe.revision(result)


def test_replay_ignores_unrelated_media_but_detects_valid_pbp_correction():
    payload, game = sample()
    original = probe.normalize(payload, game)
    edited = copy.deepcopy(payload)
    edited["news"] = {"unrelated": "new media"}
    assert probe.revision(probe.normalize(edited, game)) == probe.revision(original)
    edited["plays"][0]["clock"]["displayValue"] = "10:59"
    assert probe.revision(probe.normalize(edited, game)) != probe.revision(original)


@pytest.mark.parametrize("defect", ["score", "assist", "duplicate", "participants"])
def test_rejects_inconsistent_candidates(defect):
    payload, game = sample()
    if defect == "score":
        payload["plays"][-1]["homeScore"] = 9
    elif defect == "assist":
        payload["plays"][0]["text"] = "Blake Baker makes three point jumper"
    elif defect == "duplicate":
        payload["plays"].append(payload["plays"][0])
    else:
        payload["plays"][0]["participants"] = []
    with pytest.raises(ValueError):
        probe.normalize(payload, game)


def test_failure_is_reported_without_candidate_or_publication(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "ROOT", tmp_path)
    schedule, payload = fixtures()
    payload["plays"][-1]["homeScore"] = 9

    def capture(url, directory, session):
        directory.mkdir(parents=True)
        return schedule if "scoreboard" in url else payload

    monkeypatch.setattr(probe, "capture_json", capture)
    output = tmp_path / "reports" / "run"
    result = probe.run(SEASON, DAY, DAY, output, None)
    assert result["failed"] == 1
    assert result["observed_final_games"] == 1
    assert result["reconciled_games"] == 0
    assert not list(output.rglob("candidate.json"))
    assert json.loads((output / "result.json").read_text()) == result


def test_window_bounds_before_network_or_writes(tmp_path):
    with pytest.raises(ValueError, match="seven"):
        probe.run(SEASON, DAY, date(2025, 10, 30), tmp_path / "reports", None)


def test_refuses_overwrite(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "ROOT", tmp_path)
    output = tmp_path / "reports" / "previous"
    output.mkdir(parents=True)
    (output / "candidate.json").write_text("prior accepted data")
    with pytest.raises(ValueError, match="new directory"):
        probe.run(SEASON, DAY, DAY, output, None)
    assert (output / "candidate.json").read_text() == "prior accepted data"
