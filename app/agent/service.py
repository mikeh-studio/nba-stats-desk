from __future__ import annotations

from time import monotonic
from typing import Any, Callable

from app.agent.conversation import ConversationStore
from app.agent.execution import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    ExecutionFailure,
    create_response,
    governed_request,
)
from app.agent.legacy_loader import build_query_plan
from app.agent.legacy_loader import legacy_available as legacy_available
from app.agent.legacy_service import (
    ProgressCallback as ProgressCallback,
)
from app.agent.legacy_service import (
    _AnswerFieldStream as _AnswerFieldStream,
)
from app.agent.legacy_service import (
    _emit_progress,
    answer_legacy_repository,
)
from app.agent.legacy_service import (
    normalize_agent_answer as normalize_agent_answer,
)
from app.agent.name_matching import (
    PlayerNameError,
    run_name_matching,
)
from app.agent.name_matching import (
    _clarify_reply_name as _clarify_reply_name,
)
from app.agent.observability import AgentTrace, summarize_tool_result
from app.agent.player_resolver import PlayerResolver
from app.agent.tools import StatsToolRunner
from app.config import Settings
from app.repository import WarehouseRepository


class AgentDisabledError(RuntimeError):
    """Raised when the stats agent is not configured to call OpenAI."""


class AgentExecutionError(RuntimeError):
    """Raised when an LLM provider agent run fails."""


