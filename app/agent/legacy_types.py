"""Public legacy response/route data contracts; no routing heuristics."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class AgentRoute(str, Enum):
    RANKING = "ranking"
    PLAYER_TREND = "player_trend"
    GAME_LOG = "game_log"
    PERCENTILE = "percentile"
    SIMILARITY = "similarity"
    COMPARE = "compare"
    OVERVIEW = "overview"
    CLARIFY = "clarify"


class DateRange(BaseModel):
    start_date: str | None = None
    end_date: str | None = None


class AgentPlan(BaseModel):
    route: AgentRoute
    confidence: float = Field(ge=0, le=1)
    original_question: str
    player_names: list[str] = Field(default_factory=list)
    metric_queries: list[str] = Field(default_factory=list)
    limit: int | None = None
    min_games: int | None = None
    date_range: DateRange = Field(default_factory=DateRange)
    required_tools: list[str] = Field(default_factory=list)
    needs_clarification: bool = False
    clarification_question: str | None = None


ROUTE_TOOLS = {
    AgentRoute.RANKING: ["list_metrics", "search_rankings"],
    AgentRoute.PLAYER_TREND: ["resolve_player", "get_player_trends"],
    AgentRoute.GAME_LOG: ["resolve_player", "get_player_game_log"],
    AgentRoute.PERCENTILE: ["resolve_player", "calculate_player_percentile"],
    AgentRoute.SIMILARITY: ["resolve_player", "find_similar_players"],
    AgentRoute.COMPARE: ["resolve_player"],
    AgentRoute.OVERVIEW: ["resolve_player", "get_player_summary"],
    AgentRoute.CLARIFY: [],
}


class TimeWindow(BaseModel):
    kind: str = "unspecified"
    last_n_games: int | None = None
    last_n_weeks: int | None = None
    last_n_days: int | None = None
    start_date: str | None = None
    end_date: str | None = None
    granularity: str = "auto"
    comparison_mode: str = "previous_period"
    comparison_start_date: str | None = None
    comparison_end_date: str | None = None
    anchor_date: str | None = None


class QueryPlan(BaseModel):
    route: AgentRoute
    confidence: float = Field(ge=0, le=1)
    answer_depth: str = "normal"
    raw_player_mentions: list[str] = Field(default_factory=list)
    resolved_players: list[dict[str, Any]] = Field(default_factory=list)
    unresolved_players: list[str] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)
    min_games: int | None = Field(default=None, ge=1, le=200)
    time_window: TimeWindow = Field(default_factory=TimeWindow)
    tool_recipe: list[str] = Field(default_factory=list)
    needs_clarification: bool = False
    clarification_question: str | None = None
    clarification_options: list[dict[str, Any]] = Field(default_factory=list)
    opponent_breakdown: bool = False
    planner_source: str = "deterministic"

    def to_agent_plan(self, original_question: str) -> AgentPlan:
        required_tools = list(ROUTE_TOOLS[self.route])
        if (
            self.opponent_breakdown
            and self.route in {AgentRoute.PLAYER_TREND, AgentRoute.GAME_LOG}
            and "get_player_opponent_splits" not in required_tools
        ):
            required_tools.append("get_player_opponent_splits")
        return AgentPlan(
            route=self.route,
            confidence=self.confidence,
            original_question=original_question,
            player_names=self.raw_player_mentions,
            metric_queries=self.metrics,
            limit=self.time_window.last_n_games,
            min_games=self.min_games,
            date_range=DateRange(
                start_date=self.time_window.start_date,
                end_date=self.time_window.end_date,
            ),
            required_tools=required_tools,
            needs_clarification=self.needs_clarification,
            clarification_question=self.clarification_question,
        )
