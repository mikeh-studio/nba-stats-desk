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
from tests.support.espn import DAY, SEASON, fixtures

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
    assert all(r["dreb"] + r["oreb"] == r["reb"] for r in evidence.rows)
    assert all("pf" in r for r in evidence.rows)


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


@pytest.mark.parametrize("value", [None, "abc", "12:", "NaN", "inf", "-1:30"])
def test_malformed_minutes_are_invalid_evidence(value):
    with pytest.raises(SemanticError) as exc:
        minutes(value)
    assert exc.value.code == "invalid_evidence"


@pytest.mark.parametrize("payload", [None, "{", "null", "[]", "{}"])
def test_malformed_or_missing_joined_payload_is_invalid_evidence(payload):
    data = bundle()
    data["games"][0]["payload"] = payload
    with pytest.raises(SemanticError) as exc:
        snapshot(data, SEASON, "synthetic")
    assert exc.value.code == "invalid_evidence"


@pytest.mark.parametrize("change_input", [False, True])
def test_rehashed_but_inconsistent_box_is_not_served(change_input):
    from scripts.espn_corrections import revision

    data = bundle()
    candidate = json.loads(data["games"][0]["payload"])
    candidate["players"][0]["stats"]["PTS"] = 1
    if change_input:
        group = candidate["input"]["boxscore"]["players"][0]["statistics"][0]
        group["athletes"][0]["stats"][group["labels"].index("PTS")] = "1"
    data["games"][0]["payload"] = json.dumps(candidate)
    data["games"][0]["box_revision"] = revision(candidate)
    with pytest.raises(SemanticError) as exc:
        snapshot(data, SEASON, "synthetic")
    assert exc.value.code == "invalid_evidence"


@pytest.mark.parametrize("suffix", ["", "/stream"])
@pytest.mark.parametrize(
    "kind", ["nba", "league", "baseline", "similarity", "comparison", "comparison_rank"]
)
def test_partial_source_at_actual_endpoints(suffix, kind):
    from app.main import app, get_agent_client, get_repository, get_settings
    from app.repository import BigQueryWarehouseRepository
    from fastapi.testclient import TestClient
    from tests.support.semantic import settings

    value = snapshot(bundle(), SEASON, "synthetic")
    evidence = snapshot_evidence(value)
    config = replace(settings(SEASON), performance_cache_prewarm_enabled=False)
    repo = BigQueryWarehouseRepository(config, client=SimpleNamespace())
    repo._governed_warehouse = SimpleNamespace(load=lambda seasons: (value, evidence))
    question = {
        "nba": "How did Avery Able perform in the NBA this season?",
        "league": "How did Avery Able perform in the league this season?",
        "baseline": "Compare Avery Able points with the league average this season",
        "similarity": "Who is similar to Avery Able this season?",
        "comparison": "Compare Avery Able and Blake Baker this season",
        "comparison_rank": "Compare Avery Able and Blake Baker league percentiles this season",
    }[kind]
    previous = dict(app.dependency_overrides)
    app.dependency_overrides.update(
        {
            get_settings: lambda: config,
            get_repository: lambda: repo,
            get_agent_client: lambda: object(),
        }
    )
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/agent/ask" + suffix,
                params={"season": SEASON},
                json={"question": question},
            )
            assert response.status_code == 200
            if suffix:
                events = [
                    json.loads(s[6:])
                    for s in response.text.splitlines()
                    if s.startswith("data: ")
                ]
                result = next(e["payload"] for e in events if e.get("type") == "final")
            else:
                result = response.json()
            if kind in {"baseline", "similarity", "comparison_rank"}:
                assert result["status"] == "unsupported_coverage"
                assert not result["tables"]
            elif kind == "comparison":
                assert result["status"] == "ok"
                ev = result["semantic_evidence"]
                assert ev["kind"] == "player_comparison"
                assert ev["cohort_complete"] is False
                assert ev["scope"]["phases"] == ["Regular Season"]
                assert [
                    p["player"]["games_sampled"] for p in result["player_profiles"]
                ] == [1, 1]
                points = next(m for m in ev["metrics"] if m["key"] == "pts")
                assert (
                    points["left"]["value"],
                    points["right"]["value"],
                    points["delta"],
                ) == (0, 3, -3)
                assert all(
                    m[side]["percentile"] is None
                    for m in ev["metrics"]
                    for side in ("left", "right")
                )
                assert all(
                    m[side]["distribution"]["among_players"]["summary"]["n"] == 0
                    for m in ev["metrics"]
                    for side in ("left", "right")
                )
                assert result["source_coverage"]["provider"] == "espn"
                assert any(
                    "captured appearances only" in note
                    for note in result["assumptions"]
                )
                assert result["semantic_plan"]["model_calls"] == 0
            else:
                assert result["status"] == "ok"
                ev = result["semantic_evidence"]
                assert ev["player_id"] == 10
                assert ev["scope"]["phases"] == ["Regular Season"]
                assert ev["scope"]["start"] == "2025-07-01"
                assert ev["scope"]["end"] == "2026-06-30"
                assert next(m for m in ev["metrics"] if m["key"] == "pts")["value"] == 0
                assert result["semantic_plan"]["model_calls"] == 0
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


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


