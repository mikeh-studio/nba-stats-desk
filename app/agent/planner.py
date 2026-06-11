from __future__ import annotations

import json
import logging
import re
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from app.agent.catalog import load_semantic_catalog
from app.agent.player_resolver import load_player_aliases, normalize_player_text
from app.agent.router import (
    ROUTE_TOOLS,
    AgentPlan,
    AgentRoute,
    DateRange,
    build_agent_plan,
)
from app.config import Settings

logger = logging.getLogger("nba.agent.planner")


class TimeWindow(BaseModel):
    kind: str = "unspecified"
    last_n_games: int | None = None
    start_date: str | None = None
    end_date: str | None = None


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
    planner_source: str = "deterministic"

    def to_agent_plan(self, original_question: str) -> AgentPlan:
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
            required_tools=ROUTE_TOOLS[self.route],
            needs_clarification=self.needs_clarification,
            clarification_question=self.clarification_question,
        )


QUERY_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "route": {
            "type": "string",
            "enum": [route.value for route in AgentRoute],
        },
        "confidence": {"type": "number"},
        "answer_depth": {"type": "string", "enum": ["quick", "normal", "deep"]},
        "raw_player_mentions": {"type": "array", "items": {"type": "string"}},
        "metrics": {"type": "array", "items": {"type": "string"}},
        "min_games": {"type": ["integer", "null"], "minimum": 1, "maximum": 200},
        "time_window": {
            "type": "object",
            "properties": {
                "kind": {"type": "string"},
                "last_n_games": {"type": ["integer", "null"]},
                "start_date": {"type": ["string", "null"]},
                "end_date": {"type": ["string", "null"]},
            },
            "required": ["kind", "last_n_games", "start_date", "end_date"],
            "additionalProperties": False,
        },
        "needs_clarification": {"type": "boolean"},
        "clarification_question": {"type": ["string", "null"]},
    },
    "required": [
        "route",
        "confidence",
        "answer_depth",
        "raw_player_mentions",
        "metrics",
        "min_games",
        "time_window",
        "needs_clarification",
        "clarification_question",
    ],
    "additionalProperties": False,
}

QUERY_PLAN_TEXT_FORMAT = {
    "format": {
        "type": "json_schema",
        "name": "nba_stats_query_plan",
        "schema": QUERY_PLAN_SCHEMA,
        "strict": True,
    }
}

PLANNER_PROMPT = """
You are a query planner for an NBA stats agent.
Return only the requested JSON schema.
Classify the user's intent, extract NBA player mentions exactly as written,
extract metric words, and identify date ranges or last-N-game windows.
Extract minimum-games cohort filters such as "at least 50 games" into min_games.
Use player_trend for one-player questions comparing a player to league average
or baseline. Use compare only for two-player comparisons.
If the user did not provide enough player/metric/comparison detail, mark
needs_clarification true.
""".strip()


def _extract_time_window(question: str) -> TimeWindow:
    q = question.casefold()
    last_match = re.search(r"\blast\s+(\d{1,2})\s+games?\b", q)
    dates = re.findall(r"\b20\d{2}-\d{2}-\d{2}\b", question)
    if dates:
        return TimeWindow(
            kind="date_range",
            start_date=dates[0],
            end_date=dates[1] if len(dates) > 1 else None,
        )
    if last_match:
        return TimeWindow(kind="last_n_games", last_n_games=int(last_match.group(1)))
    if any(term in q for term in ("recent", "trend", "trending", "form")):
        return TimeWindow(kind="recent")
    return TimeWindow()


def _extract_metrics(question: str) -> list[str]:
    catalog = load_semantic_catalog()
    found: list[str] = []
    q = normalize_player_text(question)
    for metric in catalog.list_metrics():
        names = [metric["key"], metric["label"], *metric.get("aliases", [])]
        if any(normalize_player_text(str(name)) in q for name in names):
            key = str(metric["key"])
            if key not in found:
                found.append(key)
    return found


def _extract_min_games(question: str) -> int | None:
    q = question.casefold()
    patterns = (
        r"\b(?:at\s+least|minimum|min)\s+(\d{1,3})\s+games?\b",
        r"\b(\d{1,3})\+\s+games?\b",
    )
    for pattern in patterns:
        match = re.search(pattern, q)
        if match:
            return max(1, min(200, int(match.group(1))))
    return None


