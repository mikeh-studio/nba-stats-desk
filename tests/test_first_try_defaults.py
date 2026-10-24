"""First-try answers: real statistics by default, broad defaults instead of clarification."""

import copy
import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from app.agent.clock import semantic_date
from app.agent.performance_overview import wants_overview
from app.agent.player_comparison import comparison_sides
from app.agent.semantic_answer import PLANNING_FAILURE, user_message
from app.agent.semantic_planner import (
    leader_game_floor,
    league_leader_plan,
    single_period_rank_plan,
)
from scripts.ask_eval.fixtures import load_inputs
from tests.test_semantic_migration import settings

SOURCE, _ = load_inputs(Path(__file__).resolve().parents[1])


@pytest.fixture
def on_date():
    tokens = []

    def set_date(value):
        tokens.append(semantic_date.set(value))

    yield set_date
    for token in reversed(tokens):
        semantic_date.reset(token)


def no_model():
    def create(**kwargs):
        raise AssertionError("The model planner must not be called")

    return SimpleNamespace(responses=SimpleNamespace(create=create))


def ask(question, client=None):
    from app.agent.conversation import InMemoryConversationStore
    from app.agent.semantic_answer import SemanticAsk
    from scripts.ask_eval.fixtures import FrozenWarehouse

    service = SemanticAsk(
        settings(),
        FrozenWarehouse(copy.deepcopy(SOURCE), "complete"),
        InMemoryConversationStore(),
    )
    return service.answer(question, client=client or no_model(), model="test")


@pytest.mark.parametrize(
    "question,sides",
    [
        (
            "Who played better last season: Austin Reeves or Luka Doncic",
            ["Austin Reeves", "Luka Doncic"],
        ),
        (
            "Who's better, LeBron James or Kevin Durant?",
            ["LeBron James", "Kevin Durant"],
        ),
        (
            "Who was better last season between Jalen Brunson and Tyrese Haliburton?",
            ["Jalen Brunson", "Tyrese Haliburton"],
        ),
        (
            "LeBron James or Kevin Durant: who played better this year?",
            ["LeBron James", "Kevin Durant"],
        ),
        ("Is Austin Reaves better than Luka Doncic?", ["Austin Reaves", "Luka Doncic"]),
        ("Who had the better season, Jokic or Embiid?", ["Jokic", "Embiid"]),
        # A named statistic stays with the metric planner.
        ("Who scored more last season, LeBron James or Kevin Durant?", None),
        ("Who is the best scorer in the league?", None),
    ],
)
def test_evaluative_two_player_questions_use_the_scorecard(question, sides):
    assert comparison_sides(question) == sides


@pytest.mark.parametrize(
    "question,expected",
    [
        ("How was Curry this season?", True),
        ("How good was Jokic last year?", True),
        ("How has Jalen Johnson been this season?", True),
        ("What kind of season did Draymond Green have?", True),
        ("Tell me about Draymond Green's season", True),
        ("How many points did Curry score last season?", False),
        ("How was Curry's 3P% last season?", False),
        ("How long was Curry out?", False),
    ],
)
def test_unnamed_statistic_defaults_to_full_overview(question, expected):
    assert wants_overview(question) is expected


@pytest.mark.parametrize(
    "question,metric,phase",
    [
        ("Who led the league in scoring 2025-26", "pts", "Regular Season"),
        ("Who led the league is scoring 2025-26", "pts", "Regular Season"),
        ("Who led the NBA in rebounds in the 2025-26 season?", "reb", "Regular Season"),
        ("Who leads the league in assists", "ast", "Regular Season"),
        ("Who led the league in three pointers 2025-26 playoffs", "fg3m", "Playoffs"),
    ],
)
def test_league_leader_is_planned_without_the_model(question, metric, phase):
    plan = league_leader_plan(question, "2025-26")
    query = plan["queries"][0]
    assert plan["model_calls"] == 0
    assert (query["metric"], query["season_type"]) == (metric, phase)
    assert (query["operation"], query["aggregation"]) == ("rank", "average")
    # Regular-season leaders use the NBA's share-of-team-games standard.
    assert plan.get("leader_game_share") == (0.7 if phase == "Regular Season" else None)


