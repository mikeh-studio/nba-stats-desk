import copy
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from app.agent.espn_dev_source import ESPNDevSource, minutes, snapshot
from app.agent.semantic_source import BigQuerySemanticSource, snapshot_evidence
from app.agent.semantics import Query, SemanticError, run_query
from scripts.espn_corrections import normalize, validate
from scripts.espn_sources import scoreboard

from test_espn_pilot import DAY, SEASON, fixtures
from test_semantic_source import Client, source_rows


def bundle():
    schedule, payload = fixtures()
    game = scoreboard(schedule, SEASON, DAY)[0]
    candidate = normalize(payload, game, "box")
    return {
        "games": [
            {
                "source_game_id": game["source_game_id"],
                "season": SEASON,
                "result_status": "accepted",
                "box_status": "accepted",
                "pbp_status": "pending_correction",
                "result_revision": candidate["result_revision"],
                "box_revision": validate(candidate),
                "pdf_verification_status": "not_verified",
                "payload": json.dumps(candidate),
            }
        ],
        "mappings": [
            {"source_player_id": "100", "nba_player_id": "10"},
            {"source_player_id": "101", "nba_player_id": "11"},
        ],
    }


def test_box_evidence_keeps_pending_pbp_separate_and_identity_explicit():
    value = snapshot(bundle(), SEASON, "synthetic")
    evidence = snapshot_evidence(value)
    assert not evidence.cohort_complete
    assert len(evidence.rows) == 2
    assert value["review_metadata"]["excluded_identity_rows"] == 1
    assert value["review_metadata"]["pbp_pending"] == 1
    assert evidence.rows[0]["game_id"] == "espn:1234567"
    assert evidence.rows[0]["min"] == 24
    assert evidence.rows[0]["home_away"] == "HOME"
    assert evidence.rows[1]["pts"] == 3


@pytest.mark.parametrize("status", ["stale", "retained_after_failure", "unavailable"])
def test_unaccepted_box_never_served(status):
    data = bundle()
    data["games"][0]["box_status"] = status
    with pytest.raises(SemanticError):
        snapshot(data, SEASON, "synthetic")


@pytest.mark.parametrize(
    "defect", ["hash", "dependency", "duplicate", "identity", "nba_identity"]
)
def test_inconsistent_sources_fail_closed(defect):
    data = bundle()
    if defect == "hash":
        data["games"][0]["box_revision"] = "changed"
    if defect == "dependency":
        data["games"][0]["result_revision"] = "changed"
    if defect == "duplicate":
        data["games"].append(copy.deepcopy(data["games"][0]))
    if defect == "nba_identity":
        data["mappings"][1]["nba_player_id"] = "10"
    if defect == "identity":
        data["mappings"].append(copy.deepcopy(data["mappings"][0]))
    with pytest.raises(SemanticError):
        snapshot(data, SEASON, "synthetic")


def test_partial_evidence_allows_values_without_rank_and_blocks_rank():
    evidence = snapshot_evidence(snapshot(bundle(), SEASON, "synthetic"))
    query = Query(
        metric="pts", season=SEASON, aggregation="average", player_id=11, min_games=1
    )
    result = run_query(evidence, query)
    assert result["rows"][0]["value"] == 3
    assert result["rows"][0]["rank"] is None
    with pytest.raises(SemanticError, match="complete cohort"):
        run_query(evidence, replace(query, operation="rank"))


def test_minutes_units():
    assert minutes("12:30") == 12.5 and minutes("0") == 0
    with pytest.raises(SemanticError):
        minutes("12:99")


def test_opt_in_source_denies_remote_ingress(monkeypatch):
    import app.main as main
    from app.config import Settings
    from app.main import app
    from fastapi.testclient import TestClient

    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: Settings(
            project_id="test-project",
            gold_dataset="unused",
            metadata_dataset="unused",
            freshness_threshold_hours=36,
            max_search_results=12,
            ask_local_espn_dev=True,
            performance_cache_prewarm_enabled=False,
        ),
    )
    with TestClient(app, client=("192.0.2.1", 1234)) as client:
        assert client.get("/ask").status_code == 403


def test_local_retained_season_requires_missing_archive_and_keeps_season_filter():
    from google.api_core.exceptions import NotFound

    rows = [{**r, "season": "2025-26"} for r in source_rows()]

    class MissingArchiveClient(Client):
        def __init__(self):
            super().__init__(rows=rows)
            self.calls = []

        def query(self, sql, **kwargs):
            self.calls.append(sql)
            if "nba_gold_2025_26" in sql:
                raise NotFound("missing synthetic archive")
            return super().query(sql, **kwargs)

    client = MissingArchiveClient()
    value = ESPNDevSource(
        BigQuerySemanticSource(client, project="test-project")
    ).capture(["2025-26"])
    assert len(client.calls) == 2
    assert "`test-project.nba_gold.fct_player_game_stats`" in client.calls[1]
    assert "WHERE season = @season_0" in client.calls[1]
    assert client.config.query_parameters[2].value == "2025-26"
    assert value["review_metadata"]["archive_status"] == "unavailable"
    assert value["coverage_notes"]
    assert value["scope_assumptions"] == value["coverage_notes"]
    assert {r["season"] for r in snapshot_evidence(value).rows} == {"2025-26"}


@pytest.mark.parametrize(
    "code", ["access_denied", "invalid_evidence", "unsupported_coverage"]
)
def test_local_retained_season_never_replaces_permission_invalid_or_empty_errors(code):
    class Source:
        def capture(self, seasons):
            raise SemanticError(code, "original failure")

    with pytest.raises(SemanticError, match="original failure") as exc:
        ESPNDevSource(Source()).capture(["2025-26"])
    assert exc.value.code == code


@pytest.mark.parametrize("season", ["2024-25", "2025-26"])
def test_local_historical_archive_remains_preferred(season):
    client = Client(rows=[{**r, "season": season} for r in source_rows()])
    value = ESPNDevSource(
        BigQuerySemanticSource(client, project="test-project")
    ).capture([season])
    assert "nba_gold_" + season.replace("-", "_") in client.sql
    assert "review_metadata" not in value


@pytest.mark.parametrize("word", ["compare", "compares", "comparison"])
def test_partial_preview_refuses_comparisons_before_planning(word):
    from app.agent.semantic_answer import SemanticAsk

    value = snapshot(bundle(), SEASON, "synthetic")
    evidence = snapshot_evidence(value)
    agent = SemanticAsk(
        SimpleNamespace(season=SEASON, agent_conversation_max_turns=6),
        SimpleNamespace(load=lambda seasons: (value, evidence)),
    )
    result = agent.answer(
        f"How does Player One {word} to Player Two this season?",
        client=None,
        model="synthetic",
    )
    assert result["status"] == "unsupported_coverage"
    assert not result["tables"]