@pytest.mark.parametrize(
    "question",
    [
        "Compare Avery Able and Blake Baker this season",
        "Avery Able vs Blake Baker this season",
        "How does Avery Able compare to Blake Baker this season?",
    ],
)
def test_partial_preview_allows_named_scorecards_without_model_or_cohort(question):
    from app.agent.semantic_answer import SemanticAsk

    value = snapshot(bundle(), SEASON, "synthetic")
    evidence = snapshot_evidence(value)
    agent = SemanticAsk(
        SimpleNamespace(season=SEASON, agent_conversation_max_turns=6),
        SimpleNamespace(load=lambda seasons: (value, evidence)),
    )
    result = agent.answer(question, client=None, model="synthetic")
    assert result["status"] == "ok"
    assert result["semantic_evidence"]["kind"] == "player_comparison"
    assert result["semantic_plan"]["model_calls"] == 0


@pytest.mark.parametrize(
    "constraint",
    ["league rankings", "percentile", "league average", "peers", "baseline"],
)
def test_named_scorecard_does_not_bypass_partial_cohort_guards(constraint):
    from app.agent.semantic_answer import SemanticAsk

    value = snapshot(bundle(), SEASON, "synthetic")
    evidence = snapshot_evidence(value)
    agent = SemanticAsk(
        SimpleNamespace(season=SEASON, agent_conversation_max_turns=6),
        SimpleNamespace(load=lambda seasons: (value, evidence)),
    )
    result = agent.answer(
        f"Compare Avery Able and Blake Baker {constraint} this season",
        client=None,
        model="synthetic",
    )
    assert result["status"] == "unsupported_coverage"
    assert not result["tables"]


def test_provider_missing_minutes_remain_null_without_blocking_counting_stats():
    data = bundle()
    candidate = json.loads(data["games"][0]["payload"])
    candidate["input"]["boxscore"]["players"][0]["statistics"][0]["athletes"][0][
        "stats"
    ][0] = "--"
    updated = normalize(candidate["input"], candidate["game"], "box")
    data["games"][0]["payload"] = json.dumps(updated)
    data["games"][0]["box_revision"] = validate(updated)
    value = snapshot(data, SEASON, "synthetic")
    evidence = snapshot_evidence(value)
    row = next(r for r in evidence.rows if r["player_id"] == 10)
    assert row["min"] is None
    assert value["review_metadata"]["missing_minutes"] == 1
    assert "unknown, not zero" in value["limitations"][0]
    result = run_query(
        evidence,
        Query(
            metric="pts",
            season=SEASON,
            aggregation="average",
            player_id=10,
            min_games=1,
        ),
    )
    assert result["rows"][0]["value"] == 0
    assert minutes("--") is None
