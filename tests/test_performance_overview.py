import json
from datetime import date
from types import SimpleNamespace

import pytest
from app.agent.conversation import InMemoryConversationStore
from app.agent.performance_overview import (
    build_overview,
    overview_scope,
    resolve_overview_player,
    wants_overview,
)
from app.agent.semantic_answer import SemanticAsk
from app.agent.semantic_serving import source_players
from app.agent.semantics import COMPONENTS, Evidence, SemanticError


@pytest.fixture
def evidence():
    rows = []
    for season, year, multiplier in (("2024-25", 2024, 1), ("2025-26", 2025, 2)):
        for player, scale in ((1, 2), (2, 1), (3, 3)):
            for day in range(1, 7):
                rows.append(
                    {
                        **dict.fromkeys(COMPONENTS, 0),
                        "season": season,
                        "season_type": "Regular Season" if day < 6 else "Playoffs",
                        "game_id": f"{year}{day}",
                        "game_date": f"{year}-11-{day:02}",
                        "player_id": player,
                        "player_name": f"Player {player}",
                        "team_abbr": {1: "ATL", 2: "BOS", 3: "CHI"}[player],
                        "opponent_abbr": "NYK",
                        "pts": scale * multiplier * 5,
                        "reb": scale * multiplier,
                        "ast": scale * multiplier,
                        "stl": scale,
                        "blk": scale,
                    }
                )
    covered = frozenset(
        (s, p) for s in ("2024-25", "2025-26") for p in ("Regular Season", "Playoffs")
    )
    return Evidence(
        rows,
        covered,
        "fixture",
        "overview-test",
        {s: "2026-06-01" for s in covered},
        complete=True,
    )


def scope():
    return overview_scope(
        "performance the past 12 months", "2025-26", date(2026, 9, 12)
    )


def test_calendar_scope_includes_baseline_seasons_and_both_phases():
    s = scope()
    assert (s["start"], s["end"]) == (date(2025, 9, 13), date(2026, 9, 12))
    assert (s["previous_start"], s["previous_end"]) == (
        date(2024, 9, 13),
        date(2025, 9, 12),
    )
    assert set(s["seasons"]) == {"2024-25", "2025-26"}
    assert s["phases"] == ["Regular Season", "Playoffs"]


def test_overview_math_table_charts_profile_agree(evidence):
    result = build_overview(
        "Player 1 performance past 12 months",
        evidence,
        source_players(evidence),
        scope(),
    )
    assert result["status"] == "ok"
    assert [r[0] for r in result["tables"][0]["rows"]] == [
        "Points",
        "Rebounds",
        "Assists",
        "Steals",
        "Blocks",
    ]
    assert result["tables"][0]["rows"][0] == [
        "Points",
        "20.0",
        "50",
        "+10.0",
        "6 / 6",
        "3",
    ]
    assert len(result["charts"]) == 5
    assert result["charts"][0]["series"][0]["points"][0]["y"] == 20
    assert "Points: 20.0 per game (50th percentile), +10.0" in result["answer"]
    assert result["player_profile"]["player"]["headshot_url"].endswith("/1.png")


def test_missing_values_are_not_zero_or_qualified(evidence):
    next(r for r in evidence.rows if r["player_id"] == 1 and r["season"] == "2025-26")[
        "stl"
    ] = None
    result = build_overview(
        "Player 1 performance", evidence, source_players(evidence), scope()
    )
    row = result["tables"][0]["rows"][3]
    assert row[1:5] == ["2.0", "Unavailable", "Unavailable", "5 / 6"]
    assert len(result["charts"]) == 4
    assert "Partial data for Steals" in result["answer"]


