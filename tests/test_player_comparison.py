from datetime import date
from types import SimpleNamespace

import pytest
from app.agent.conversation import InMemoryConversationStore
from app.agent.history import saved_context_question
from app.agent.player_comparison import (
    build_comparison,
    comparison_scope,
    comparison_sides,
)
from app.agent.semantic_answer import SemanticAsk
from app.agent.semantic_serving import source_players
from app.agent.semantics import COMPONENTS, Evidence, SemanticError


@pytest.fixture
def evidence():
    rows = []
    for player, name in [
        (1, "Jalen Brunson"),
        (2, "Victor Wembanyama"),
        (3, "Player Three"),
    ]:
        for day in range(1, 7):
            rows.append(
                {
                    **dict.fromkeys(COMPONENTS, 0),
                    "season": "2025-26",
                    "season_type": "Playoffs",
                    "game_id": f"p{day}",
                    "game_date": f"2026-05-{day:02}",
                    "player_id": player,
                    "player_name": name,
                    "team_abbr": "NYK" if player == 1 else "SAS",
                    "opponent_abbr": "BOS",
                    "pts": 30 - player,
                    "reb": player * 3,
                    "ast": 10 - player,
                    "stl": player,
                    "blk": player,
                    "min": 30,
                }
            )
    return Evidence(
        rows,
        frozenset({("2025-26", "Playoffs")}),
        "fixture",
        "comparison-test",
        {("2025-26", "Playoffs"): "2026-05-06"},
        complete=True,
    )


def scope():
    return comparison_scope(
        "Jalen Brunson vs Wemby. Who had a better playoff this season",
        "2025-26",
        date(2026, 9, 12),
    )


def test_same_scope_all_metrics_aliases_and_no_invented_impact(evidence):
    result = build_comparison(
        "Jalen Brunson vs Wemby. Who had a better playoff this season",
        evidence,
        source_players(evidence),
        scope(),
    )
    assert result["status"] == "ok"
    data = result["semantic_evidence"]
    assert data["scope"]["seasons"] == ["2025-26"]
    assert data["scope"]["phases"] == ["Playoffs"]
    assert [m["key"] for m in data["metrics"]] == ["pts", "reb", "ast", "stl", "blk"]
    assert data["metrics"][0]["delta"] == 1
    assert data["metrics"][0]["left"]["percentile"] == 100
    assert data["metrics"][0]["right"]["percentile"] == 50
    assert all(m["left"] is None and m["right"] is None for m in data["impact"])
    assert result["player_profiles"][0]["minutes"] == 180
    assert result["player_profiles"][0]["minutes_per_game"] == 30
    assert "from 2025-07-01 through 2026-06-30" in saved_context_question(
        "relative question", result
    )


def test_missing_partial_and_zero_data(evidence):
    evidence.rows[0]["pts"] = None
    result = build_comparison(
        "Jalen Brunson vs Wemby playoffs", evidence, source_players(evidence), scope()
    )
    points = result["semantic_evidence"]["metrics"][0]
    assert points["delta"] is None
    assert points["left"]["percentile"] is None
    assert points["left"]["valid_games"] == 5
    for row in evidence.rows:
        row["stl"] = 0
    result = build_comparison(
        "Jalen Brunson vs Wemby playoffs", evidence, source_players(evidence), scope()
    )
    assert result["semantic_evidence"]["metrics"][3]["delta"] == 0


def test_ambiguous_side_does_not_collapse_both_players(evidence):
    players = source_players(evidence)
    players[0]["aliases"].append("Jalen")
    players[2]["aliases"].append("Jalen")
    question = "Jalen vs Wemby playoffs"
    result = build_comparison(question, evidence, players, scope())
    assert result["status"] == "clarification_required"
    assert len(result["clarification_options"]) == 2
    resolved = build_comparison(question, evidence, players, scope(), {"player_id": 1})
    assert [p["player"]["player_id"] for p in resolved["player_profiles"]] == [1, 2]
    with pytest.raises(SemanticError):
        build_comparison(
            "Wemby vs Victor Wembanyama playoffs", evidence, players, scope()
        )


def test_no_appearance_is_not_a_loss(evidence):
    current = scope()
    current["start"], current["end"] = date(2026, 6, 1), date(2026, 6, 30)
    result = build_comparison(
        "Jalen Brunson vs Wemby playoffs", evidence, source_players(evidence), current
    )
    assert result["status"] == "no_observations"
    assert all(m["delta"] is None for m in result["semantic_evidence"]["metrics"])
    assert "no recorded appearances" in result["answer"]


def test_scope_guard_and_period_comparison_keeps_old_route():
    assert comparison_sides("Jalen Brunson last 10 games vs previous 10") is None
    assert comparison_sides("Jalen Brunson vs. Wemby")
    for phrase in ("last 10 games", "Finals", "per 48", "home", "in wins"):
        with pytest.raises(SemanticError):
            comparison_scope(f"Jalen Brunson vs Wemby {phrase}", "2025-26")
    explicit = comparison_scope(
        "Jalen Brunson vs Wemby 2025 playoffs", "2025-26", date(2026, 9, 12)
    )
    assert explicit["seasons"] == ["2024-25"]


@pytest.mark.parametrize("separator", ["with", "and", "vs", "vs.", "versus"])
@pytest.mark.parametrize(
    "baseline", ["his previous 5 games", "the prior 5 games", "last 5 games", "2024-25"]
)
def test_period_baselines_never_select_two_player_route(separator, baseline):
    assert (
        comparison_sides(f"Compare Jalen Brunson’s last 5 games {separator} {baseline}")
        is None
    )


@pytest.mark.parametrize(
    "question",
    [
        "Compare Jalen Brunson with Wemby",
        "Compare Jalen Brunson and Victor Wembanyama playoffs",
        "Jalen Brunson versus Wemby",
    ],
)
def test_explicit_two_player_wordings_keep_scorecard_route(question):
    assert len(comparison_sides(question)) == 2


def test_sequential_identity_choices_are_retained(evidence):
    players = source_players(evidence)
    for player in players:
        player["aliases"].extend(["First", "Second"])
    question = "First vs Second playoffs"
    first = build_comparison(question, evidence, players, scope(), {"player_id": 1})
    assert first["status"] == "clarification_required"
    assert first["comparison_choices"] == {"0": 1}
    resolved = build_comparison(
        question, evidence, players, scope(), {"player_id": 2}, {"0": 1}
    )
    assert [p["player"]["player_id"] for p in resolved["player_profiles"]] == [1, 2]


def test_service_uses_no_model_and_saves_both_players(evidence, monkeypatch):
    from app.agent import semantic_answer

    monkeypatch.setattr(semantic_answer, "comparison_scope", lambda *a: scope())
    store = InMemoryConversationStore()

    class Warehouse:
        def load(self, seasons):
            assert seasons == ["2025-26"]
            return {"capture": {}}, evidence

    agent = SemanticAsk(
        SimpleNamespace(season="2025-26", agent_conversation_max_turns=6),
        Warehouse(),
        store,
    )
    result = agent.answer(
        "Jalen Brunson vs Wemby. Who had a better playoff this season",
        client=None,
        model="unused",
        conversation_id="compare",
    )
    assert result["status"] == "ok"
    assert result["semantic_plan"]["model_calls"] == 0
    assert "Victor Wembanyama" in store.get_turns("compare", max_turns=1)[0].question
