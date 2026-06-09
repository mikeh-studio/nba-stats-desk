import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.router import ROUTE_TOOLS, AgentRoute, build_agent_plan
from app.agent.tools import get_tool_schemas


def test_routes_ranking_question() -> None:
    plan = build_agent_plan(
        "Who are the top 5 players by assists in the Western conferences?"
    )
    assert plan.route == AgentRoute.RANKING
    assert plan.original_question == (
        "Who are the top 5 players by assists in the Western conferences?"
    )
    assert "search_rankings" in plan.required_tools
    assert 0 < plan.confidence <= 1


def test_routes_percentile_question() -> None:
    plan = build_agent_plan(
        "Which percentile is LeBron James' points attributed in the league?"
    )
    assert plan.route == AgentRoute.PERCENTILE
    assert plan.required_tools == ["resolve_player", "calculate_player_percentile"]
    assert "resolve_player" in plan.required_tools


def test_routes_similarity_question() -> None:
    plan = build_agent_plan("Who is Keldon Johnson's most similar player?")
    assert plan.route == AgentRoute.SIMILARITY
    assert plan.required_tools == ["resolve_player", "find_similar_players"]


def test_routes_compare_against_league_baseline_as_trend() -> None:
    plan = build_agent_plan(
        "Compare LeBron's last 10 games against league baseline for points."
    )

    assert plan.route == AgentRoute.PLAYER_TREND
    assert plan.confidence > 0.5
    assert plan.required_tools == ["resolve_player", "get_player_trends"]


def test_routes_two_player_compare_as_compare() -> None:
    plan = build_agent_plan("Compare LeBron James and Kevin Durant.")

    assert plan.route == AgentRoute.COMPARE
    assert plan.confidence > 0.3


def test_routes_under_specified_question_as_clarify() -> None:
    plan = build_agent_plan("stats")

    assert plan.route == AgentRoute.CLARIFY
    assert plan.needs_clarification is True
    assert plan.required_tools == []


def test_route_tools_are_registered_tool_names() -> None:
    registered_tool_names = {schema["name"] for schema in get_tool_schemas()}

    for tool_names in ROUTE_TOOLS.values():
        for tool_name in tool_names:
            assert tool_name in registered_tool_names