class StatsAgent:
    def __init__(
        self,
        settings: Settings,
        repo: WarehouseRepository,
        *,
        client: Any | None = None,
        claude_client: Any | None = None,
        openrouter_client: Any | None = None,
        tool_runner: StatsToolRunner | None = None,
        conversation_store: ConversationStore | None = None,
        player_resolver: PlayerResolver | None = None,
        semantic_warehouse: Any | None = None,
    ) -> None:
        from app.agent.runtime_policy import get_runtime_policy

        if get_runtime_policy() is not None:
            conversation_store = None
        self.settings = settings
        self.repo = repo
        from app.agent.semantic_answer import SemanticAsk
        from app.agent.semantic_serving import warehouse_for_repository
        from app.repository import BigQueryWarehouseRepository

        if semantic_warehouse is None and isinstance(repo, BigQueryWarehouseRepository):
            semantic_warehouse = warehouse_for_repository(repo)
        self.semantic_agent = (
            SemanticAsk(settings, semantic_warehouse, conversation_store)
            if semantic_warehouse is not None
            else None
        )
        self.client = client
        self.claude_client = claude_client
        self.openrouter_client = openrouter_client
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
        from app.agent.runtime_policy import get_runtime_policy, guard_client

        runtime = get_runtime_policy()
        if runtime is not None and provider != "openai":
            raise ExecutionFailure("runtime_selection_denied")
        if provider == "openrouter":
            return self._get_openrouter_client()
        if provider == "claude":
            return self._get_claude_client()
        if self.client is not None:
            return guard_client(self.client, provider)
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
        self.client = OpenAI(api_key=self.settings.openai_api_key, max_retries=0)
        return guard_client(self.client, provider)

    def _get_openrouter_client(self) -> Any:
        from app.agent.runtime_policy import get_runtime_policy

        if get_runtime_policy() is not None:
            raise ExecutionFailure("runtime_selection_denied")
        if self.openrouter_client is not None:
            return self.openrouter_client
        if not self.settings.openai_agent_enabled:
            raise AgentDisabledError("The stats agent is disabled.")
        if not self.settings.openrouter_api_key:
            raise AgentDisabledError(
                "OPENROUTER_API_KEY is required to use OpenRouter for Ask NBA Stats."
            )
        from openai import OpenAI

        from app.agent.openrouter_client import OpenRouterResponsesClient

        self.openrouter_client = OpenRouterResponsesClient(
            OpenAI(
                api_key=self.settings.openrouter_api_key,
                base_url="https://openrouter.ai/api/v1",
                max_retries=0,
            )
        )
        return self.openrouter_client

    def _get_claude_client(self) -> Any:
        from app.agent.runtime_policy import get_runtime_policy

        if get_runtime_policy() is not None:
            raise ExecutionFailure("runtime_selection_denied")
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
        if provider == "openrouter":
            return self.settings.openrouter_agent_timeout_seconds
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
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    ) -> Any:
        kwargs: dict[str, Any] = dict(
            model=model, instructions=instructions, input=input_messages, text=text
        )
        if tools is not None:
            kwargs["tools"] = tools
        if on_output_text_delta is not None:
            kwargs["on_output_text_delta"] = on_output_text_delta
        return create_response(
            client,
            settings=self.settings,
            provider=provider,
            timeout_seconds=timeout_seconds,
            max_output_tokens=max_output_tokens,
            **kwargs,
        )

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

    @governed_request
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
        from app.agent.route_labels import finish_route
        from app.agent.semantics import SemanticError
        from app.agent.time_defaults import resolve_recent_season
        from app.agent.visualization import VisualizationAgent

        routing: dict[str, str] = {}
        time_notes = []
        try:
            effective_question, time_notes = resolve_recent_season(question)
        except SemanticError as exc:
            from app.agent.payload import answer_payload

            routing.update(route="clarify", source="deterministic")
            payload = answer_payload(
                str(exc), status=exc.code, conversation_id=conversation_id
            )
        else:
            time_scope = dict(
                period_question=question if time_notes else None, assumptions=time_notes
            )
            payload = run_name_matching(
                self,
                effective_question,
                selected_player,
                conversation_id,
                lambda effective_question, selection: self._answer(
                    effective_question,
                    conversation_id=conversation_id,
                    trace=trace,
                    progress_callback=progress_callback,
                    selected_player=selection,
                    provider=provider,
                    model=model,
                    routing=routing,
                    period_question=time_scope["period_question"],
                ),
                trace=trace,
                time_scope=time_scope,
            )
            time_notes = time_scope["assumptions"]
        payload.setdefault("assumptions", []).extend(time_notes)
        payload = finish_route(
            payload,
            route=routing["route"],
            source=routing["source"],
            trace=trace,
        )
        provider_name = (provider or "openai").strip().lower()
        selected_model = model or (
            self.settings.openrouter_agent_model
            if provider_name == "openrouter"
            else self.settings.anthropic_agent_model
            if provider_name == "claude"
            else self.settings.openai_agent_model
        )
        from app.agent.claims import finalize
        from app.agent.payload import complete_payload

        return finalize(
            complete_payload(
                VisualizationAgent().enrich(
                    self, payload, question, provider_name, selected_model, trace
                )
            ),
            mode=self.settings.ask_claim_enforcement,
        )

    def _answer(
        self,
        question: str,
        *,
        conversation_id: str | None = None,
        trace: AgentTrace | None = None,
        progress_callback: ProgressCallback | None = None,
        selected_player: dict[str, Any] | None = None,
        provider: str | None = None,
        model: str | None = None,
        routing: dict[str, str] | None = None,
        period_question: str | None = None,
    ) -> dict[str, Any]:
        cleaned_question = question.strip()
        if not cleaned_question:
            raise ValueError("Question must not be blank.")
        provider_name = (provider or "openai").strip().lower()
        if provider_name not in {"openai", "claude", "openrouter"}:
            raise ValueError(f"Unknown agent provider: {provider_name}")
        selected_model = (
            model.strip()
            if model and model.strip()
            else (
                self.settings.openrouter_agent_model
                if provider_name == "openrouter"
                else self.settings.anthropic_agent_model
                if provider_name == "claude"
                else self.settings.openai_agent_model
            )
        )
        from app.agent.runtime_policy import get_runtime_policy

        runtime = get_runtime_policy()
        if runtime is not None:
            runtime.validate_selection(provider_name, selected_model)
        if not self.settings.openai_agent_enabled:
            raise AgentDisabledError("The OpenAI stats agent is disabled.")
        from app.agent.intelligence import get_provider

        intelligence = get_provider(self.settings)

        from app.agent.appearance_ask import answer_appearances
        from app.agent.availability_ask import answer_availability
        from app.agent.payload import answer_payload
        from app.agent.reference_ask import answer_reference
        from app.agent.research_ask import answer_research
        from app.agent.route_labels import DISPATCH_LABELS, route_hint
        from app.agent.routes import (
            dispatch_source,
            prepare_route_context,
            select_route,
        )
        from app.agent.scope_policy import fantasy_decision, round_scope_message
        from app.agent.teammate_ask import answer_study

        if decision := fantasy_decision(cleaned_question):
            if routing is not None:
                routing.update(route="clarify", source="deterministic")
            if trace:
                trace.set_plan(route="clarify", confidence=1.0)
                trace.outcome = "clarification_required"
            return route_hint(
                answer_payload(
                    decision["message"],
                    status="clarification_required",
                    conversation_id=conversation_id,
                ),
                reason_code="fantasy_scope",
            )

        route_context, keep_pending, abandoned_pending_route = prepare_route_context(
            cleaned_question,
            settings=self.settings,
            governed=self.semantic_agent is not None,
            store=self.conversation_store,
            conversation_id=conversation_id,
            selected_player=selected_player,
            period_question=period_question,
        )
        publication_id = route_context.context.get("publication_id")
        pending_route = (
            self.conversation_store.get_pending_clarification(conversation_id)
            if conversation_id and self.conversation_store
            else None
        )
        if (
            keep_pending
            and pending_route
            and (pending_route.query_plan or {}).get("publication_retry")
        ):
            publication_id = publication_id or pending_route.publication_id
        from app.agent.award_lookup import wants_award

        participation = None
        if not wants_award(cleaned_question) and round_scope_message(cleaned_question):
            from app.agent.participation import answer_known_nonparticipation

            participation = answer_known_nonparticipation(
                cleaned_question, self.settings.season, conversation_id
            )
            if participation is not None and not publication_id:
                if routing is not None:
                    routing.update(route="event_participation", source="deterministic")
                if trace:
                    trace.set_plan(route="event_participation", confidence=1.0)
                    trace.outcome = "not_applicable"
                return route_hint(participation, route="event_participation")
        route = select_route(route_context)
        if routing is not None:
            routing.update(
                route=DISPATCH_LABELS.get(route.key, route.key),
                source=dispatch_source(route_context, route),
            )
        if trace:
            trace.set_plan(route=route.key, confidence=1.0)
        if publication_id and (
            participation is not None or route.handler != "semantic"
        ):
            if trace:
                trace.outcome = "unsupported_scope"
            return answer_payload(
                "Start a separate analysis for a question that uses a different source.",
                status="unsupported_scope",
                conversation_id=conversation_id,
            )
        if route.handler == "scope_refusal":
            if trace:
                trace.outcome = "unsupported_scope"
                trace.error_type = "unsupported_scope"
            return route_hint(
                answer_payload(
                    round_scope_message(route_context.routing_question)
                    or "Unsupported scope",
                    status="unsupported_scope",
                    conversation_id=conversation_id,
                ),
                reason_code="unsupported_round",
            )
        if route.handler == "appearances":
            return answer_appearances(
                self, cleaned_question, route_context.context, conversation_id, trace
            )
        if route.handler == "availability":
            return answer_availability(self, cleaned_question, conversation_id, trace)
        if route.handler == "research":
            return answer_research(
                self,
                cleaned_question,
                provider_name,
                selected_model,
                conversation_id,
                trace,
            )
        if route.handler == "reference":
            try:
                return answer_reference(
                    self,
                    cleaned_question,
                    route.key,
                    conversation_id,
                    trace,
                    selected_player,
                    route_context=route_context,
                )
            except PlayerNameError:
                raise
            except Exception as exc:
                raise AgentExecutionError("Governed reference request failed") from exc
        if route.handler == "teammate_study":
            try:
                payload = answer_study(
                    self,
                    route_context.study_question,
                    provider_name,
                    selected_model,
                    trace,
                )
                payload["conversation_id"] = conversation_id
                if trace:
                    trace.outcome = payload["study_status"]
                if conversation_id and self.conversation_store:
                    self.conversation_store.append_turn(
                        conversation_id,
                        question=cleaned_question,
                        answer=payload["answer"],
                        max_turns=self.settings.agent_conversation_max_turns,
                        context={
                            "teammate_study": payload["study_status"] == "answered",
                            "route": "teammate_study_v1",
                        },
                    )
                return payload
            except PlayerNameError:
                raise
            except Exception as exc:
                raise AgentExecutionError("Teammate study request failed") from exc
        if route.handler == "semantic":
            try:
                assert self.semantic_agent is not None
                from app.agent.semantic_answer import try_published_three_point_leaders

                if (
                    route.key != "playoff_round"
                    and (
                        payload := try_published_three_point_leaders(
                            self,
                            cleaned_question,
                            route_context.context,
                            conversation_id,
                            runtime=runtime,
                            selected_player=selected_player,
                            keep_pending=keep_pending,
                        )
                    )
                    is not None
                ):
                    return payload
                payload = self.semantic_agent.answer(
                    cleaned_question,
                    client=self._get_client(provider_name)
                    if intelligence.descriptor.requires_model
                    else None,
                    model=selected_model,
                    conversation_id=conversation_id,
                    selected_player=selected_player,
                    trace=trace,
                    progress_callback=progress_callback,
                )
                return payload
            except (ValueError, PlayerNameError):
                raise
            except Exception as exc:
                if (
                    isinstance(exc, ExecutionFailure)
                    and exc.code == "provider_unavailable"
                ):
                    raise
                raise AgentExecutionError("Governed analytics request failed") from exc

        if not legacy_available():
            raise AgentDisabledError("Legacy repository analytics are unavailable.")
        return answer_legacy_repository(
            self,
            cleaned_question,
            conversation_id=conversation_id,
            trace=trace,
            progress_callback=progress_callback,
            selected_player=selected_player,
            provider_name=provider_name,
            selected_model=selected_model,
            abandoned_pending_route=abandoned_pending_route,
            build_query_plan=build_query_plan,
        )
