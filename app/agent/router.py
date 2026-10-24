from __future__ import annotations

import re

from app.agent.legacy_types import ROUTE_TOOLS as ROUTE_TOOLS
from app.agent.legacy_types import AgentPlan as AgentPlan
from app.agent.legacy_types import AgentRoute as AgentRoute
from app.agent.legacy_types import DateRange as DateRange

_METRIC_TERMS = {
    "assist",
    "assists",
    "ast",
    "point",
    "points",
    "pts",
    "rebound",
    "rebounds",
    "reb",
    "steal",
    "steals",
    "block",
    "blocks",
    "turnover",
    "turnovers",
    "attributed",
    "created",
}

_ROUTE_KEYWORDS: dict[AgentRoute, set[str]] = {
    AgentRoute.SIMILARITY: {"similar", "similarity", "alike", "nearest"},
    AgentRoute.PERCENTILE: {"percentile", "percentille", "rank percentile"},
    AgentRoute.GAME_LOG: {"game-by-game", "game by game", "game log", "box score"},
    AgentRoute.RANKING: {"top", "leaders", "leaderboard", "rankings", "best"},
    AgentRoute.PLAYER_TREND: {
        "trend",
        "trending",
        "recent",
        "last",
        "form",
        "baseline",
        "league average",
        "league baseline",
    },
    AgentRoute.COMPARE: {"compare", " vs ", " versus ", "head to head"},
}


def _contains_any(text: str, terms: set[str]) -> bool:
    return any(term in text for term in terms)


def _has_metric_context(text: str) -> bool:
    return _contains_any(text, _METRIC_TERMS)


def _looks_like_two_player_compare(text: str) -> bool:
    if "league average" in text or "league baseline" in text or "baseline" in text:
        return False
    if " vs " in text or " versus " in text:
        return True
    if "compare" not in text and "against" not in text:
        return False
    if " against " in text and ("league" in text or "baseline" in text):
        return False
    # A rough local heuristic: compare questions that name "and" or "with"
    # are usually two-player comparisons; metric-baseline comparisons are trends.
    return bool(re.search(r"\b(compare|against)\b.+\b(and|with|vs|versus)\b", text))


def _score_route(question: str, route: AgentRoute) -> float:
    q = question.casefold()
    score = 0.0
    for keyword in _ROUTE_KEYWORDS.get(route, set()):
        if keyword in q:
            score += 0.25
    if route in {AgentRoute.PLAYER_TREND, AgentRoute.RANKING, AgentRoute.PERCENTILE}:
        if _has_metric_context(q):
            score += 0.15
    if route == AgentRoute.COMPARE and _looks_like_two_player_compare(q):
        score += 0.35
    if route == AgentRoute.PLAYER_TREND and (
        "league average" in q or "league baseline" in q or "against baseline" in q
    ):
        score += 0.35
    if route == AgentRoute.GAME_LOG and re.search(r"\b20\d{2}-\d{2}-\d{2}\b", q):
        score += 0.45
        if _has_metric_context(q):
            score += 0.15
    return min(score, 0.98)


def build_agent_plan(question: str) -> AgentPlan:
    cleaned = question.strip()
    q = cleaned.casefold()
    if len(q.split()) < 3 or q in {"help", "stats", "nba", "what can you do"}:
        return AgentPlan(
            route=AgentRoute.CLARIFY,
            confidence=0.95,
            original_question=question,
            required_tools=ROUTE_TOOLS[AgentRoute.CLARIFY],
            needs_clarification=True,
            clarification_question=(
                "Which player, metric, or comparison do you want to analyze?"
            ),
        )

    scores = {
        route: _score_route(cleaned, route)
        for route in (
            AgentRoute.SIMILARITY,
            AgentRoute.PERCENTILE,
            AgentRoute.GAME_LOG,
            AgentRoute.RANKING,
            AgentRoute.PLAYER_TREND,
            AgentRoute.COMPARE,
        )
    }
    if "compare" in q and not _looks_like_two_player_compare(q):
        scores[AgentRoute.PLAYER_TREND] = max(scores[AgentRoute.PLAYER_TREND], 0.7)
        scores[AgentRoute.COMPARE] = min(scores[AgentRoute.COMPARE], 0.35)
    route = max(scores, key=lambda scored_route: scores[scored_route])
    confidence = scores[route]
    if confidence < 0.2:
        route = AgentRoute.OVERVIEW
        confidence = 0.45

    return AgentPlan(
        route=route,
        confidence=round(confidence, 2),
        original_question=question,
        required_tools=ROUTE_TOOLS[route],
    )
