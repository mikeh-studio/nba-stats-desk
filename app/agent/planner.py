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
                "last_n_weeks": {"type": ["integer", "null"]},
                "last_n_days": {"type": ["integer", "null"]},
                "start_date": {"type": ["string", "null"]},
                "end_date": {"type": ["string", "null"]},
                "granularity": {
                    "type": "string",
                    "enum": ["auto", "game", "week", "month"],
                },
                "comparison_mode": {
                    "type": "string",
                    "enum": ["previous_period", "league_baseline", "none"],
                },
                "comparison_start_date": {"type": ["string", "null"]},
                "comparison_end_date": {"type": ["string", "null"]},
                "anchor_date": {"type": ["string", "null"]},
            },
            "required": [
                "kind",
                "last_n_games",
                "last_n_weeks",
                "last_n_days",
                "start_date",
                "end_date",
                "granularity",
                "comparison_mode",
                "comparison_start_date",
                "comparison_end_date",
                "anchor_date",
            ],
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
extract metric words, and identify date ranges, last-N-game windows,
last-N-day windows, or past/last-N-week windows. For week-over-week, weekly, or
past N weeks requests, set time_window.kind to "last_n_weeks", last_n_weeks to
N, granularity to "week", and last_n_games to null. Never convert a week window
into a game-count window. For "last N days" or "past N days", set
time_window.kind to "last_n_days", last_n_days to N, and use granularity "auto"
unless the user explicitly asks game-by-game, weekly, or monthly. Set
granularity to "game" for game-by-game/per-game breakdown requests, "week" for
weekly/week-over-week requests, "month" for monthly requests, otherwise "auto".
Use comparison_mode "league_baseline" only when the user explicitly asks for
league average or baseline; otherwise use "previous_period" for relative or
date-range trend windows.
Extract minimum-games cohort filters such as "at least 50 games" into min_games.
Only put concrete box-score stats in metrics (points, rebounds, assists,
steals, blocks, turnovers, threes, minutes). For vague catch-all wording like
"stats", "all stats", "individual stats", or "everything", leave metrics empty
so the default tier set is used (tier 1 points/rebounds/assists plus tier 2
steals/blocks/turnovers); never emit those words as metrics, and never route to
clarify just because the metric is vague.
Use player_trend for one-player questions comparing a player to league average
or baseline. Use compare only for two-player comparisons.
Mark needs_clarification true ONLY when the player or comparison target is
missing or ambiguous. Never ask which stat to use: vague or missing metrics are
answered with the default box-score set, so a named player always gets an
answer rather than a follow-up question.
""".strip()


def _extract_granularity(question: str) -> str:
    q = question.casefold()
    if re.search(r"\b(game[-\s]?by[-\s]?game|per game|each game|every game)\b", q):
        return "game"
    if re.search(r"\b(week[-\s]?over[-\s]?week|weekly|by week|week by week)\b", q):
        return "week"
    if re.search(
        r"\b(month[-\s]?over[-\s]?month|monthly|by month|month by month)\b", q
    ):
        return "month"
    return "auto"


def _extract_comparison_mode(question: str) -> str:
    q = question.casefold()
    if re.search(r"\b(league average|league baseline|baseline)\b", q):
        return "league_baseline"
    return "previous_period"


def _extract_time_window(question: str) -> TimeWindow:
    q = question.casefold()
    granularity = _extract_granularity(question)
    comparison_mode = _extract_comparison_mode(question)
    last_match = re.search(r"\blast\s+(\d{1,2})\s+games?\b", q)
    week_match = re.search(
        r"\b(?:over\s+the\s+)?(?:past|last|previous|prior)\s+(\d{1,2})[-\s]+weeks?\b",
        q,
    )
    day_match = re.search(
        r"\b(?:over\s+the\s+|in\s+the\s+)?(?:past|last|previous|prior)\s+(\d{1,3})[-\s]+days?\b",
        q,
    )
    dates = re.findall(r"\b20\d{2}-\d{2}-\d{2}\b", question)
    if dates:
        return TimeWindow(
            kind="date_range",
            start_date=dates[0],
            end_date=dates[1] if len(dates) > 1 else None,
            granularity=granularity,
            comparison_mode=comparison_mode,
        )
    if last_match:
        return TimeWindow(
            kind="last_n_games",
            last_n_games=int(last_match.group(1)),
            granularity="game" if granularity == "auto" else granularity,
            comparison_mode=comparison_mode,
        )
    if week_match:
        return TimeWindow(
            kind="last_n_weeks",
            last_n_weeks=max(1, min(52, int(week_match.group(1)))),
            granularity="week" if granularity == "auto" else granularity,
            comparison_mode=comparison_mode,
        )
    if day_match:
        return TimeWindow(
            kind="last_n_days",
            last_n_days=max(1, min(365, int(day_match.group(1)))),
            granularity=granularity,
            comparison_mode=comparison_mode,
        )
    if any(
        term in q
        for term in (
            "recent",
            "trend",
            "trending",
            "form",
            "weekly",
            "week-over-week",
            "week over week",
        )
    ):
        return TimeWindow(kind="recent", granularity=granularity)
    return TimeWindow(granularity=granularity)


def _metric_name_in_question(question_norm: str, name: object) -> bool:
    name_norm = normalize_player_text(str(name))
    if not name_norm:
        return False
    return (
        re.search(rf"(?:^|\s){re.escape(name_norm)}(?:\s|$)", question_norm) is not None
    )


def _extract_metrics(question: str) -> list[str]:
    catalog = load_semantic_catalog()
    found: list[str] = []
    q = normalize_player_text(question)
    for metric in catalog.list_metrics():
        names = [metric["key"], metric["label"], *metric.get("aliases", [])]
        if any(_metric_name_in_question(q, name) for name in names):
            key = str(metric["key"])
            if key not in found:
                found.append(key)
    return found


_OPPONENT_BREAKDOWN_PATTERNS = (
    r"\bagainst (?:which|what|certain|a specific|specific) (?:team|teams|opponent)",
    r"\b(?:which|what|specific|certain) (?:team|teams|opponent)",
    r"\bstruggle[ds]? (?:more |most )?(?:with|against|versus|vs)\b",
    r"\b(?:by|per|each|every) (?:team|opponent)\b",
    r"\bopponent (?:split|splits|breakdown|matchup)",
    r"\bmatchups?\b",
)


def detect_opponent_breakdown(question: str) -> bool:
    """True when the question asks how a player fares against specific teams.

    Drives an opponent-by-opponent game-log aggregation so the agent can name
    the toughest matchup instead of refusing for lack of opponent data.
    """
    q = question.casefold()
    return any(re.search(pattern, q) for pattern in _OPPONENT_BREAKDOWN_PATTERNS)


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
        "analyze",
        # League and cohort words.
        "nba", "league", "average", "baseline", "eastern", "western",
        "conference", "regular", "season", "playoffs", "team", "teams",
        "player", "players", "game", "games", "top", "best", "leaders",
        # Metric words.
        "points", "assists", "rebounds", "steals", "blocks", "turnovers",
        "percentile", "trend", "trends", "stats", "minutes", "performance",
        # Time words.
        "january", "february", "march", "april", "may", "june", "july",
        "august", "september", "october", "november", "december",
        "monday", "tuesday", "wednesday", "thursday", "friday",
        "saturday", "sunday", "last", "recent", "this", "today",
        "yesterday", "past", "week", "weeks", "weekly", "month", "basis",
    }
)  # fmt: skip


_MENTION_TRIM_PREFIXES = frozenset(
    {
        "analyze",
        "compare",
        "show",
        "tell",
        "give",
        "list",
        "find",
        "rank",
    }
)


def _trim_mention_phrase(phrase: str) -> str:
    words = phrase.split()
    while len(words) > 1 and words[0].casefold() in _MENTION_TRIM_PREFIXES:
        words = words[1:]
    return " ".join(words)


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
        phrase = _trim_mention_phrase(match.group(0).strip())
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
        opponent_breakdown=detect_opponent_breakdown(question),
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


def _has_explicit_time_window(window: TimeWindow) -> bool:
    return (
        window.kind in {"date_range", "last_n_games", "last_n_weeks", "last_n_days"}
        or window.last_n_games is not None
        or window.last_n_weeks is not None
        or window.last_n_days is not None
        or window.start_date is not None
        or window.end_date is not None
    )


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
    # A named player must never dead-end on a metric/clarify question. If the
    # LLM punted to CLARIFY (it sometimes reads "summarize his stats" as
    # under-specified) but the deterministic router found a real route, trust
    # the deterministic plan: vague metrics resolve to the default tier set
    # rather than asking the user which stat to use.
    if plan.route == AgentRoute.CLARIFY and fallback.route != AgentRoute.CLARIFY:
        return fallback
    if _has_explicit_time_window(fallback.time_window):
        plan.time_window = fallback.time_window
    return plan