def test_duplicate_names_require_selection_or_chat(evidence):
    players = source_players(evidence)
    for p in players[:2]:
        p["player_name"] = "Same Name"
        p["aliases"] = ["Same Name"]
    first = build_overview("Same Name performance", evidence, players, scope())
    assert first["status"] == "clarification_required"
    assert first["player_profile"] is None
    assert len(first["clarification_options"]) == 2
    assert len({p["label"] for p in first["clarification_options"]}) == 2
    for reply in ("ATL", "1"):
        result = build_overview(
            "Same Name performance\nClarification: " + reply, evidence, players, scope()
        )
        assert result["semantic_evidence"]["player_id"] == 1
    result = build_overview(
        "Same Name performance", evidence, players, scope(), {"player_id": 2}
    )
    assert result["semantic_evidence"]["player_id"] == 2
    assert (
        build_overview(
            "Same Name performance", evidence, players, scope(), {"player_id": 3}
        )["status"]
        == "clarification_required"
    )


def test_two_different_named_players_are_not_silently_collapsed(evidence):
    players = source_players(evidence)
    players[0]["aliases"] = ["A Very Long Name"]
    assert (
        len(
            resolve_overview_player(
                "A Very Long Name and Player 2 performance", players, evidence.rows
            )
        )
        == 2
    )


@pytest.mark.parametrize(
    "question",
    [
        "Player 1 points past 12 months",
        "Player 1 fantasy performance",
        "Compare Player 1 stats against Player 2",
    ],
)
def test_specific_questions_keep_existing_route(question):
    assert not wants_overview(question)


def test_scope_does_not_substitute_last_games_or_unavailable_archives():
    with pytest.raises(SemanticError):
        overview_scope("performance last 10 games", "2025-26")
    with pytest.raises(SemanticError):
        overview_scope("performance past 100 months", "2025-26")


@pytest.mark.parametrize(
    "selection", [None, {"player_id": 1, "player_name": "Same Name"}]
)
def test_conversation_resumes_original_dates_after_clarification(
    evidence, monkeypatch, selection
):
    from app.agent import semantic_answer

    monkeypatch.setattr(semantic_answer, "overview_scope", lambda *args: scope())
    for r in evidence.rows:
        if r["player_id"] in (1, 2):
            r["player_name"] = "Same Name"
    calls = []

    class Warehouse:
        def load(self, seasons):
            calls.append(seasons)
            return {"capture": {}}, evidence

    store = InMemoryConversationStore()
    agent = SemanticAsk(
        SimpleNamespace(season="2025-26", agent_conversation_max_turns=6),
        Warehouse(),
        store,
    )
    first = agent.answer(
        "Same Name performance past 12 months",
        client=None,
        model="test",
        conversation_id="test",
    )
    assert first["status"] == "clarification_required"
    second = agent.answer(
        "ATL",
        client=None,
        model="test",
        conversation_id="test",
        selected_player=selection,
    )
    assert second["semantic_evidence"]["player_id"] == 1
    assert second["semantic_evidence"]["scope"]["start"] == "2025-09-13"
    assert store.get_pending_clarification("test") is None
    assert len(calls) == 2


@pytest.mark.parametrize(
    "selection", [None, {"player_id": 1, "player_name": "Same Name"}]
)
def test_specific_metric_duplicate_name_can_resume(evidence, selection):
    for row in evidence.rows:
        if row["player_id"] in (1, 2):
            row["player_name"] = "Same Name"

    class Warehouse:
        def load(self, seasons):
            return {"capture": {}}, evidence

    def create(**kwargs):
        return SimpleNamespace(
            output_text=json.dumps(
                {
                    "status": "query",
                    "message": "",
                    "queries": [
                        {
                            "metric": "pts",
                            "aggregation": "average",
                            "season": "2025-26",
                            "player_name": "resolved_player_1",
                        }
                    ],
                }
            )
        )

    client = SimpleNamespace(responses=SimpleNamespace(create=create))
    agent = SemanticAsk(
        SimpleNamespace(season="2025-26", agent_conversation_max_turns=6),
        Warehouse(),
        InMemoryConversationStore(),
    )
    first = agent.answer(
        "Same Name points", client=client, model="test", conversation_id="metric"
    )
    assert first["status"] == "clarification_required"
    assert len(first["clarification_options"]) == 2
    result = agent.answer(
        "ATL",
        client=client,
        model="test",
        conversation_id="metric",
        selected_player=selection,
    )
    assert result["status"] == "ok"
    assert result["player_profile"]["player"]["player_id"] == 1
