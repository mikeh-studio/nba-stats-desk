"""Public synthetic cases for route, scope, arithmetic and endpoint parity."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from app.agent.conversation import InMemoryConversationStore
from app.agent.player_comparison import comparison_sides
from app.agent.player_splits import answer_split, parse_split
from app.agent.semantic_answer import SemanticAsk
from app.agent.semantic_serving import source_players
from app.agent.semantics import COMPONENTS, Evidence, SemanticError
from app.agent.service import StatsAgent
from tests.test_semantic_migration import settings


@pytest.fixture
def source():
    rows = []
    for i in range(8):
        rows.append(
            dict(
                dict.fromkeys(COMPONENTS, 0),
                season="2024-25",
                season_type="Regular Season" if i < 6 else "Playoffs",
                game_id=str(i),
                game_date=f"2025-04-{10 + i:02}",
                player_id=101,
                player_name="Avery Example",
                team_abbr="AAA",
                opponent_abbr="BBB",
                home_away="HOME" if i % 2 else "AWAY",
                min=30,
                pts=i * 2,
                ast=i,
                fgm=1 if i % 2 else 0,
                fga=1 if i % 2 else 9,
                fta=0,
            )
        )
    coverage = {
        ("2024-25", phase): max(
            r["game_date"] for r in rows if r["season_type"] == phase
        )
        for phase in ("Regular Season", "Playoffs")
    }
    return Evidence(
        rows, frozenset(coverage), "synthetic", "split-fixture", coverage, complete=True
    )


def answer(source, text):
    return answer_split(text, source, source_players(source), "2024-25")


def test_singular_playoff_is_phase_not_identity(source):
    question = "How did Avery Example played in Regular Season vs Playoff 2024-25?"
    assert comparison_sides(question) is None
    result = answer(source, question)
    points = result["semantic_evidence"]["comparisons"][0]
    assert points["current"]["rows"][0]["value"] == 13
    assert points["baseline"]["rows"][0]["value"] == 5
    assert points["difference"] == 8
    assert points["overlapping_game_ids"] == []


def test_venue_pooled_ratios_not_means_and_filters(source):
    result = answer(
        source,
        "Compare Avery Example FG% home vs away in 2024-25 regular season against BBB",
    )
    metric = result["semantic_evidence"]["comparisons"][0]
    assert metric["current"]["rows"][0]["value"] == 0
    assert metric["baseline"]["rows"][0]["value"] == 1
    assert metric["difference"] == -100
    assert set(metric["current"]["rows"][0]["game_ids"]) == {"0", "2", "4"}


def test_recent_prior_disjoint_and_cutoff(source):
    result = answer(
        source,
        "Compare Avery Example points last 2 games vs prior 2 games in 2024-25 as of 2025-04-14",
    )
    metric = result["semantic_evidence"]["comparisons"][0]
    assert set(metric["current"]["rows"][0]["game_ids"]) == {"3", "4"}
    assert set(metric["baseline"]["rows"][0]["game_ids"]) == {"1", "2"}
    assert metric["difference"] == 4


def test_before_after_boundaries_and_partial_interpretation(source):
    result = answer(
        source, "Why did Avery Example improve before and after 2025-04-13 in 2024-25?"
    )
    metric = result["semantic_evidence"]["comparisons"][0]
    assert set(metric["current"]["rows"][0]["game_ids"]) == {"3", "4", "5"}
    assert set(metric["baseline"]["rows"][0]["game_ids"]) == {"0", "1", "2"}
    assert result["answerability"] == "partial"
    assert "not evidence of causation" in result["answer"]


def test_missing_components_suppress_difference_and_missing_venue_blocks(source):
    source.rows[0]["pts"] = None
    metric = answer(source, "Compare Avery Example points home vs away 2024-25")[
        "semantic_evidence"
    ]["comparisons"][0]
    assert metric["difference"] is None
    assert metric["current"]["rows"][0]["valid_games"] == 2
    source.rows[0]["home_away"] = None
    with pytest.raises(SemanticError, match="Home/away"):
        answer(source, "Compare Avery Example points home vs away 2024-25")


@pytest.mark.parametrize(
    "suffix",
    [
        "in wins",
        "in the fourth quarter",
        "against the best defenses",
        "excluding injured games",
        "with at least 30 minutes",
        "on Tuesdays",
    ],
)
def test_no_unparsed_condition_disappears(source, suffix):
    with pytest.raises(SemanticError):
        answer(source, "Compare Avery Example points home vs away 2024-25 " + suffix)


def test_missing_trade_date_clarifies_without_guess(source):
    with pytest.raises(SemanticError, match="event date"):
        answer(source, "Compare Avery Example before and after the trade in 2024-25")


def test_ambiguous_identity_never_chooses(source):
    players = source_players(source)
    players.append(dict(player_id=202, player_name="Avery Other", aliases=["Avery"]))
    with pytest.raises(SemanticError, match="full names"):
        parse_split("Avery regular season vs playoff", players, "2024-25")


def test_integrated_route_and_metric_followup_no_model(source):
    warehouse = SimpleNamespace(load=lambda seasons: ({"capture": {}}, source))
    store = InMemoryConversationStore()
    agent = SemanticAsk(settings(), warehouse, store)
    result = agent.answer(
        "Avery Example regular season vs playoff 2024-25",
        client=None,
        model="none",
        conversation_id="split",
    )
    assert result["status"] == "ok"
    result = agent.answer(
        "What about assists?", client=None, model="none", conversation_id="split"
    )
    assert result["status"] == "ok"
    assert [
        r["current"]["metric"]["key"]
        for r in result["semantic_evidence"]["comparisons"]
    ] == ["ast"]
    assert result["semantic_plan"]["model_calls"] == 0


def test_service_home_away_uses_split_not_research(source):
    agent = StatsAgent(
        settings(),
        SimpleNamespace(),
        client=object(),
        semantic_warehouse=SimpleNamespace(
            load=lambda seasons: ({"capture": {}}, source)
        ),
    )
    result = agent.answer("Compare Avery Example points home vs away 2024-25")
    assert result["status"] == "ok"
    assert result["semantic_evidence"]["kind"] == "player_split"


@pytest.mark.parametrize(
    "question,expected",
    [
        ("Which is the best closing lineup?", "unsupported_scope"),
        ("Should I drop Avery Example in fantasy?", "clarification_required"),
        ("Compare on-court and off-court ratings", "unsupported_scope"),
    ],
)
def test_preflight_needs_no_source_or_model(question, expected):
    def fail(*args):
        raise AssertionError("Should not read warehouse")

    agent = SemanticAsk(settings(), SimpleNamespace(load=fail))
    result = agent.answer(question, client=None, model="none")
    assert result["status"] == expected
    assert result["semantic_evidence"] is None


def test_missing_phase_does_not_fall_back_or_pool(source):
    missing = replace(
        source,
        covered_scopes=frozenset({("2024-25", "Regular Season")}),
        rows=[r for r in source.rows if r["season_type"] == "Regular Season"],
        data_through={("2024-25", "Regular Season"): "2025-04-15"},
    )
    with pytest.raises(SemanticError, match="not covered"):
        answer(missing, "Avery Example regular season vs playoffs 2024-25")


def test_json_and_stream_endpoints_preserve_split_evidence(source):
    import json

    from app.main import app, get_agent_client, get_repository, get_settings
    from app.repository import BigQueryWarehouseRepository
    from fastapi.testclient import TestClient

    repo = BigQueryWarehouseRepository(settings(), client=SimpleNamespace())
    repo._governed_warehouse = SimpleNamespace(
        load=lambda seasons: ({"capture": {}}, source)
    )
    old = dict(app.dependency_overrides)
    app.dependency_overrides.update(
        {
            get_settings: settings,
            get_repository: lambda: repo,
            get_agent_client: lambda: object(),
        }
    )
    try:
        with TestClient(app) as client:
            body = {"question": "Compare Avery Example FG% home vs away 2024-25"}
            response = client.post("/api/agent/ask?season=2024-25", json=body)
            assert response.status_code == 200
            payload = response.json()
            followup = client.post(
                "/api/agent/ask?season=2024-25",
                json={
                    "question": "What about assists?",
                    "conversation_id": "recovered-split-context",
                    "previous_context": payload["conversation_context"],
                },
            )
            assert followup.status_code == 200
            continued = followup.json()
            assert continued["status"] == "ok"
            assert continued["conversation_context"]["metrics"] == ["ast"]
            assert continued["semantic_evidence"]["request"]["kind"] == "venue"
            stream = client.post("/api/agent/ask/stream?season=2024-25", json=body)
            assert stream.status_code == 200
            events = [
                json.loads(line[6:])
                for line in stream.text.splitlines()
                if line.startswith("data: ")
            ]
            final = next(e for e in events if e.get("type") == "final")
            result = final.get("payload", final.get("response", final))
            assert result["semantic_evidence"] == payload["semantic_evidence"]
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(old)