@pytest.mark.parametrize(
    "question",
    [
        "Who led the league in scoring among guards 2025-26",
        "Who led the league in scoring 2024-25",
        "Who led the league in scoring at home 2025-26",
    ],
)
def test_league_leader_defers_anything_beyond_season_and_phase(question):
    assert league_leader_plan(question, "2025-26") is None


@pytest.mark.parametrize("team_games,floor", [(82, 58), (40, 28), (5, 4), (1, 1)])
def test_leader_floor_is_seventy_percent_of_team_games(team_games, floor):
    rows = [
        {
            "season": "2025-26",
            "season_type": "Regular Season",
            "team_abbr": "AAA",
            "game_id": str(i),
        }
        for i in range(team_games)
    ] + [
        {
            "season": "2024-25",
            "season_type": "Regular Season",
            "team_abbr": "AAA",
            "game_id": "x",
        }
    ]
    args = {"season": "2025-26", "season_type": "Regular Season"}
    assert leader_game_floor(rows, args, 0.7) == floor


def rank(season):
    return dict(
        metric="pts",
        season=season,
        aggregation="average",
        operation="rank",
        season_type="Regular Season",
        player_name=None,
    )


def test_one_period_leaderboard_is_not_a_season_comparison():
    plan = {"status": "compare", "queries": [rank("2024-25"), rank("2025-26")]}
    collapsed = single_period_rank_plan(
        plan, "Who led the league in scoring 2025-26", "2025-26"
    )
    assert collapsed["status"] == "query"
    assert collapsed["queries"] == [rank("2025-26")]
    # Explicit comparison wording keeps both sides.
    assert (
        single_period_rank_plan(
            plan, "Compare scoring leaders in 2024-25 versus 2025-26", "2025-26"
        )
        is plan
    )


def test_internal_planner_errors_are_not_shown_to_users():
    assert (
        user_message(
            "Comparison requires compatible individual summaries", "invalid_scope"
        )
        == PLANNING_FAILURE
    )
    assert user_message("Venue must be HOME or AWAY", "invalid_scope") == (
        "Venue must be HOME or AWAY"
    )


@pytest.mark.usefixtures("scripted_intelligence")
def test_relative_season_reaches_the_planner_once(on_date):
    on_date(date(2025, 8, 1))
    seen = []

    def create(**kwargs):
        seen.append(json.loads(kwargs["input"][0]["content"]))
        return SimpleNamespace(
            output_text=json.dumps(
                {
                    "status": "query",
                    "message": "",
                    "queries": [
                        {
                            **rank("2024-25"),
                            "metric": "ast",
                            "direction": "higher",
                            "window": "season_to_date",
                            "limit": 10,
                        }
                    ],
                }
            )
        )

    ask(
        "Which player had the most assists per game last season?",
        SimpleNamespace(responses=SimpleNamespace(create=create)),
    )
    assert seen, "planner was not called"
    for field in ("question", "current_question"):
        assert "last season" not in seen[0][field]
        assert "2024-25" in seen[0][field]
    assert seen[0]["selected_season"] == "2024-25"


def test_who_played_better_returns_a_scorecard_without_the_model():
    result = ask("Who played better in 2024-25: Avery Example or Blair Sample?")
    assert result["status"] == "ok", result["answer"]
    assert result["semantic_evidence"]["kind"] == "player_comparison"


def test_scoring_leader_answers_without_the_model(on_date):
    on_date(date(2025, 3, 1))
    result = ask("Who led the league in scoring in 2024-25?")
    assert result["status"] == "ok", result["answer"]
    assert result["semantic_plan"]["model_calls"] == 0
    assert result["semantic_plan"]["queries"][0]["operation"] == "rank"


def test_leaderboard_answer_names_the_leader(on_date):
    on_date(date(2025, 3, 1))
    result = ask("Who led the league in scoring in 2024-25?")
    assert "ranked first with" in result["answer"]
    assert "players qualified (minimum" in result["answer"]