# Capitalized words that show up in stats questions but are never player
# names. A capitalized phrase is skipped only when every word is listed here,
# so real names ("Jalen Williams") survive even if a word overlaps.
_MENTION_STOPWORDS = frozenset(
    {
        # Question/sentence-leading words.
        "who", "what", "show", "compare", "which", "how", "follow",
        "previous", "question", "tell", "give", "list", "find", "rank",
        # League and cohort words.
        "nba", "league", "average", "baseline", "eastern", "western",
        "conference", "regular", "season", "playoffs", "team", "teams",
        "player", "players", "game", "games", "top", "best", "leaders",
        # Metric words.
        "points", "assists", "rebounds", "steals", "blocks", "turnovers",
        "percentile", "trend", "trends", "stats", "minutes",
        # Time words.
        "january", "february", "march", "april", "may", "june", "july",
        "august", "september", "october", "november", "december",
        "monday", "tuesday", "wednesday", "thursday", "friday",
        "saturday", "sunday", "last", "recent", "this", "today",
        "yesterday", "week", "month",
    }
)  # fmt: skip


def _is_stopword_phrase(phrase: str) -> bool:
    words = phrase.casefold().split()
    return bool(words) and all(word in _MENTION_STOPWORDS for word in words)


def _extract_player_mentions(question: str) -> list[str]:
    mentions: list[str] = []
    q_norm = normalize_player_text(question)
    for alias_norm, canonical in load_player_aliases().items():
        if re.search(rf"\b{re.escape(alias_norm)}\b", q_norm):
            mention = (
                canonical
                if alias_norm != normalize_player_text(canonical)
                else alias_norm
            )
            if all(
                normalize_player_text(existing) != normalize_player_text(mention)
                for existing in mentions
            ):
                mentions.append(mention)
    for match in re.finditer(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2}\b", question):
        phrase = match.group(0).strip()
        # Every false mention costs a player-search warehouse query, so filter
        # capitalized phrases made up entirely of non-name words.
        if _is_stopword_phrase(phrase):
            continue
        phrase_norm = normalize_player_text(phrase)
        replaced = False
        for index, existing in enumerate(mentions):
            if normalize_player_text(existing) == phrase_norm:
                mentions[index] = phrase
                replaced = True
                break
        if not replaced:
            mentions.append(phrase)
    return mentions[:4]


def deterministic_query_plan(question: str) -> QueryPlan:
    agent_plan = build_agent_plan(question)
    route = agent_plan.route
    mentions = _extract_player_mentions(question)
    metrics = _extract_metrics(question)
    if (
        route
        in {
            AgentRoute.PLAYER_TREND,
            AgentRoute.GAME_LOG,
            AgentRoute.PERCENTILE,
            AgentRoute.SIMILARITY,
            AgentRoute.OVERVIEW,
        }
        and not mentions
    ):
        route = AgentRoute.CLARIFY
    if route == AgentRoute.COMPARE and len(mentions) < 2:
        route = AgentRoute.CLARIFY
    needs_clarification = route == AgentRoute.CLARIFY
    return QueryPlan(
        route=route,
        confidence=agent_plan.confidence,
        answer_depth="deep" if "why" in question.casefold() else "normal",
        raw_player_mentions=mentions,
        metrics=metrics,
        min_games=_extract_min_games(question),
        time_window=_extract_time_window(question),
        tool_recipe=ROUTE_TOOLS[route],
        needs_clarification=needs_clarification,
        clarification_question=(
            "Which player or comparison do you want to analyze?"
            if needs_clarification
            else None
        ),
        planner_source="deterministic",
    )


def _parse_plan_response(response: Any) -> QueryPlan | None:
    output_text = str(getattr(response, "output_text", "") or "")
    if not output_text:
        return None
    try:
        raw = json.loads(output_text)
        plan = QueryPlan.model_validate(raw)
    except (json.JSONDecodeError, ValidationError, TypeError, ValueError):
        return None
    plan.planner_source = "llm"
    plan.tool_recipe = ROUTE_TOOLS[plan.route]
    return plan


def build_query_plan(
    question: str,
    *,
    settings: Settings,
    client: Any | None,
    model: str | None = None,
) -> QueryPlan:
    fallback = deterministic_query_plan(question)
    if (
        not settings.agent_planner_enabled
        or client is None
        or not settings.openai_agent_enabled
    ):
        return fallback
    request_client = client
    create_extra: dict[str, Any] = {}
    if hasattr(client, "with_options"):
        request_client = client.with_options(
            timeout=settings.agent_planner_timeout_seconds
        )
    else:
        create_extra["timeout"] = settings.agent_planner_timeout_seconds
    try:
        response = request_client.responses.create(
            model=settings.agent_planner_model or model or settings.openai_agent_model,
            instructions=PLANNER_PROMPT,
            input=[{"role": "user", "content": question}],
            text=QUERY_PLAN_TEXT_FORMAT,
            **create_extra,
        )
    except Exception as exc:
        # The deterministic fallback keeps the request alive, but a planner
        # that fails every call (e.g. a schema rejection) must show in logs.
        logger.warning("LLM planner failed; using deterministic plan: %s", exc)
        return fallback
    plan = _parse_plan_response(response)
    if plan is None or plan.confidence < settings.agent_planner_min_confidence:
        return fallback
    return plan
