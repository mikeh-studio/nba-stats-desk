from __future__ import annotations

import json
from time import monotonic, sleep
from typing import Any, Callable

from app.agent.conversation import ConversationStore
from app.agent.observability import AgentTrace, summarize_tool_result
from app.agent.planner import QueryPlan, build_query_plan
from app.agent.player_resolver import PlayerResolution, PlayerResolver
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


def _is_transient_openai_error(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int) and (
        status_code in {408, 409, 429} or status_code >= 500
    ):
        return True
    exc_name = type(exc).__name__.casefold()
    text = str(exc).casefold()
    return "timeout" in exc_name or "timeout" in text or "timed out" in text


def _tool_schemas_for_plan(required_tools: list[str]) -> list[dict[str, Any]]:
    schemas = get_tool_schemas()
    if not required_tools:
        return schemas
    by_name = {str(schema["name"]): schema for schema in schemas}
    ordered = [by_name[name] for name in required_tools if name in by_name]
    ordered.extend(schema for schema in schemas if schema["name"] not in required_tools)
    return ordered


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


class StatsAgent:
    def __init__(
        self,
        settings: Settings,
        repo: WarehouseRepository,
        *,
        client: Any | None = None,
        tool_runner: StatsToolRunner | None = None,
        conversation_store: ConversationStore | None = None,
        player_resolver: PlayerResolver | None = None,
    ) -> None:
        self.settings = settings
        self.repo = repo
        self.client = client
        self.tool_runner = tool_runner or StatsToolRunner(
            repo,
            cache_ttl_seconds=settings.agent_cache_ttl_seconds,
        )
        self.conversation_store = conversation_store
        self.player_resolver = player_resolver or PlayerResolver(
            repo,
            min_confidence=settings.agent_player_match_min_confidence,
        )

    def _get_client(self) -> Any:
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

    def _create_response(
        self,
        *,
        client: Any,
        instructions: str,
        input_messages: list[Any],
        tools: list[dict[str, Any]] | None,
        text: dict[str, Any],
        timeout_seconds: float,
    ) -> Any:
        request_client = client
        create_extra: dict[str, Any] = {}
        if hasattr(client, "with_options"):
            request_client = client.with_options(timeout=timeout_seconds)
        else:
            create_extra["timeout"] = timeout_seconds
        attempts = max(0, self.settings.openai_agent_max_retries) + 1
        delay = max(0.0, self.settings.openai_agent_retry_base_delay_seconds)
        last_exc: Exception | None = None
        for attempt in range(attempts):
            try:
                kwargs: dict[str, Any] = {
                    "model": self.settings.openai_agent_model,
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
                if attempt >= attempts - 1 or not _is_transient_openai_error(exc):
                    break
                sleep(delay * (2**attempt))
        raise AgentExecutionError("OpenAI agent request failed.") from last_exc

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
        for resolution in resolutions:
            options.extend(
                {
                    "raw_name": resolution.raw_name,
                    "player_id": candidate.player_id,
                    "player_name": candidate.player_name,
                    "team_abbr": candidate.team_abbr,
                    "confidence": candidate.confidence,
                }
                for candidate in resolution.candidates
            )
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
        return payload

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
    ) -> dict[str, Any] | None:
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
        response = self._create_response(
            client=client,
            instructions=f"{EVIDENCE_PROMPT}\n\n{_route_hint(agent_plan)}",
            input_messages=evidence_messages,
            tools=None,
            text=TEXT_FORMAT,
            timeout_seconds=self.settings.openai_agent_timeout_seconds,
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
        payload["tool_calls"] = tool_calls
        payload["conversation_id"] = conversation_id
        payload["agent_plan"] = agent_plan.model_dump(mode="json")
        payload["query_plan"] = query_plan.model_dump(mode="json")
        payload["resolved_players"] = evidence.get("resolved_players", [])
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
    ) -> dict[str, Any]:
        cleaned_question = question.strip()
        if not cleaned_question:
            raise ValueError("Question must not be blank.")
        if not self.settings.openai_agent_enabled:
            raise AgentDisabledError("The OpenAI stats agent is disabled.")
        if self.client is None and not self.settings.openai_api_key:
            self._get_client()

        agent_plan = build_agent_plan(cleaned_question)
        if agent_plan.needs_clarification:
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
            payload = _default_agent_answer(
                agent_plan.clarification_question
                or "Can you clarify which player or metric you want?"
            )
            payload["tool_calls"] = []
            payload["conversation_id"] = conversation_id
            payload["agent_plan"] = agent_plan.model_dump(mode="json")
            if trace is not None:
                trace.outcome = "clarified"
            return payload

        client = self._get_client()
        history_turns = (
            self.conversation_store.get_turns(
                conversation_id,
                max_turns=self.settings.agent_conversation_max_turns,
            )
            if conversation_id and self.conversation_store is not None
            else []
        )
        planning_question = cleaned_question
        if history_turns:
            planning_question = (
                f"Previous question: {history_turns[-1].question}\n"
                f"Follow-up question: {cleaned_question}"
            )
        query_plan = build_query_plan(
            planning_question,
            settings=self.settings,
            client=client,
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
        tool_calls: list[dict[str, Any]] = []
        instructions = f"{SYSTEM_PROMPT}\n\n{_route_hint(agent_plan)}"
        max_tool_calls = max(1, self.settings.agent_max_tool_calls)
        tools = _tool_schemas_for_plan(agent_plan.required_tools)

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
        needs_clarification = query_plan.needs_clarification or (
            needs_players and ok_resolution_count == 0
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
            )
            if evidence_payload is not None:
                return evidence_payload

        while True:
            response = self._create_response(
                client=client,
                instructions=instructions,
                input_messages=input_messages,
                tools=tools,
                text=TEXT_FORMAT,
                timeout_seconds=self.settings.openai_agent_timeout_seconds,
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
                payload["clarification_options"] = []
                if trace is not None:
                    trace.outcome = "tool_limit"
                return payload
