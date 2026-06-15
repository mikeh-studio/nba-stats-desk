from __future__ import annotations

import json
import re
from time import monotonic, sleep
from typing import Any, Callable

from pydantic import ValidationError

from app.agent.conversation import ConversationStore, PendingClarification
from app.agent.observability import AgentTrace, summarize_tool_result
from app.agent.planner import (
    QueryPlan,
    build_query_plan,
    detect_opponent_breakdown,
    deterministic_query_plan,
)
from app.agent.player_resolver import (
    PlayerCandidate,
    PlayerResolution,
    PlayerResolver,
)
from app.agent.router import AgentRoute, build_agent_plan
from app.agent.tools import StatsToolRunner, get_tool_schemas
from app.config import Settings
from app.repository import WarehouseRepository

SYSTEM_PROMPT = """
You are an NBA stats analyst for the 2025-26 NBA Stats Desk site.
Answer only from the provided tool results and curated gold-model semantics.
Use tools for player identity, game logs, percentiles, trends, rankings, and similarity.
Use calculate_player_percentile for questions asking where one player ranks in a metric cohort.
For "points attributed", "points created", or "points + assists * 2", use metric points_created.
For game-by-game questions, call get_player_game_log so the response can include each game's values.
For "which team did they struggle against" or opponent-matchup questions, use get_player_opponent_splits and cite the toughest_opponent it returns.
For date-range questions, pass start_date and end_date as YYYY-MM-DD tool arguments; use null for an open side of the range.
Respect explicit minimum-games filters; if the cohort is empty or the player is outside it, say that directly.
Do not invent SQL, raw table names, injuries, transactions, or facts not present in tool data.
If a player name is ambiguous, ask the user to choose from the matches.
Copy relevant tool chart/table payloads into the final structured response.
Keep answers direct and useful for NBA fans comparing player form against the league.
""".strip()

EVIDENCE_PROMPT = f"""
{SYSTEM_PROMPT}

You are writing from a prebuilt evidence bundle. Do not call tools unless the
bundle is clearly insufficient. Explain the answer with enough depth for the
route, cite concrete values from evidence, and surface caveats from tool
statuses or limited samples.
Answer with whatever the bundle does contain rather than refusing outright: if
a tool fell back to default metrics or returned partial data, use it and note
the limitation. Only decline a sub-question when no relevant evidence is
present at all.
""".strip()


AGENT_ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "tables": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "columns": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "key": {"type": "string"},
                                "label": {"type": "string"},
                            },
                            "required": ["key", "label"],
                            "additionalProperties": False,
                        },
                    },
                    "rows": {
                        "type": "array",
                        "items": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                },
                "required": ["title", "columns", "rows"],
                "additionalProperties": False,
            },
        },
        "charts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ["line", "bar"]},
                    "title": {"type": "string"},
                    "x_label": {"type": "string"},
                    "y_label": {"type": "string"},
                    "series": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "key": {"type": "string"},
                                "label": {"type": "string"},
                                "points": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "x": {"type": "string"},
                                            "y": {"type": "number"},
                                            "meta": {"type": "string"},
                                        },
                                        "required": ["x", "y", "meta"],
                                        "additionalProperties": False,
                                    },
                                },
                            },
                            "required": ["key", "label", "points"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["type", "title", "x_label", "y_label", "series"],
                "additionalProperties": False,
            },
        },
        "metric_definitions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "label": {"type": "string"},
                    "definition": {"type": "string"},
                },
                "required": ["key", "label", "definition"],
                "additionalProperties": False,
            },
        },
        "followups": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "answer",
        "assumptions",
        "tables",
        "charts",
        "metric_definitions",
        "followups",
    ],
    "additionalProperties": False,
}

TEXT_FORMAT = {
    "format": {
        "type": "json_schema",
        "name": "nba_stats_agent_answer",
        "schema": AGENT_ANSWER_SCHEMA,
        "strict": True,
    }
}


class AgentDisabledError(RuntimeError):
    """Raised when the stats agent is not configured to call OpenAI."""


class AgentExecutionError(RuntimeError):
    """Raised when an OpenAI agent run fails."""


def _default_agent_answer(answer: str) -> dict[str, Any]:
    return {
        "answer": answer,
        "assumptions": [],
        "tables": [],
        "charts": [],
        "metric_definitions": [],
        "followups": [],
        "player_profile": None,
    }


