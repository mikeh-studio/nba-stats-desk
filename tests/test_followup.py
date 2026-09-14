from dataclasses import asdict
from types import SimpleNamespace

import pytest
from app.agent.conversation import InMemoryConversationStore
from app.agent.followup import analysis_context, resolve_followup
from app.agent.semantic_answer import SemanticAsk
from app.agent.semantic_planner import explicit_scope
from app.agent.semantics import COMPONENTS, Evidence, Query


def context():
    return analysis_context(
        "Jalen Johnson performance",
        {
            "status": "ok",
            "answer": "Playmaking stands out.",
            "player_profile": {
                "player": {"player_id": 1, "player_name": "Jalen Johnson"}
            },
            "semantic_evidence": {
                "metrics": [{"key": "ast"}],
                "scope": {
                    "start": "2025-06-01",
                    "end": "2026-06-30",
                    "seasons": ["2024-25", "2025-26"],
                    "phases": ["Regular Season"],
                },
            },
        },
    )


def test_followup_inherits_context_without_pronoun_and_preserves_overrides():
    question, hints = resolve_followup(
        "Beside Johnson, who are the other top playmaking leads?", context()
    )
    assert "Beside Jalen Johnson" in question
    assert "from 2025-06-01 through 2026-06-30" in question
    assert hints["excluded_player_ids"] == [1]
    assert hints["seasons"] == ["2024-25", "2025-26"]
    assert "assists per game" in hints["assumption"]
    question, hints = resolve_followup(
        "Keldon Johnson last 5 games playoffs", context()
    )
    assert "Jalen" not in question
    assert "from 2025" not in question
    assert "Regular Season" not in question
    assert not hints
    assert analysis_context("clarify", {"status": "clarification_required"}) == {}


@pytest.mark.parametrize(
    "plan_kind", ["query", "duplicate_ranks", "rank_summary", "default_leaders"]
)
def test_followup_ranks_other_players_across_inherited_seasons_and_keeps_success_on_clarification(
    monkeypatch,
    plan_kind,
):
    rows = []
    for season, year in [("2024-25", 2025), ("2025-26", 2026)]:
        for player_id, name in [(1, "Jalen Johnson"), (2, "Other Player")]:
            for day in range(1, 7):
                rows.append(
                    {
                        **dict.fromkeys(COMPONENTS, 0),
                        "player_id": player_id,
                        "player_name": name,
                        "game_id": f"{year}-{day}",
                        "game_date": f"{year}-06-{day:02}",
                        "season": season,
                        "season_type": "Regular Season",
                        "team_abbr": "ATL",
                        "opponent_abbr": "BOS",
                        "ast": (10 if year == 2025 else 2)
                        + (2 if player_id == 2 else 0),
                    }
                )
    scopes = {(s, "Regular Season") for s in ("2024-25", "2025-26")}
    evidence = Evidence(
        rows,
        frozenset(scopes),
        "fixture",
        "followup",
        {s: f"{2025 if s[0] == '2024-25' else 2026}-06-30" for s in scopes},
        complete=True,
    )
    store = InMemoryConversationStore()
    store.append_turn(
        "chat",
        question="original",
        answer="successful",
        context={**context(), "browser_recovered": True},
        max_turns=6,
    )

    def load(seasons):
        assert seasons == ["2024-25", "2025-26"]
        return {"capture": {}}, evidence

    def planner(*args, **kwargs):
        assert plan_kind != "default_leaders", (
            "Supported leaderboard must not ask the model for defaults"
        )
        assert (
            kwargs["conversation_context"]["answer_summary"] == "Playmaking stands out."
        )
        assert "Jalen Johnson" in kwargs["question"]
        query = asdict(
            Query(
                metric="ast", season="2025-26", aggregation="average", operation="rank"
            )
        )
        query.pop("player_id")
        query["player_name"] = None
        query.update(explicit_scope(kwargs["question"]))
        other = query.copy()
        if plan_kind == "rank_summary":
            other.update(operation="summary", player_name="Jalen Johnson")
        return {
            "status": "query" if plan_kind == "query" else "compare",
            "queries": [query] if plan_kind == "query" else [query, other],
            "message": "",
            "model_calls": 0,
        }

    monkeypatch.setattr("app.agent.semantic_answer.plan_question", planner)
    if plan_kind != "default_leaders":
        monkeypatch.setattr(
            "app.agent.semantic_answer.contextual_leader_plan", lambda *args: None
        )
    else:
        store.set_pending_clarification(
            "chat", question="How many leaders?", query_plan=None
        )
    agent = SemanticAsk(
        SimpleNamespace(season="2025-26", agent_conversation_max_turns=6),
        SimpleNamespace(load=load),
        store,
    )
    result = agent.answer(
        "Beside Johnson, who are the other the other top playmaking leads",
        client=None,
        model="fixture",
        conversation_id="chat",
    )
    assert result["status"] == "ok"
    assert [r["player_id"] for r in result["semantic_evidence"]["rows"]] == [2]
    assert result["semantic_evidence"]["rows"][0]["value"] == 8
    assert "assists per game" in result["answer"]
    assert "Other Player: 8.0 assists per game" in result["answer"]
    assert "Jalen Johnson: 6.0 assists per game, league rank" in result["answer"]
    assert "2.0 ahead of Jalen Johnson" in result["answer"]
    assert result["tables"][0]["rows"][0][-1] == "+2.0"
    assert result["tables"][0]["reference_row_index"] == 1
    assert result["tables"][0]["rows"][-1] == [
        "Jalen Johnson",
        "6.0",
        "12",
        "12",
        "2",
        "0.0",
        "0.0",
    ]
    assert result["reference_evidence"]["scope"]["excluded_player_ids"] is None
    if plan_kind == "default_leaders":
        assert result["semantic_plan"]["model_calls"] == 0
        assert result["semantic_plan"]["queries"][0]["limit"] == 5
    successful = store.get_turns("chat", 6)
    monkeypatch.setattr(
        "app.agent.semantic_answer.plan_question",
        lambda *a, **k: {
            "status": "clarification_required",
            "queries": [],
            "message": "Which metric?",
            "model_calls": 0,
        },
    )
    result = agent.answer(
        "How about efficiency?", client=None, model="fixture", conversation_id="chat"
    )
    assert result["status"] == "clarification_required"
    assert store.get_turns("chat", 6) == successful