def normalize_agent_answer(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return _default_agent_answer(str(payload or "No answer returned."))
    answer = _default_agent_answer(str(payload.get("answer") or "No answer returned."))
    for key in ("assumptions", "tables", "charts", "metric_definitions", "followups"):
        value = payload.get(key)
        answer[key] = value if isinstance(value, list) else []
    return answer


ProgressCallback = Callable[[dict[str, Any]], None]


def _is_transient_api_error(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int) and (
        status_code in {408, 409, 429} or status_code >= 500
    ):
        return True
    exc_name = type(exc).__name__.casefold()
    text = str(exc).casefold()
    return "timeout" in exc_name or "timeout" in text or "timed out" in text


def _tool_schemas_for_plan(required_tools: list[str]) -> list[dict[str, Any]]:
    # Catalog order is kept stable regardless of the route: tools render at
    # the front of the prompt, so per-route reordering would invalidate the
    # provider prompt cache on every request. The route hint message already
    # tells the model which tools to prefer.
    del required_tools
    return get_tool_schemas()


def _route_hint(agent_plan: Any) -> str:
    tools = ", ".join(agent_plan.required_tools) or "none"
    return (
        "Routing plan for this question: "
        f"route={agent_plan.route.value}; confidence={agent_plan.confidence}; "
        f"suggested_tools={tools}. Prefer these tools first when they apply, "
        "but still obey the strict tool schemas and answer only from tool results."
    )


def _emit_progress(callback: ProgressCallback | None, payload: dict[str, Any]) -> None:
    if callback is not None:
        callback(payload)


_JSON_STRING_ESCAPES = {
    '"': '"',
    "\\": "\\",
    "/": "/",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}


class _AnswerFieldStream:
    """Incrementally decode the top-level "answer" string from streamed JSON.

    The structured answer arrives as JSON tokens; users should see the prose
    of the "answer" field (the schema's first property) as it generates, not
    raw JSON. Feeds may split anywhere, including mid-escape. On anything
    unexpected the extractor goes quiet — the final parsed payload still
    renders the complete answer.
    """

    _KEY = '"answer"'

    def __init__(self, emit: Callable[[str], None]) -> None:
        self._emit = emit
        self._buf = ""
        self._state = "seek"
        self.emitted = False

    def feed(self, chunk: str) -> None:
        if not chunk or self._state == "done":
            return
        self._buf += chunk
        while self._state != "done":
            if self._state == "seek":
                idx = self._buf.find(self._KEY)
                if idx < 0:
                    return
                self._buf = self._buf[idx + len(self._KEY) :]
                self._state = "colon"
            elif self._state in {"colon", "quote"}:
                stripped = self._buf.lstrip()
                if not stripped:
                    self._buf = ""
                    return
                expected = ":" if self._state == "colon" else '"'
                if stripped[0] != expected:
                    self._state = "done"
                    return
                self._buf = stripped[1:]
                self._state = "string" if self._state == "quote" else "quote"
            else:  # "string"
                self._consume_string()
                return

    def _consume_string(self) -> None:
        decoded: list[str] = []
        i = 0
        buf = self._buf
        while i < len(buf):
            char = buf[i]
            if char == '"':
                self._state = "done"
                i += 1
                break
            if char == "\\":
                if i + 1 >= len(buf):
                    break  # incomplete escape; wait for the next chunk
                escape = buf[i + 1]
                if escape == "u":
                    if i + 6 > len(buf):
                        break
                    try:
                        decoded.append(chr(int(buf[i + 2 : i + 6], 16)))
                    except ValueError:
                        pass
                    i += 6
                    continue
                decoded.append(_JSON_STRING_ESCAPES.get(escape, escape))
                i += 2
                continue
            decoded.append(char)
            i += 1
        self._buf = buf[i:]
        if decoded:
            self.emitted = True
            self._emit("".join(decoded))


def _resolved_players_payload(
    resolutions: list[PlayerResolution],
) -> list[dict[str, Any]]:
    return [
        resolution.player.model_dump(mode="json")
        for resolution in resolutions
        if resolution.status == "ok" and resolution.player is not None
    ]


def _safe_json(value: Any) -> str:
    return json.dumps(value, default=str, sort_keys=True)


def _compact_mapping(
    source: dict[str, Any],
    fields: tuple[str, ...],
) -> dict[str, Any]:
    return {
        field: source.get(field)
        for field in fields
        if source.get(field) not in (None, "")
    }


def _player_initials(name: str) -> str:
    initials = "".join(part[:1] for part in name.split()[:2]).upper()
    return initials or "NBA"


def _dict_field(source: dict[str, Any], key: str) -> dict[str, Any]:
    value = source.get(key)
    return value if isinstance(value, dict) else {}


def _player_profile_payload(
    *,
    candidate: PlayerCandidate,
    detail: dict[str, Any] | None,
) -> dict[str, Any] | None:
    detail = detail or {}
    raw_player = _dict_field(detail, "player")
    player_id = raw_player.get("player_id") or candidate.player_id
    player_name = raw_player.get("player_name") or candidate.player_name
    if not player_name:
        return None

    player = _compact_mapping(
        {
            **raw_player,
            "player_id": player_id,
            "player_name": player_name,
            "team_abbr": raw_player.get("team_abbr") or candidate.team_abbr,
            "latest_game_date": raw_player.get("latest_game_date")
            or candidate.latest_game_date,
            "games_sampled": raw_player.get("games_sampled") or candidate.games_sampled,
            "overall_rank": raw_player.get("overall_rank") or candidate.overall_rank,
            "player_initials": raw_player.get("player_initials")
            or _player_initials(str(player_name)),
        },
        (
            "player_id",
            "player_name",
            "team_abbr",
            "headshot_url",
            "player_initials",
            "latest_game_date",
            "overall_rank",
            "recommendation_score",
            "recommendation_tier",
            "category_strengths",
            "category_risks",
            "games_sampled",
            "sample_status",
            "is_qualified",
        ),
    )
    profile: dict[str, Any] = {
        "player": player,
        "profile_url": f"/players/{player_id}" if player_id is not None else None,
        "availability_state": detail.get("availability_state"),
        "availability_reason": detail.get("availability_reason"),
        "reason_summary": detail.get("reason_summary"),
        "sample": _compact_mapping(
            _dict_field(detail, "sample"),
            (
                "games_sampled",
                "qualification_games",
                "is_qualified",
                "sample_status",
                "sample_warning",
            ),
        ),
        "trend": _compact_mapping(
            _dict_field(detail, "trend"),
            ("status", "delta", "pct_change"),
        ),
        "recent_form": [
            _compact_mapping(
                item,
                (
                    "window_key",
                    "window_label",
                    "games_in_window",
                    "avg_pts",
                    "avg_reb",
                    "avg_ast",
                    "avg_fg3m",
                    "fantasy_proxy",
                ),
            )
            for item in (detail.get("recent_form") or [])[:2]
            if isinstance(item, dict)
        ],
        "category_profile": [
            _compact_mapping(
                item,
                ("category", "impact_score", "category_tier", "category_direction"),
            )
            for item in (detail.get("category_profile") or [])[:4]
            if isinstance(item, dict)
        ],
        "stat_percentiles": [
            _compact_mapping(item, ("key", "label", "average", "percentile"))
            for item in (detail.get("stat_percentiles") or [])[:4]
            if isinstance(item, dict)
        ],
        "archetype": _compact_mapping(
            _dict_field(detail, "archetype"),
            ("archetype_label", "summary"),
        ),
        "similar_players": [
            _compact_mapping(
                item,
                ("player_id", "player_name", "team_abbr", "similarity_score"),
            )
            for item in (detail.get("similar_players") or [])[:3]
            if isinstance(item, dict)
        ],
    }
    return {key: value for key, value in profile.items() if value not in (None, {}, [])}


_CLARIFY_REPLY_FILLER = re.compile(
    r"^(?:i\s+meant|i\s+mean|no[,!]?|yes[,!]?|it'?s|the\s+player|player)\s+",
    re.IGNORECASE,
)


def _clarify_reply_name(text: str) -> str | None:
    """Extract a player-name guess from a reply to 'Which player did you mean?'.

    Returns the stripped name for short replies (with leading fillers like
    "I meant ..." removed), or None when the reply reads like a brand-new
    question that should drop the pending clarification.
    """
    stripped = _CLARIFY_REPLY_FILLER.sub("", text.strip()).strip(" .!?")
    words = stripped.split()
    if 0 < len(words) <= 4:
        return stripped
    return None


def _selection_resolution(player_id: int | None, player_name: str) -> PlayerResolution:
    candidate = PlayerCandidate(
        player_id=player_id,
        player_name=player_name,
        confidence=1.0,
        match_method="user_selected",
    )
    return PlayerResolution(
        raw_name=player_name,
        status="ok",
        confidence=1.0,
        player=candidate,
        candidates=[candidate],
        resolution_method="user_selected",
    )


def _resolutions_from_plan_players(query_plan: QueryPlan) -> list[PlayerResolution]:
    resolutions: list[PlayerResolution] = []
    for raw in query_plan.resolved_players:
        try:
            candidate = PlayerCandidate.model_validate(raw)
        except ValidationError:
            continue
        resolutions.append(
            PlayerResolution(
                raw_name=candidate.player_name,
                status="ok",
                confidence=candidate.confidence,
                player=candidate,
                candidates=[candidate],
                resolution_method=candidate.match_method,
            )
        )
    return resolutions


def _merge_selected_resolution(
    resolutions: list[PlayerResolution],
    selected: PlayerResolution,
) -> list[PlayerResolution]:
    """Place the user's pick first; it answers the first unresolved mention.

    Other resolved players are kept (compare questions), and any remaining
    unresolved mentions are kept too so the next clarification round still has
    its candidates.
    """
    selected_id = selected.player.player_id if selected.player else None
    merged: list[PlayerResolution] = [selected]
    replaced = False
    for resolution in resolutions:
        if resolution.status == "ok" and resolution.player is not None:
            if selected_id is not None and resolution.player.player_id == selected_id:
                continue
            merged.append(resolution)
        elif not replaced:
            replaced = True
        else:
            merged.append(resolution)
    return merged


class StatsAgent:
    def __init__(
        self,
        settings: Settings,
        repo: WarehouseRepository,
        *,
        client: Any | None = None,
        claude_client: Any | None = None,
        tool_runner: StatsToolRunner | None = None,
        conversation_store: ConversationStore | None = None,
        player_resolver: PlayerResolver | None = None,
    ) -> None:
        self.settings = settings
        self.repo = repo
        self.client = client
        self.claude_client = claude_client
        self.tool_runner = tool_runner or StatsToolRunner(
            repo,
            cache_ttl_seconds=settings.agent_cache_ttl_seconds,
        )
        self.conversation_store = conversation_store
        self.player_resolver = player_resolver or PlayerResolver(
            repo,
            min_confidence=settings.agent_player_match_min_confidence,
        )

    def _get_client(self, provider: str = "openai") -> Any:
        if provider == "claude":
            return self._get_claude_client()
        if self.client is not None:
            return self.client
        if not self.settings.openai_agent_enabled:
            raise AgentDisabledError("The OpenAI stats agent is disabled.")
        if not self.settings.openai_api_key:
            raise AgentDisabledError("OPENAI_API_KEY is required to use Ask NBA Stats.")
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise AgentDisabledError(
                "The openai package is not installed. Run pip install -r requirements.txt."
            ) from exc
        self.client = OpenAI(api_key=self.settings.openai_api_key)
        return self.client

    def _get_claude_client(self) -> Any:
        if self.claude_client is not None:
            return self.claude_client
        if not self.settings.openai_agent_enabled:
            raise AgentDisabledError("The stats agent is disabled.")
        if not self.settings.anthropic_api_key:
            raise AgentDisabledError(
                "ANTHROPIC_API_KEY is required to use Claude for Ask NBA Stats."
            )
        try:
            from anthropic import Anthropic
        except ImportError as exc:
            raise AgentDisabledError(
                "The anthropic package is not installed. "
                "Run pip install -r requirements.txt."
            ) from exc
        from app.agent.claude_client import ClaudeResponsesClient

        self.claude_client = ClaudeResponsesClient(
            # max_retries=0: _create_response owns retry/backoff; stacking the
            # SDK's internal retries on top multiplies worst-case latency.
            client=Anthropic(
                api_key=self.settings.anthropic_api_key,
                max_retries=0,
            ),
            model=self.settings.anthropic_agent_model,
        )
        return self.claude_client

    def _request_timeout_seconds(self, provider: str) -> float:
        if provider == "claude":
            return self.settings.anthropic_agent_timeout_seconds
        return self.settings.openai_agent_timeout_seconds

    def _create_response(
        self,
        *,
        client: Any,
        model: str,
        instructions: str,
        input_messages: list[Any],
        tools: list[dict[str, Any]] | None,
        text: dict[str, Any],
        timeout_seconds: float,
        provider: str = "openai",
        on_output_text_delta: Callable[[str], None] | None = None,
    ) -> Any:
        request_client = client
        create_extra: dict[str, Any] = {}
        if hasattr(client, "with_options"):
            request_client = client.with_options(timeout=timeout_seconds)
        else:
            create_extra["timeout"] = timeout_seconds
        if on_output_text_delta is not None and provider == "claude":
            # Only the Claude adapter understands this kwarg; the OpenAI SDK
            # would reject it.
            create_extra["on_output_text_delta"] = on_output_text_delta
        attempts = max(0, self.settings.openai_agent_max_retries) + 1
        delay = max(0.0, self.settings.openai_agent_retry_base_delay_seconds)
        last_exc: Exception | None = None
        for attempt in range(attempts):
            try:
                kwargs: dict[str, Any] = {
                    "model": model,
                    "instructions": instructions,
                    "input": input_messages,
                    "text": text,
                    **create_extra,
                }
                if tools is not None:
                    kwargs["tools"] = tools
                return request_client.responses.create(**kwargs)
            except Exception as exc:  # noqa: PERF203 - retry clarity matters here.
                last_exc = exc
                if attempt >= attempts - 1 or not _is_transient_api_error(exc):
                    break
                sleep(delay * (2**attempt))
        # Name the provider and underlying error (Anthropic/OpenAI messages
        # carry their request IDs) so failures are diagnosable from logs.
        raise AgentExecutionError(
            f"{provider} agent request failed: {type(last_exc).__name__}: {last_exc}"
        ) from last_exc

    def _record_tool_call(
        self,
        *,
        name: str,
        args: dict[str, Any],
        result: dict[str, Any],
        started: float,
        tool_calls: list[dict[str, Any]],
        trace: AgentTrace | None,
        progress_callback: ProgressCallback | None,
    ) -> None:
        duration_ms = int((monotonic() - started) * 1000)
        status = str(result.get("status", "ok"))
        if trace is not None:
            record = trace.add_tool(
                name=name,
                args=args,
                status=status,
                duration_ms=duration_ms,
                result=result,
            )
        else:
            record = {
                "name": name,
                "args": args,
                "status": status,
                "duration_ms": duration_ms,
                "result": summarize_tool_result(result),
            }
        tool_calls.append(record)
        _emit_progress(
            progress_callback,
            {
                "type": "tool_end",
                "name": name,
                "status": status,
                "duration_ms": duration_ms,
            },
        )

    def _resolution_result(self, resolution: PlayerResolution) -> dict[str, Any]:
        return {
            "status": resolution.status,
            "raw_name": resolution.raw_name,
            "confidence": resolution.confidence,
            "player": (
                resolution.player.model_dump(mode="json")
                if resolution.player is not None
                else None
            ),
            "matches": [
                candidate.model_dump(mode="json") for candidate in resolution.candidates
            ],
            "resolution_method": resolution.resolution_method,
        }

    def _player_profile_for_resolutions(
        self,
        resolutions: list[PlayerResolution],
    ) -> dict[str, Any] | None:
        players = [
            resolution.player
            for resolution in resolutions
            if resolution.status == "ok" and resolution.player is not None
        ]
        if len(players) != 1:
            return None

        candidate = players[0]
        detail: dict[str, Any] | None = None
        if candidate.player_id is not None:
            try:
                detail = self.repo.get_player_detail(candidate.player_id)
            except Exception:
                detail = None
        return _player_profile_payload(candidate=candidate, detail=detail)

    def _clarification_payload(
        self,
        *,
        query_plan: QueryPlan,
        agent_plan: Any,
        resolutions: list[PlayerResolution],
        conversation_id: str | None,
        tool_calls: list[dict[str, Any]],
    ) -> dict[str, Any]:
        options: list[dict[str, Any]] = []
        seen_options: set[int | str] = set()
        for resolution in resolutions:
            if resolution.status == "ok":
                continue
            for candidate in resolution.candidates:
                key: int | str = (
                    candidate.player_id
                    if candidate.player_id is not None
                    else candidate.player_name.casefold()
                )
                if key in seen_options:
                    continue
                seen_options.add(key)
                options.append(
                    {
                        "raw_name": resolution.raw_name,
                        "player_id": candidate.player_id,
                        "player_name": candidate.player_name,
                        "team_abbr": candidate.team_abbr,
                        "confidence": candidate.confidence,
                    }
                )
        options = options[:6]
        query_plan.needs_clarification = True
        query_plan.clarification_options = options
        query_plan.unresolved_players = [
            resolution.raw_name
            for resolution in resolutions
            if resolution.status != "ok"
        ]
        if not query_plan.clarification_question:
            if options:
                query_plan.clarification_question = "Which player did you mean?"
            else:
                query_plan.clarification_question = (
                    "Which player should I use for this question?"
                )
        payload = _default_agent_answer(query_plan.clarification_question)
        payload["tool_calls"] = tool_calls
        payload["conversation_id"] = conversation_id
        payload["agent_plan"] = agent_plan.model_dump(mode="json")
        payload["query_plan"] = query_plan.model_dump(mode="json")
        payload["resolved_players"] = []
        payload["clarification_options"] = options
        # Remember what we still owe an answer for so the next reply (or an
        # option click) resumes the original question instead of re-clarifying.
        original_question = str(
            getattr(agent_plan, "original_question", "") or ""
        ).strip()
        if conversation_id and self.conversation_store is not None:
            if original_question:
                self.conversation_store.set_pending_clarification(
                    conversation_id,
                    question=original_question,
                    query_plan=query_plan.model_dump(mode="json"),
                )
                self.conversation_store.append_turn(
                    conversation_id,
                    question=original_question,
                    answer=str(query_plan.clarification_question or ""),
                    max_turns=self.settings.agent_conversation_max_turns,
                )
        return payload

    def _pending_query_plan(self, pending: PendingClarification) -> QueryPlan | None:
        if not pending.query_plan:
            return None
        try:
            plan = QueryPlan.model_validate(pending.query_plan)
        except ValidationError:
            return None
        # The stored plan was dumped in its clarifying state; reset that so it
        # can drive a normal answer once the player is supplied.
        plan.needs_clarification = False
        plan.clarification_question = None
        plan.clarification_options = []
        plan.unresolved_players = []
        return plan

    def _failed_pick_clarification(
        self,
        *,
        pending: PendingClarification,
        resolution: PlayerResolution,
        reply: str,
        conversation_id: str | None,
        trace: AgentTrace | None,
        progress_callback: ProgressCallback | None,
    ) -> dict[str, Any]:
        query_plan = self._pending_query_plan(pending) or deterministic_query_plan(
            pending.question
        )
        if resolution.candidates:
            query_plan.clarification_question = "Which of these players did you mean?"
        else:
            query_plan.clarification_question = (
                f'I could not find a player matching "{reply}". '
                "Which player did you mean?"
            )
        agent_plan = query_plan.to_agent_plan(pending.question)
        if trace is not None:
            trace.conversation_id = conversation_id
            trace.set_plan(
                route=agent_plan.route.value, confidence=agent_plan.confidence
            )
            trace.outcome = "clarified"
        _emit_progress(
            progress_callback,
            {
                "type": "plan",
                "route": agent_plan.route.value,
                "confidence": agent_plan.confidence,
                "required_tools": agent_plan.required_tools,
            },
        )
        tool_calls: list[dict[str, Any]] = []
        self._record_tool_call(
            name="resolve_player",
            args={"name": resolution.raw_name, "limit": 5},
            result=self._resolution_result(resolution),
            started=monotonic(),
            tool_calls=tool_calls,
            trace=trace,
            progress_callback=progress_callback,
        )
        return self._clarification_payload(
            query_plan=query_plan,
            agent_plan=agent_plan,
            resolutions=[resolution],
            conversation_id=conversation_id,
            tool_calls=tool_calls,
        )

    def _run_recipe_tool(
        self,
        *,
        name: str,
        args: dict[str, Any],
        tool_calls: list[dict[str, Any]],
        trace: AgentTrace | None,
        progress_callback: ProgressCallback | None,
    ) -> dict[str, Any]:
        _emit_progress(
            progress_callback, {"type": "tool_start", "name": name, "args": args}
        )
        started = monotonic()
        if name == "compare_players":
            try:
                result = self.repo.get_compare(
                    int(args["player_a_id"]),
                    int(args["player_b_id"]),
                    window=args.get("window") or "last_5",
                    focus=args.get("focus") or "balanced",
                )
                result = {"status": "ok", **result}
            except Exception as exc:
                result = {"status": "error", "message": str(exc)}
        else:
            result = self.tool_runner.call_tool(name, args)
        self._record_tool_call(
            name=name,
            args=args,
            result=result,
            started=started,
            tool_calls=tool_calls,
            trace=trace,
            progress_callback=progress_callback,
        )
        return result

    def _build_evidence_bundle(
        self,
        *,
        query_plan: QueryPlan,
        resolutions: list[PlayerResolution],
        tool_calls: list[dict[str, Any]],
        trace: AgentTrace | None,
        progress_callback: ProgressCallback | None,
        max_tool_calls: int,
    ) -> dict[str, Any] | None:
        players = [
            resolution.player
            for resolution in resolutions
            if resolution.status == "ok" and resolution.player is not None
        ]
        metrics = query_plan.metrics or None
        window = query_plan.time_window
        evidence: dict[str, Any] = {
            "query_plan": query_plan.model_dump(mode="json"),
            "resolved_players": [
                player.model_dump(mode="json")
                for player in players
                if player is not None
            ],
            "results": [],
            "tool_limit_reached": False,
        }
        for resolution in resolutions:
            started = monotonic()
            if len(tool_calls) >= max_tool_calls:
                result = {
                    "status": "tool_limit",
                    "message": "Tool-call limit reached before this call executed.",
                }
                evidence["tool_limit_reached"] = True
            else:
                result = self._resolution_result(resolution)
            self._record_tool_call(
                name="resolve_player",
                args={"name": resolution.raw_name, "limit": 5},
                result=result,
                started=started,
                tool_calls=tool_calls,
                trace=trace,
                progress_callback=progress_callback,
            )
            evidence["results"].append({"tool": "resolve_player", "result": result})
            if evidence["tool_limit_reached"]:
                return evidence

        def add_tool(name: str, args: dict[str, Any]) -> bool:
            if len(tool_calls) >= max_tool_calls:
                started = monotonic()
                result = {
                    "status": "tool_limit",
                    "message": "Tool-call limit reached before this call executed.",
                }
                self._record_tool_call(
                    name=name,
                    args=args,
                    result=result,
                    started=started,
                    tool_calls=tool_calls,
                    trace=trace,
                    progress_callback=progress_callback,
                )
                evidence["tool_limit_reached"] = True
            else:
                result = self._run_recipe_tool(
                    name=name,
                    args=args,
                    tool_calls=tool_calls,
                    trace=trace,
                    progress_callback=progress_callback,
                )
            evidence["results"].append({"tool": name, "result": result})
            return not evidence["tool_limit_reached"]

        if query_plan.route == AgentRoute.RANKING:
            metric = (metrics or ["pts"])[0]
            add_tool("search_rankings", {"metric": metric, "limit": 10})
            return evidence
        if query_plan.route in {AgentRoute.CLARIFY}:
            return None
        if not players:
            return None

        primary = players[0]
        player_id = primary.player_id
        if player_id is None:
            return None

        start_date = window.start_date
        end_date = window.end_date
        game_limit = window.last_n_games or 10
        if query_plan.route == AgentRoute.PLAYER_TREND:
            if not add_tool(
                "get_player_trends",
                {
                    "player_id": player_id,
                    "metrics": metrics,
                    "start_date": start_date,
                    "end_date": end_date,
                },
            ):
                return evidence
            if not add_tool(
                "get_player_game_log",
                {
                    "player_id": player_id,
                    "metrics": metrics,
                    "limit": game_limit,
                    "start_date": start_date,
                    "end_date": end_date,
                },
            ):
                return evidence
            if metrics:
                add_tool(
                    "get_player_percentiles",
                    {"player_id": player_id, "metrics": metrics},
                )
            if query_plan.opponent_breakdown:
                add_tool(
                    "get_player_opponent_splits",
                    {
                        "player_id": player_id,
                        "metrics": metrics,
                        "limit": game_limit,
                        "start_date": start_date,
                        "end_date": end_date,
                    },
                )
            return evidence
        if query_plan.route == AgentRoute.GAME_LOG:
            add_tool(
                "get_player_game_log",
                {
                    "player_id": player_id,
                    "metrics": metrics,
                    "limit": game_limit,
                    "start_date": start_date,
                    "end_date": end_date,
                },
            )
            if query_plan.opponent_breakdown:
                add_tool(
                    "get_player_opponent_splits",
                    {
                        "player_id": player_id,
                        "metrics": metrics,
                        "limit": game_limit,
                        "start_date": start_date,
                        "end_date": end_date,
                    },
                )
            return evidence
        if query_plan.route == AgentRoute.PERCENTILE:
            add_tool(
                "calculate_player_percentile",
                {
                    "player_id": player_id,
                    "metric": (metrics or ["pts"])[0],
                    "min_games": query_plan.min_games or 5,
                },
            )
            return evidence
        if query_plan.route == AgentRoute.SIMILARITY:
            add_tool("find_similar_players", {"player_id": player_id, "limit": 5})
            return evidence
        if query_plan.route == AgentRoute.COMPARE and len(players) >= 2:
            secondary_id = players[1].player_id
            if secondary_id is None:
                return None
            if not add_tool(
                "compare_players",
                {
                    "player_a_id": player_id,
                    "player_b_id": secondary_id,
                    "window": "last_5",
                    "focus": "balanced",
                },
            ):
                return evidence
            if window.kind in {"recent", "last_n_games"}:
                for player in players[:2]:
                    if player.player_id is not None:
                        if not add_tool(
                            "get_player_game_log",
                            {
                                "player_id": player.player_id,
                                "metrics": metrics,
                                "limit": game_limit,
                                "start_date": start_date,
                                "end_date": end_date,
                            },
                        ):
                            return evidence
            return evidence
        if query_plan.route == AgentRoute.OVERVIEW:
            add_tool("get_player_summary", {"player_id": player_id})
            return evidence
        return None

    def _answer_from_evidence(
        self,
        *,
        client: Any,
        cleaned_question: str,
        input_messages: list[Any],
        evidence: dict[str, Any],
        query_plan: QueryPlan,
        agent_plan: Any,
        tool_calls: list[dict[str, Any]],
        conversation_id: str | None,
        trace: AgentTrace | None,
        model: str,
        provider: str,
        resolutions: list[PlayerResolution],
        progress_callback: ProgressCallback | None = None,
    ) -> dict[str, Any] | None:
        # input_messages already carries the route hint as a developer
        # message; instructions stay byte-stable so the provider prompt
        # cache can hit across requests.
        evidence_messages = [
            *input_messages,
            {
                "role": "developer",
                "content": (
                    "Use this prebuilt NBA evidence bundle as the only factual "
                    f"source:\n{_safe_json(evidence)}"
                ),
            },
        ]
        answer_stream = (
            _AnswerFieldStream(
                lambda delta: _emit_progress(
                    progress_callback, {"type": "answer_delta", "delta": delta}
                )
            )
            if progress_callback is not None
            else None
        )
        response = self._create_response(
            client=client,
            model=model,
            instructions=EVIDENCE_PROMPT,
            input_messages=evidence_messages,
            tools=None,
            text=TEXT_FORMAT,
            timeout_seconds=self._request_timeout_seconds(provider),
            provider=provider,
            on_output_text_delta=(
                answer_stream.feed if answer_stream is not None else None
            ),
        )
        if trace is not None:
            trace.add_usage(getattr(response, "usage", None))
        if any(
            getattr(item, "type", None) == "function_call"
            for item in getattr(response, "output", []) or []
        ):
            return None
        output_text = str(getattr(response, "output_text", "") or "")
        try:
            parsed = json.loads(output_text)
        except json.JSONDecodeError:
            parsed = _default_agent_answer(output_text or "No answer returned.")
        payload = normalize_agent_answer(parsed)
        # Tells the SSE endpoint the answer text already went out as deltas,
        # so it must not replay it word by word.
        payload["answer_streamed"] = answer_stream is not None and answer_stream.emitted
        payload["tool_calls"] = tool_calls
        payload["conversation_id"] = conversation_id
        payload["agent_plan"] = agent_plan.model_dump(mode="json")
        payload["query_plan"] = query_plan.model_dump(mode="json")
        payload["resolved_players"] = evidence.get("resolved_players", [])
        payload["player_profile"] = self._player_profile_for_resolutions(resolutions)
        payload["clarification_options"] = []
        if conversation_id and self.conversation_store is not None:
            self.conversation_store.append_turn(
                conversation_id,
                question=cleaned_question,
                answer=str(payload.get("answer") or ""),
                max_turns=self.settings.agent_conversation_max_turns,
            )
        if trace is not None:
            trace.outcome = "answered"
        return payload

    def answer(
        self,
        question: str,
        *,
        conversation_id: str | None = None,
        trace: AgentTrace | None = None,
        progress_callback: ProgressCallback | None = None,
        selected_player: dict[str, Any] | None = None,
        provider: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        cleaned_question = question.strip()
        if not cleaned_question:
            raise ValueError("Question must not be blank.")
        provider_name = (provider or "openai").strip().lower()
        if provider_name not in {"openai", "claude"}:
            raise ValueError(f"Unknown agent provider: {provider_name}")
        selected_model = (
            model.strip()
            if model and model.strip()
            else (
                self.settings.anthropic_agent_model
                if provider_name == "claude"
                else self.settings.openai_agent_model
            )
        )
        if not self.settings.openai_agent_enabled:
            raise AgentDisabledError("The OpenAI stats agent is disabled.")
        # Fail fast on missing credentials before any planning work happens.
        if provider_name == "claude":
            if self.claude_client is None and not self.settings.anthropic_api_key:
                self._get_claude_client()
        elif self.client is None and not self.settings.openai_api_key:
            self._get_client()

        store = (
            self.conversation_store
            if conversation_id and self.conversation_store is not None
            else None
        )
        max_turns = self.settings.agent_conversation_max_turns
        pending = (
            store.get_pending_clarification(conversation_id)
            if store is not None and conversation_id
            else None
        )

        selected_resolution: PlayerResolution | None = None
        if selected_player is not None:
            selected_name = str(selected_player.get("player_name") or "").strip()
            if selected_name:
                raw_id = selected_player.get("player_id")
                try:
                    selected_id = int(raw_id) if raw_id is not None else None
                except (TypeError, ValueError):
                    selected_id = None
                selected_resolution = _selection_resolution(selected_id, selected_name)

        if pending is not None and selected_resolution is None:
            reply_name = _clarify_reply_name(cleaned_question)
            if reply_name is None:
                # Reads like a brand-new question; drop the pending one.
                if store is not None and conversation_id:
                    store.clear_pending_clarification(conversation_id)
                pending = None
            else:
                resolution = self.player_resolver.resolve(reply_name, limit=5)
                if resolution.status == "ok":
                    selected_resolution = resolution
                else:
                    return self._failed_pick_clarification(
                        pending=pending,
                        resolution=resolution,
                        reply=reply_name,
                        conversation_id=conversation_id,
                        trace=trace,
                        progress_callback=progress_callback,
                    )

        stored_plan: QueryPlan | None = None
        if pending is not None and selected_resolution is not None:
            # Resume the original question with the picked player.
            cleaned_question = pending.question
            stored_plan = self._pending_query_plan(pending)
            if store is not None and conversation_id:
                store.clear_pending_clarification(conversation_id)

        history_turns = (
            store.get_turns(conversation_id, max_turns=max_turns)
            if store is not None and conversation_id
            else []
        )

        agent_plan = build_agent_plan(cleaned_question)
        if (
            agent_plan.needs_clarification
            and not history_turns
            and selected_resolution is None
        ):
            if trace is not None:
                trace.conversation_id = conversation_id
                trace.set_plan(
                    route=agent_plan.route.value, confidence=agent_plan.confidence
                )
            _emit_progress(
                progress_callback,
                {
                    "type": "plan",
                    "route": agent_plan.route.value,
                    "confidence": agent_plan.confidence,
                    "required_tools": agent_plan.required_tools,
                },
            )
            clarification_text = (
                agent_plan.clarification_question
                or "Can you clarify which player or metric you want?"
            )
            payload = _default_agent_answer(clarification_text)
            payload["tool_calls"] = []
            payload["conversation_id"] = conversation_id
            payload["agent_plan"] = agent_plan.model_dump(mode="json")
            if store is not None and conversation_id:
                store.append_turn(
                    conversation_id,
                    question=cleaned_question,
                    answer=clarification_text,
                    max_turns=max_turns,
                )
            if trace is not None:
                trace.outcome = "clarified"
            return payload

        client = self._get_client(provider_name)
        planning_question = cleaned_question
        if history_turns:
            planning_question = (
                f"Previous question: {history_turns[-1].question}\n"
                f"Follow-up question: {cleaned_question}"
            )
        if stored_plan is not None and stored_plan.route != AgentRoute.CLARIFY:
            # The plan from the clarified question is reused as-is: no second
            # planner call is needed just to swap in the chosen player.
            query_plan = stored_plan
        else:
            query_plan = build_query_plan(
                planning_question,
                settings=self.settings,
                client=client,
                model=selected_model,
            )
        # Opponent intent is detected on the user's own wording so it holds
        # whether the plan came from the LLM planner or the deterministic one.
        query_plan.opponent_breakdown = (
            query_plan.opponent_breakdown
            or detect_opponent_breakdown(cleaned_question)
        )
        agent_plan = query_plan.to_agent_plan(cleaned_question)
        if trace is not None:
            trace.conversation_id = conversation_id
            trace.set_plan(
                route=agent_plan.route.value, confidence=agent_plan.confidence
            )
        _emit_progress(
            progress_callback,
            {
                "type": "plan",
                "route": agent_plan.route.value,
                "confidence": agent_plan.confidence,
                "required_tools": agent_plan.required_tools,
            },
        )

        input_messages: list[Any] = []
        for turn in history_turns:
            input_messages.append({"role": "user", "content": turn.question})
            input_messages.append({"role": "assistant", "content": turn.answer})
        input_messages.append({"role": "user", "content": cleaned_question})
        # Route hint as a message keeps SYSTEM_PROMPT byte-stable so the
        # provider prompt cache survives across requests.
        input_messages.append({"role": "developer", "content": _route_hint(agent_plan)})
        tool_calls: list[dict[str, Any]] = []
        instructions = SYSTEM_PROMPT
        max_tool_calls = max(1, self.settings.agent_max_tool_calls)
        tools = _tool_schemas_for_plan(agent_plan.required_tools)

        if selected_resolution is not None:
            base_resolutions = (
                _resolutions_from_plan_players(query_plan)
                if stored_plan is not None
                else self.player_resolver.resolve_many(
                    query_plan.raw_player_mentions,
                    limit=5,
                )
            )
            resolutions = _merge_selected_resolution(
                base_resolutions, selected_resolution
            )
            self._record_tool_call(
                name="resolve_player",
                args={"name": selected_resolution.raw_name, "limit": 5},
                result=self._resolution_result(selected_resolution),
                started=monotonic(),
                tool_calls=tool_calls,
                trace=trace,
                progress_callback=progress_callback,
            )
        else:
            resolutions = self.player_resolver.resolve_many(
                query_plan.raw_player_mentions,
                limit=5,
            )
        query_plan.resolved_players = _resolved_players_payload(resolutions)
        query_plan.unresolved_players = [
            resolution.raw_name
            for resolution in resolutions
            if resolution.status != "ok"
        ]
        needs_players = query_plan.route not in {
            AgentRoute.RANKING,
            AgentRoute.CLARIFY,
        }
        ok_resolution_count = len(query_plan.resolved_players)
        # Only clarify over the player/target, never over vague metrics: "stats"
        # or "how is he doing" is answered with the default key-metric set, so a
        # resolved player should get an analysis, not a follow-up question. A
        # planner clarification flag is honored only for routes that have no
        # player to anchor on (e.g. ranking) or when no usable player resolved.
        missing_target = needs_players and ok_resolution_count == 0
        needs_clarification = missing_target or (
            query_plan.needs_clarification and not needs_players
        )
        if query_plan.route == AgentRoute.COMPARE and ok_resolution_count < 2:
            needs_clarification = True
        if needs_clarification:
            for resolution in resolutions:
                started = monotonic()
                self._record_tool_call(
                    name="resolve_player",
                    args={"name": resolution.raw_name, "limit": 5},
                    result=self._resolution_result(resolution),
                    started=started,
                    tool_calls=tool_calls,
                    trace=trace,
                    progress_callback=progress_callback,
                )
            if trace is not None:
                trace.outcome = "clarified"
            return self._clarification_payload(
                query_plan=query_plan,
                agent_plan=agent_plan,
                resolutions=resolutions,
                conversation_id=conversation_id,
                tool_calls=tool_calls,
            )

        evidence = self._build_evidence_bundle(
            query_plan=query_plan,
            resolutions=resolutions,
            tool_calls=tool_calls,
            trace=trace,
            progress_callback=progress_callback,
            max_tool_calls=max_tool_calls,
        )
        if evidence is not None:
            if evidence.get("tool_limit_reached"):
                payload = _default_agent_answer(
                    "I hit the tool-call limit before finishing. Try a narrower question."
                )
                payload["tool_calls"] = tool_calls
                payload["conversation_id"] = conversation_id
                payload["agent_plan"] = agent_plan.model_dump(mode="json")
                payload["query_plan"] = query_plan.model_dump(mode="json")
                payload["resolved_players"] = query_plan.resolved_players
                payload["player_profile"] = self._player_profile_for_resolutions(
                    resolutions
                )
                payload["clarification_options"] = []
                if trace is not None:
                    trace.outcome = "tool_limit"
                return payload
            evidence_payload = self._answer_from_evidence(
                client=client,
                cleaned_question=cleaned_question,
                input_messages=input_messages,
                evidence=evidence,
                query_plan=query_plan,
                agent_plan=agent_plan,
                tool_calls=tool_calls,
                conversation_id=conversation_id,
                trace=trace,
                model=selected_model,
                provider=provider_name,
                resolutions=resolutions,
                progress_callback=progress_callback,
            )
            if evidence_payload is not None:
                return evidence_payload

        while True:
            response = self._create_response(
                client=client,
                model=selected_model,
                instructions=instructions,
                input_messages=input_messages,
                tools=tools,
                text=TEXT_FORMAT,
                timeout_seconds=self._request_timeout_seconds(provider_name),
                provider=provider_name,
            )
            if trace is not None:
                trace.add_usage(getattr(response, "usage", None))

            output_items = list(getattr(response, "output", []) or [])
            function_calls = [
                item
                for item in output_items
                if getattr(item, "type", None) == "function_call"
            ]
            if not function_calls:
                output_text = str(getattr(response, "output_text", "") or "")
                try:
                    parsed = json.loads(output_text)
                except json.JSONDecodeError:
                    parsed = _default_agent_answer(output_text or "No answer returned.")
                payload = normalize_agent_answer(parsed)
                payload["tool_calls"] = tool_calls
                payload["conversation_id"] = conversation_id
                payload["agent_plan"] = agent_plan.model_dump(mode="json")
                payload["query_plan"] = query_plan.model_dump(mode="json")
                payload["resolved_players"] = query_plan.resolved_players
                payload["player_profile"] = self._player_profile_for_resolutions(
                    resolutions
                )
                payload["clarification_options"] = []
                if conversation_id and self.conversation_store is not None:
                    self.conversation_store.append_turn(
                        conversation_id,
                        question=cleaned_question,
                        answer=str(payload.get("answer") or ""),
                        max_turns=self.settings.agent_conversation_max_turns,
                    )
                if trace is not None:
                    trace.outcome = "answered"
                return payload

            input_messages.extend(output_items)
            hit_tool_limit = False
            for call in function_calls:
                name = str(getattr(call, "name", ""))
                try:
                    args = json.loads(getattr(call, "arguments", "{}") or "{}")
                except json.JSONDecodeError:
                    args = {}
                started = monotonic()
                _emit_progress(
                    progress_callback,
                    {"type": "tool_start", "name": name, "args": args},
                )
                if len(tool_calls) >= max_tool_calls:
                    result = {
                        "status": "tool_limit",
                        "message": "Tool-call limit reached before this call executed.",
                    }
                    hit_tool_limit = True
                else:
                    result = self.tool_runner.call_tool(name, args)
                duration_ms = int((monotonic() - started) * 1000)
                status = str(result.get("status", "ok"))
                if trace is not None:
                    tool_record = trace.add_tool(
                        name=name,
                        args=args,
                        status=status,
                        duration_ms=duration_ms,
                        result=result,
                    )
                else:
                    tool_record = {
                        "name": name,
                        "args": args,
                        "status": status,
                        "duration_ms": duration_ms,
                        "result": summarize_tool_result(result),
                    }
                tool_calls.append(tool_record)
                _emit_progress(
                    progress_callback,
                    {
                        "type": "tool_end",
                        "name": name,
                        "status": status,
                        "duration_ms": duration_ms,
                    },
                )
                input_messages.append(
                    {
                        "type": "function_call_output",
                        "call_id": getattr(call, "call_id", ""),
                        "output": json.dumps(result, default=str),
                    }
                )
            if hit_tool_limit:
                payload = _default_agent_answer(
                    "I hit the tool-call limit before finishing. Try a narrower question."
                )
                payload["tool_calls"] = tool_calls
                payload["conversation_id"] = conversation_id
                payload["agent_plan"] = agent_plan.model_dump(mode="json")
                payload["query_plan"] = query_plan.model_dump(mode="json")
                payload["resolved_players"] = query_plan.resolved_players
                payload["player_profile"] = self._player_profile_for_resolutions(
                    resolutions
                )
                payload["clarification_options"] = []
                if trace is not None:
                    trace.outcome = "tool_limit"
                return payload
