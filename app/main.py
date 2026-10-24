from __future__ import annotations

import json
import logging
import time
from collections.abc import MutableMapping
from contextlib import asynccontextmanager
from contextvars import copy_context
from copy import deepcopy
from datetime import UTC, date, datetime
from functools import lru_cache
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Annotated, Any, Literal
from uuid import uuid4

from anyio.to_thread import current_default_thread_limiter
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app.agent.capacity import CapacityExceeded, Delivery, admission
from app.agent.conversation import get_conversation_store
from app.agent.followup import (
    analysis_context,
    hydrate_availability_context,
    successful_analysis,
)
from app.agent.history import (
    append_history_turn,
    clear_history,
    read_history,
    saved_context_question,
)
from app.agent.observability import LOGGER_NAME, AgentTrace
from app.agent.private_traces import PrivateCapture
from app.agent.service import AgentDisabledError, StatsAgent
from app.config import (
    AGENT_MODEL_OPTIONS,
    AGENT_MODEL_VALUES,
    Settings,
    get_settings,
)
from app.rate_limit import get_agent_rate_limiter
from app.repository import (
    BigQueryWarehouseRepository,
    CompareFocus,
    CompareWindow,
    WarehouseRepository,
    get_compare_focus_options,
    get_compare_window_options,
)
from app.research import (
    ResearchQuery,
    available_research,
    breakdown,
    load_context,
    load_research_evidence,
)
from app.research_studies import catalog as research_catalog
from app.seasons import (
    DEFAULT_SEASON,
    SEASONS,
    _selected_season,
    current_season,
    settings_for_season,
    validate_season,
)
from app.security import is_direct_local, rate_limit_identity, require_local_history
from app.telemetry import instrument_compare_view, instrument_player_view
from app.what_changed import ComparisonPeriod, SeasonPhase, WhatChangedUnavailable

BASE_DIR = Path(__file__).resolve().parent
STATIC_VERSION = "20261023-ask-stable-buttons-v5"
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.globals["static_version"] = STATIC_VERSION
templates.env.globals["available_seasons"] = SEASONS
TRACKING_CAP = 8
HEALTH_CACHE_TTL_SECONDS = 60
PERFORMANCE_CACHE_TTL_SECONDS = 900
PERFORMANCE_STALE_TTL_SECONDS = 3600
PLAYER_DETAIL_CACHE_TTL_SECONDS = 300
PLAYER_DETAIL_STALE_TTL_SECONDS = 900
PERFORMANCE_INITIAL_DEFAULT_LIMIT = 240
PAYLOAD_CACHE_MAX_ENTRIES = 256
_health_cache: dict[int, tuple[float, dict[str, Any]]] = {}
_payload_cache: dict[tuple[Any, ...], tuple[float, Any]] = {}
_payload_cache_lock = Lock()
_payload_refreshing: set[tuple[Any, ...]] = set()
_payload_building: dict[tuple[Any, ...], Event] = {}
_repo_refs: dict[int, Any] = {}
app_logger = logging.getLogger(__name__)


class CacheControlledStaticFiles(StaticFiles):
    async def get_response(
        self, path: str, scope: MutableMapping[str, Any]
    ) -> Response:
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            query = (scope.get("query_string") or b"").decode("utf-8", "ignore")
            cache_control = (
                "public, max-age=31536000, immutable"
                if "v=" in query
                else "public, max-age=300"
            )
            response.headers.setdefault("Cache-Control", cache_control)
            response.headers.setdefault("X-Content-Type-Options", "nosniff")
        return response


class PriorPlayer(BaseModel):
    player_id: int = Field(ge=1)
    player_name: str = Field(min_length=1, max_length=80)
    role: Literal["focal", "teammate"] | None = None


class PriorScope(BaseModel):
    season: str | None = Field(default=None, max_length=7)
    start: date | None = None
    end: date | None = None
    phases: list[Literal["Regular Season", "Playoffs", "Both"]] = Field(
        default_factory=list, max_length=2
    )


class PriorAvailability(BaseModel):
    player_id: int = Field(ge=1)
    teammate_id: int = Field(ge=1)
    season: str = Field(max_length=7)
    phase: Literal["Regular Season", "Playoffs", "Both"]
    explicit_phase: bool = False
    start: date | None = None
    end: date | None = None
    home_away: Literal["home", "away"] | None = None
    opponent: str | None = Field(default=None, max_length=3)
    metrics: list[str] = Field(max_length=24)
    aggregation: Literal["average", "total"] = "average"
    include_limited_minutes: bool = False
    minutes_threshold: float = Field(default=0.5, gt=0, le=1)


class PriorAward(BaseModel):
    award_key: str = Field(min_length=1, max_length=32)
    season: str = Field(pattern=r"^20\d{2}-\d{2}$")
    performance_phase: Literal["Regular Season", "Playoffs", "Finals"]


class PriorAnalysis(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    players: list[PriorPlayer] = Field(default_factory=list, max_length=2)
    scope: PriorScope = Field(default_factory=PriorScope)
    metrics: list[str] = Field(default_factory=list, max_length=24)
    availability_scope: PriorAvailability | None = None
    split_question: str | None = Field(default=None, max_length=4000)
    reference_question: str | None = Field(default=None, max_length=4000)
    award_reference: PriorAward | None = None
    analysis_type: (
        Literal["availability", "summary", "comparison", "appearances", "player_split"]
        | None
    ) = None


class AgentAskRequest(BaseModel):
    # Question length policy lives in _validate_agent_question so every
    # violation gets the same 400 with a readable detail message.
    question: str
    conversation_id: str | None = Field(default=None, max_length=80)
    previous_context: PriorAnalysis | None = None
    # Set when the user clicks a clarification option; the agent resumes the
    # pending question with this player pinned instead of re-resolving.
    selected_player_id: int | None = Field(default=None, ge=1)
    selected_player_name: str | None = Field(default=None, max_length=80)
    # Which LLM backend answers this question; defaults to OpenAI.
    provider: Literal["openai", "claude", "openrouter"] | None = None
    # Concrete model id for the selected provider; env defaults apply when empty.
    model: str | None = Field(default=None, max_length=80)


def _selected_player(payload: AgentAskRequest) -> dict[str, Any] | None:
    name = (payload.selected_player_name or "").strip()
    if not name:
        return None
    return {"player_id": payload.selected_player_id, "player_name": name}


def _selected_agent_provider(
    payload: AgentAskRequest,
) -> Literal["openai", "claude", "openrouter"]:
    return payload.provider or "openai"


def _selected_agent_model(payload: AgentAskRequest, settings: Settings) -> str:
    provider = _selected_agent_provider(payload)
    requested = (payload.model or "").strip()
    if requested:
        if requested not in AGENT_MODEL_VALUES[provider]:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported {provider} model: {requested}",
            )
        return requested
    if provider == "openrouter":
        return settings.openrouter_agent_model
    return (
        settings.anthropic_agent_model
        if provider == "claude"
        else settings.openai_agent_model
    )


def _time_ago(value: str | None) -> str:
    if not value:
        return "unavailable"
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        diff = datetime.now(UTC) - dt
        seconds = int(diff.total_seconds())
        if seconds < 60:
            return "just now"
        if seconds < 3600:
            m = seconds // 60
            return f"{m} minute{'s' if m != 1 else ''} ago"
        if seconds < 86400:
            h = seconds // 3600
            return f"{h} hour{'s' if h != 1 else ''} ago"
        d = seconds // 86400
        return f"{d} day{'s' if d != 1 else ''} ago"
    except (ValueError, TypeError):
        return str(value)


templates.env.filters["time_ago"] = _time_ago


@asynccontextmanager
async def _lifespan(_: FastAPI):
    _start_cache_prewarm()
    yield


app = FastAPI(title="NBA Stats Desk Public API", version="1.0.0", lifespan=_lifespan)
app.mount(
    "/static",
    CacheControlledStaticFiles(directory=str(BASE_DIR / "static")),
    name="static",
)
agent_logger = logging.getLogger(LOGGER_NAME)


class SeasonMiddleware:
    """Keep season context through actual delivery without buffering responses."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        # Historical scope belongs to Ask questions or explicit API reads, not tabs.
        if (
            request.method == "GET"
            and not request.url.path.startswith(("/api/", "/static/"))
            and "season" in request.query_params
        ):
            url = request.url.remove_query_params("season")
            response = RedirectResponse(
                url.path + ("?" + url.query if url.query else "")
            )
            await response(scope, receive, send)
            return
        try:
            season = validate_season(request.query_params.get("season", DEFAULT_SEASON))
        except ValueError as exc:
            response = JSONResponse(status_code=422, content={"detail": str(exc)})
            await response(scope, receive, send)
            return
        token = _selected_season.set(season)
        try:
            await self.app(scope, receive, send)
        finally:
            _selected_season.reset(token)


class LocalDevSourceBoundary:
    """Pure ASGI guard: preserve streaming send/backpressure semantics."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and get_settings().ask_local_espn_dev:
            request = Request(scope)
            if not is_direct_local(request):
                response = JSONResponse(
                    status_code=403,
                    content={
                        "detail": "ESPN dev review requires direct localhost access"
                    },
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


app.add_middleware(LocalDevSourceBoundary)
app.add_middleware(SeasonMiddleware)


def _dependency_override_value(callable_: Any) -> Any | None:
    override = app.dependency_overrides.get(callable_)
    if override is None:
        return None
    return override()


@lru_cache(maxsize=8)
def _cached_repository(settings: Settings) -> BigQueryWarehouseRepository:
    return BigQueryWarehouseRepository(settings)


def get_repository(
    settings: Annotated[Settings, Depends(get_settings)],
) -> WarehouseRepository:
    return _cached_repository(settings_for_season(settings, current_season()))


def get_agent_client() -> Any | None:
    return None


def _repo_cache_token(repo: WarehouseRepository) -> int:
    # Cache keys embed id(repo); pin the repo so CPython cannot recycle this
    # id for a different repository while keyed cache entries are still alive.
    token = id(repo)
    _repo_refs[token] = repo
    return token


def _get_cached_health(repo: WarehouseRepository) -> dict[str, Any]:
    key = _repo_cache_token(repo)
    now = time.monotonic()
    cached = _health_cache.get(key)
    if cached is not None:
        cached_at, payload = cached
        if now - cached_at < HEALTH_CACHE_TTL_SECONDS:
            return dict(payload)
    payload = repo.get_health()
    _health_cache[key] = (now, dict(payload))
    return payload


def _performance_initial_cache_key(
    repo: WarehouseRepository,
    *,
    game_date: str | None,
    game_id: str | None,
    limit: int,
) -> tuple[Any, ...]:
    return (_repo_cache_token(repo), "performance_initial", game_date, game_id, limit)


def _player_detail_cache_key(
    repo: WarehouseRepository, player_id: int
) -> tuple[Any, ...]:
    return (_repo_cache_token(repo), "player_detail", player_id)


def _store_payload_locked(key: tuple[Any, ...], payload: Any) -> None:
    # A deep copy is stored so later builder/caller mutations cannot reach the
    # cache; reads hand out the cached object directly, so handlers must treat
    # cached payloads as immutable.
    # Keys embed user-controlled query params (game_id, limit, dates), so the
    # cache must stay bounded; evict the oldest entries past the cap.
    _payload_cache[key] = (time.monotonic(), deepcopy(payload))
    while len(_payload_cache) > PAYLOAD_CACHE_MAX_ENTRIES:
        oldest_key = min(_payload_cache, key=lambda item: _payload_cache[item][0])
        _payload_cache.pop(oldest_key, None)


def _get_cached_payload(
    key: tuple[Any, ...],
    ttl_seconds: int,
    builder: Any,
    *,
    stale_ttl_seconds: int = 0,
) -> Any:
    now = time.monotonic()
    with _payload_cache_lock:
        cached = _payload_cache.get(key)
        if cached is not None:
            cached_at, payload = cached
            if now - cached_at < ttl_seconds:
                return payload
            if stale_ttl_seconds and now - cached_at < stale_ttl_seconds:
                if key not in _payload_refreshing:
                    _payload_refreshing.add(key)
                    Thread(
                        target=_refresh_cached_payload,
                        args=(key, builder),
                        daemon=True,
                    ).start()
                return payload

        building = _payload_building.get(key)
        if building is None:
            building = Event()
            _payload_building[key] = building
            should_build = True
        else:
            should_build = False

    if not should_build:
        building.wait(timeout=min(max(1, ttl_seconds), 30))
        with _payload_cache_lock:
            cached = _payload_cache.get(key)
            if cached is not None:
                return cached[1]
        return _get_cached_payload(
            key,
            ttl_seconds,
            builder,
            stale_ttl_seconds=stale_ttl_seconds,
        )

    try:
        payload = builder()
        with _payload_cache_lock:
            _store_payload_locked(key, payload)
        return payload
    finally:
        with _payload_cache_lock:
            building.set()
            _payload_building.pop(key, None)


def _refresh_cached_payload(key: tuple[Any, ...], builder: Any) -> None:
    try:
        payload = builder()
        with _payload_cache_lock:
            _store_payload_locked(key, payload)
    except Exception:
        app_logger.warning("stale payload cache refresh failed", exc_info=True)
        return
    finally:
        with _payload_cache_lock:
            _payload_refreshing.discard(key)


def _set_public_cache_header(response: Response, ttl_seconds: int) -> None:
    response.headers["Cache-Control"] = f"public, max-age={ttl_seconds}"


def _get_cached_player_detail(
    repo: WarehouseRepository, player_id: int
) -> dict[str, Any] | None:
    return _get_cached_payload(
        _player_detail_cache_key(repo, player_id),
        PLAYER_DETAIL_CACHE_TTL_SECONDS,
        lambda: repo.get_player_detail(player_id),
        stale_ttl_seconds=PLAYER_DETAIL_STALE_TTL_SECONDS,
    )


def _agent_rate_limit_key(request: Request, settings: Settings) -> str:
    return rate_limit_identity(request, settings.agent_trusted_proxy_cidrs)


def _check_agent_rate_limit(request: Request, settings: Settings) -> None:
    if not settings.openai_agent_enabled:
        return

    key = _agent_rate_limit_key(request, settings)
    limiter = get_agent_rate_limiter(settings.agent_rate_limit_redis_url)
    decision = limiter.check(
        key=key,
        per_minute=settings.agent_rate_limit_per_minute,
        per_day=settings.agent_rate_limit_daily,
    )
    if not decision.allowed:
        if decision.scope == "day":
            detail = "Ask NBA Stats daily request limit exceeded. Try again tomorrow."
        else:
            detail = "Ask NBA Stats rate limit exceeded. Try again in a minute."
        raise HTTPException(status_code=429, detail=detail)


def _request_id() -> str:
    # Client-supplied identifiers can contain private text; log a server ID only.
    return uuid4().hex


def _conversation_id(value: str | None) -> str:
    return value or f"agent-{uuid4().hex}"


def _validate_agent_question(question: str, settings: Settings) -> str:
    cleaned = question.strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="Question must not be blank")
    if len(cleaned) > settings.agent_question_max_chars:
        raise HTTPException(
            status_code=400,
            detail=(
                "Question is too long. "
                f"Limit is {settings.agent_question_max_chars} characters."
            ),
        )
    return cleaned


def _sse(event: str, data: dict[str, Any], *, allow_nan: bool = True) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str, allow_nan=allow_nan)}\n\n"


def _agent_terminal_bytes(terminal: dict[str, Any], *, streaming: bool) -> bytes:
    # Validate/encode on the worker before history is saved. JSON retains the
    # framework's encoder semantics; neither transport permits non-finite values.
    encoded = jsonable_encoder(terminal)
    if streaming:
        return _sse(encoded["type"], encoded, allow_nan=False).encode()
    content = (
        encoded["payload"]
        if encoded["type"] == "final"
        else {"detail": encoded["detail"]}
    )
    return bytes(JSONResponse(content).body)


def _emit_agent_trace(trace: AgentTrace, *, private: bool = True) -> None:
    # A logging sink failure must not change the response or strand a lease.
    try:
        trace.emit()
    except Exception:
        pass
    if private and trace.private_capture is not None:
        trace.private_capture.finish(trace)


async def _emit_agent_trace_off_loop(trace: AgentTrace) -> None:
    try:
        await run_in_threadpool(_emit_agent_trace, trace)
    except Exception:
        # Thread exhaustion can prevent even best-effort logging after startup
        # failed. Keep the authored failure envelope and released lease intact.
        pass


def _answer_delta_chunks(answer: str, *, chunk_size: int = 120):
    text = str(answer or "")
    for index in range(0, len(text), chunk_size):
        yield text[index : index + chunk_size]


AGENT_UNAVAILABLE_DETAIL = "Ask NBA Stats is unavailable."


def _agent_unavailable_detail(payload: AgentAskRequest, settings: Settings) -> str:
    if (
        payload.provider == "openrouter"
        and settings.openai_agent_enabled
        and not settings.openrouter_api_key
    ):
        return "OpenRouter is not configured. Add OPENROUTER_API_KEY to the server environment and restart."
    return AGENT_UNAVAILABLE_DETAIL


AGENT_FAILED_DETAIL = (
    "Ask NBA Stats failed while generating an answer. Try again shortly."
)


def _fail_trace(trace: AgentTrace, exc: Exception) -> None:
    if trace.outcome not in {
        "request_timeout",
        "provider_timeout",
        "provider_failure",
        "invalid_plan",
        "invalid_response",
    }:
        trace.outcome = "error"
        trace.error_type = type(exc).__name__


@lru_cache(maxsize=3)
def _season_conversation_store(season: str):
    if season == DEFAULT_SEASON:
        return get_conversation_store()
    from app.agent.conversation import InMemoryConversationStore

    return InMemoryConversationStore()


def _build_stats_agent(
    settings: Settings,
    repo: WarehouseRepository,
    agent_client: Any | None,
) -> StatsAgent:
    return StatsAgent(
        settings_for_season(settings, current_season()),
        repo,
        client=agent_client,
        conversation_store=_season_conversation_store(current_season()),
    )


def _prepare_agent_request(
    request: Request,
    payload: AgentAskRequest,
    settings: Settings,
) -> tuple[str, str, str, AgentTrace]:
    require_local_history(request, settings)
    request_id = _request_id()
    conversation_id = _conversation_id(payload.conversation_id)
    trace = AgentTrace(
        request_id=request_id,
        question=payload.question,
        model="unvalidated",
        conversation_id=conversation_id,
    )
    if settings.agent_trace_enabled and is_direct_local(request):
        try:
            trace.private_capture = PrivateCapture(
                settings_for_season(settings, current_season()),
                trace,
                provider=payload.provider,
                transport="sse" if request.url.path.endswith("/stream") else "json",
                static_version=STATIC_VERSION,
                selected_player=_selected_player(payload),
            )
        except Exception:
            try:
                app_logger.warning("private_trace_init_failed")
            except Exception:
                pass
    try:
        trace.model = _selected_agent_model(payload, settings)
        question = _validate_agent_question(payload.question, settings)
        _check_agent_rate_limit(request, settings)
    except HTTPException as exc:
        trace.outcome = "error"
        trace.error_type = f"HTTP_{exc.status_code}"
        exc.headers = {**(exc.headers or {}), "X-Request-ID": request_id}
        _emit_agent_trace(trace)
        raise
    return request_id, conversation_id, question, trace


def _restore_agent_context(
    payload: AgentAskRequest, settings: Settings, conversation_id: str
) -> None:
    # The local history file survives server restarts; recover conversational
    # context without rerunning saved analyses or exposing history remotely.
    store = _season_conversation_store(current_season())
    if (
        payload.conversation_id
        and settings.agent_history_enabled
        and not store.get_turns(conversation_id, max_turns=1)
        and not store.get_pending_clarification(conversation_id)
    ):
        saved = read_history(
            settings_for_season(settings, current_season()).agent_history_path,
            conversation_id=conversation_id,
            limit=1,
        ).get("conversations", [])
        if saved:
            recovered_context: dict[str, Any] = {}
            for turn in saved[0]["turns"]:
                stored_payload = turn["payload"]
                if stored_payload.get("status") == "clarification_required":
                    from app.agent.overview_clarification import restored_overview_plan

                    store.set_pending_clarification(
                        conversation_id,
                        question=stored_payload.get("clarification_question")
                        or turn["question"],
                        query_plan=restored_overview_plan(stored_payload)
                        or stored_payload.get("semantic_plan"),
                        # History saved before route labels has no ask_route.
                        route=(stored_payload.get("ask_route") or {}).get("route"),
                        candidates=stored_payload.get("clarification_options"),
                    )
                elif successful_analysis(stored_payload):
                    next_context = analysis_context(turn["question"], stored_payload)
                    if next_context.get("availability_scope"):
                        try:
                            next_context = hydrate_availability_context(
                                next_context, settings.research_availability_path
                            )
                        except (ValueError, OSError, KeyError):
                            continue
                    if not next_context.get("players"):
                        next_context["players"] = recovered_context.get("players", [])
                    recovered_context = next_context
                    store.clear_pending_clarification(conversation_id)
                    store.append_turn(
                        conversation_id,
                        question=saved_context_question(
                            turn["question"], stored_payload
                        ),
                        answer=str(stored_payload.get("answer", "")),
                        max_turns=settings.agent_conversation_max_turns,
                        context=recovered_context,
                    )
    # Older chats exist only in browser storage. Recover bounded conversational
    # hints, never cached numbers, and never overwrite successful server state.
    if (
        payload.conversation_id
        and payload.previous_context
        and not any(
            turn.context
            for turn in store.get_turns(
                conversation_id, max_turns=settings.agent_conversation_max_turns
            )
        )
    ):
        context = payload.previous_context.model_dump(mode="json", exclude_none=True)
        from app.agent.award_lookup import hydrate_award_context
        from app.agent.semantics import SemanticError

        try:
            context = hydrate_award_context(context)
        except (ValueError, KeyError, SemanticError) as exc:
            raise HTTPException(
                status_code=400,
                detail="Saved award context could not be validated. Restate the award and season.",
            ) from exc
        if payload.previous_context.availability_scope:
            # Preserve nullable filters when rebuilding an availability request.
            context["availability_scope"] = (
                payload.previous_context.availability_scope.model_dump(mode="json")
            )
            try:
                context = hydrate_availability_context(
                    context, settings.research_availability_path
                )
            except (ValueError, OSError, KeyError) as exc:
                raise HTTPException(
                    status_code=400,
                    detail="Saved conversation context could not be validated. Restate the player names.",
                ) from exc
        context["browser_recovered"] = True
        if not payload.selected_player_name:
            store.clear_pending_clarification(conversation_id)
        store.append_turn(
            conversation_id,
            question=context["question"],
            answer="",
            context=context,
            max_turns=settings.agent_conversation_max_turns,
        )
    # A legacy saved leaderboard may have scope but no reference identity.
    # Fill only that missing identity; keep the server's dates and metric scope.
    turns = store.get_turns(conversation_id, max_turns=1)
    if (
        turns
        and turns[0].context
        and not turns[0].context.get("players")
        and payload.previous_context
        and payload.previous_context.players
    ):
        context = {
            **turns[0].context,
            "players": [p.model_dump() for p in payload.previous_context.players],
            "browser_recovered": True,
        }
        store.append_turn(
            conversation_id,
            question=turns[0].question,
            answer=turns[0].answer,
            context=context,
            max_turns=settings.agent_conversation_max_turns,
        )


def _record_agent_history(
    settings: Settings,
    *,
    request_id: str,
    conversation_id: str,
    question: str,
    payload: dict[str, Any],
    provider: str,
    model: str,
) -> None:
    if not settings.agent_history_enabled:
        return
    append_history_turn(
        settings_for_season(settings, current_season()).agent_history_path,
        conversation_id=conversation_id,
        request_id=request_id,
        question=question,
        provider=provider,
        model=model,
        payload=payload,
    )


@app.get("/api/rankings")
def api_rankings(
    repo: Annotated[WarehouseRepository, Depends(get_repository)],
    limit: int = Query(25, ge=1, le=100),
) -> dict:
    return {"season": current_season(), "items": repo.get_rankings(limit=limit)}


@app.get("/api/players/search")
def api_player_search(
    repo: Annotated[WarehouseRepository, Depends(get_repository)],
    settings: Annotated[Settings, Depends(get_settings)],
    q: str = Query(min_length=1, max_length=64),
) -> dict:
    query = q.strip()
    if not query:
        raise HTTPException(status_code=400, detail="Search query must not be blank")
    return {
        "season": current_season(),
        "query": query,
        "items": repo.search_players(query, limit=settings.max_search_results),
    }


@app.get("/api/players/{player_id}")
def api_player_detail(
    player_id: int,
    repo: Annotated[WarehouseRepository, Depends(get_repository)],
) -> dict:
    detail = _get_cached_player_detail(repo, player_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Player not found")
    return {"season": current_season(), "item": detail}


@app.get("/api/compare")
def api_compare(
    player_a_id: int,
    player_b_id: int,
    repo: Annotated[WarehouseRepository, Depends(get_repository)],
    *,
    window: CompareWindow = Query(default="last_5"),
    focus: CompareFocus = Query(default="balanced"),
) -> dict:
    if player_a_id == player_b_id:
        raise HTTPException(
            status_code=400,
            detail="Compare players must be different",
        )
    return repo.get_compare(player_a_id, player_b_id, window=window, focus=focus)


@app.get("/api/health")
def api_health(
    response: Response,
    repo: Annotated[WarehouseRepository, Depends(get_repository)],
) -> dict:
    _set_public_cache_header(response, HEALTH_CACHE_TTL_SECONDS)
    return _get_cached_health(repo)


@app.get("/", response_class=RedirectResponse)
def home() -> RedirectResponse:
    return RedirectResponse(
        url="/ask"
        if current_season() == DEFAULT_SEASON
        else f"/ask?season={current_season()}"
    )


@app.get("/players/{player_id}", response_class=HTMLResponse)
def player_shell(player_id: int, request: Request) -> HTMLResponse:
    # No repository dependency: navigation never waits for warehouse queries.
    return templates.TemplateResponse(
        request,
        "player_shell.html",
        {
            "request": request,
            "page_title": "Player profile",
            "season": current_season(),
            "player_id": player_id,
        },
    )


@app.get("/players/{player_id}/content", response_class=HTMLResponse)
def player_page(
    player_id: int,
    request: Request,
    repo: Annotated[WarehouseRepository, Depends(get_repository)],
) -> HTMLResponse:
    player_detail = _get_cached_player_detail(repo, player_id)
    if player_detail is None:
        raise HTTPException(status_code=404, detail="Player not found")
    instrument_player_view(
        route="/players/{player_id}",
        season=current_season(),
        player_detail=player_detail,
    )
    context = {
        "request": request,
        "page_title": f"{player_detail['player']['player_name']} Stats Outlook",
        "season": current_season(),
        "player_detail": player_detail,
        "fragment": True,
        "tracking_cap": TRACKING_CAP,
    }
    return templates.TemplateResponse(request, "player.html", context)


@app.get("/api/players/{player_id}/game-log")
def api_player_game_log(
    player_id: int,
    repo: Annotated[WarehouseRepository, Depends(get_repository)],
    limit: int = Query(30, ge=1, le=82),
    start_date: date | None = Query(None),
    end_date: date | None = Query(None),
) -> dict:
    if start_date is not None and end_date is not None and start_date > end_date:
        raise HTTPException(
            status_code=400,
            detail="start_date must be on or before end_date",
        )
    result = repo.get_player_game_log(
        player_id,
        limit=limit,
        start_date=start_date.isoformat() if start_date is not None else None,
        end_date=end_date.isoformat() if end_date is not None else None,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Player not found")
    return {"season": current_season(), "item": result}


def _build_performance_initial_payload(
    repo: WarehouseRepository,
    *,
    game_date: str | None,
    game_id: str | None,
    limit: int,
) -> dict[str, Any]:
    return repo.get_recent_performance_initial(
        game_date=game_date,
        game_id=game_id,
        limit=limit,
    )


def _prewarm_performance_flow(repo: WarehouseRepository) -> None:
    try:
        _get_cached_health(repo)
    except Exception:
        app_logger.warning("health cache prewarm failed", exc_info=True)

    try:
        _get_cached_payload(
            _performance_initial_cache_key(
                repo,
                game_date=None,
                game_id=None,
                limit=PERFORMANCE_INITIAL_DEFAULT_LIMIT,
            ),
            PERFORMANCE_CACHE_TTL_SECONDS,
            lambda: _build_performance_initial_payload(
                repo,
                game_date=None,
                game_id=None,
                limit=PERFORMANCE_INITIAL_DEFAULT_LIMIT,
            ),
            stale_ttl_seconds=PERFORMANCE_STALE_TTL_SECONDS,
        )
    except Exception:
        app_logger.warning("performance cache prewarm failed", exc_info=True)


def _start_cache_prewarm() -> None:
    settings = _dependency_override_value(get_settings) or get_settings()
    if not settings.performance_cache_prewarm_enabled:
        return

    repo = _dependency_override_value(get_repository)
    if repo is None:
        repo = _cached_repository(settings)

    Thread(target=_prewarm_performance_flow, args=(repo,), daemon=True).start()


@app.get("/api/performance/initial")
def api_performance_initial(
    response: Response,
    repo: Annotated[WarehouseRepository, Depends(get_repository)],
    game_date: date | None = Query(None),
    game_id: str | None = Query(default=None, min_length=1, max_length=32),
    limit: int = Query(240, ge=1, le=500),
) -> dict:
    _set_public_cache_header(response, 60)
    game_date_value = game_date.isoformat() if game_date is not None else None
    return _get_cached_payload(
        _performance_initial_cache_key(
            repo,
            game_date=game_date_value,
            game_id=game_id,
            limit=limit,
        ),
        PERFORMANCE_CACHE_TTL_SECONDS,
        lambda: _build_performance_initial_payload(
            repo,
            game_date=game_date_value,
            game_id=game_id,
            limit=limit,
        ),
        stale_ttl_seconds=PERFORMANCE_STALE_TTL_SECONDS,
    )


@app.get("/api/performance/dates")
def api_performance_dates(
    response: Response,
    repo: Annotated[WarehouseRepository, Depends(get_repository)],
) -> dict:
    _set_public_cache_header(response, 60)
    return _get_cached_payload(
        (_repo_cache_token(repo), "performance_dates"),
        PERFORMANCE_CACHE_TTL_SECONDS,
        lambda: {
            "season": current_season(),
            "items": repo.get_recent_performance_dates(),
        },
        stale_ttl_seconds=PERFORMANCE_STALE_TTL_SECONDS,
    )


@app.get("/api/performance/games")
def api_performance_games(
    response: Response,
    repo: Annotated[WarehouseRepository, Depends(get_repository)],
    game_date: date | None = Query(None),
) -> dict:
    _set_public_cache_header(response, 60)
    game_date_value = game_date.isoformat() if game_date is not None else None
    return _get_cached_payload(
        (_repo_cache_token(repo), "performance_games", game_date_value),
        PERFORMANCE_CACHE_TTL_SECONDS,
        lambda: {
            "season": current_season(),
            "game_date": game_date_value,
            "items": repo.get_recent_performance_games(game_date=game_date_value),
        },
        stale_ttl_seconds=PERFORMANCE_STALE_TTL_SECONDS,
    )


@app.get("/api/performance/players")
def api_performance_players(
    response: Response,
    repo: Annotated[WarehouseRepository, Depends(get_repository)],
    game_date: date = Query(...),
    game_id: str | None = Query(default=None, min_length=1, max_length=32),
    limit: int = Query(240, ge=1, le=500),
) -> dict:
    _set_public_cache_header(response, 60)
    game_date_value = game_date.isoformat()
    return _get_cached_payload(
        (
            _repo_cache_token(repo),
            "performance_players",
            game_date_value,
            game_id,
            limit,
        ),
        PERFORMANCE_CACHE_TTL_SECONDS,
        lambda: {
            "season": current_season(),
            "game_date": game_date_value,
            "game_id": game_id,
            "items": repo.get_recent_performance_players(
                game_date=game_date_value,
                game_id=game_id,
                limit=limit,
            ),
        },
        stale_ttl_seconds=PERFORMANCE_STALE_TTL_SECONDS,
    )


@app.get("/api/performance/players/{player_id}")
def api_performance_player_detail(
    player_id: int,
    response: Response,
    repo: Annotated[WarehouseRepository, Depends(get_repository)],
    game_id: str = Query(..., min_length=1, max_length=32),
) -> dict:
    _set_public_cache_header(response, 60)
    item = _get_cached_payload(
        (_repo_cache_token(repo), "performance_player_detail", player_id, game_id),
        PERFORMANCE_CACHE_TTL_SECONDS,
        lambda: repo.get_recent_performance_player(player_id, game_id=game_id),
        stale_ttl_seconds=PERFORMANCE_STALE_TTL_SECONDS,
    )
    if item is None:
        raise HTTPException(status_code=404, detail="Performance row not found")
    return {"season": current_season(), "item": item}


class _AgentStreamingResponse(StreamingResponse):
    """Detach even when ASGI send fails while the generator is suspended."""

    def __init__(self, content, delivery: Delivery, **kwargs):
        super().__init__(content, **kwargs)
        self.delivery = delivery

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.delivery.detach()
            await self.body_iterator.aclose()


class _AgentJSONResponse(Response):
    def __init__(self, content: bytes, delivery: Delivery, **kwargs):
        super().__init__(content, media_type="application/json", **kwargs)
        self.delivery = delivery

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.delivery.detach()


async def _start_agent_execution(
    request: Request,
    payload: AgentAskRequest,
    repo: WarehouseRepository,
    settings: Settings,
    agent_client: Any | None,
    *,
    streaming: bool,
) -> tuple[str, str, Delivery]:
    # The configured rate limiter can perform synchronous Redis I/O. Preserve
    # the season context while keeping that I/O off the event loop.
    request_id, conversation_id, question, trace = await run_in_threadpool(
        _prepare_agent_request, request, payload, settings
    )
    limit = settings.agent_max_concurrent_requests
    if limit is None:
        limit = int(current_default_thread_limiter().total_tokens)
    trace.capacity["concurrency_limit"] = limit
    try:
        lease = admission.acquire(current_season(), conversation_id, limit)
    except CapacityExceeded as exc:
        trace.outcome = trace.error_type = exc.code
        await _emit_agent_trace_off_loop(trace)
        detail = (
            "This conversation already has a request in progress. Wait for it to finish."
            if exc.code == "conversation_busy"
            else "Ask NBA Stats is busy. Try again after an active request finishes."
        )
        raise HTTPException(
            status_code=409 if exc.code == "conversation_busy" else 503,
            detail=detail,
            headers={"X-Request-ID": request_id},
        ) from exc
    trace.capacity["active_at_admission"] = lease.active
    try:
        delivery = Delivery(
            max_bytes=settings.agent_stream_buffer_bytes,
            max_events=settings.agent_stream_buffer_events,
            on_detach=lambda: lease.complete(delivery=True),
        )
        request_context = copy_context()

        def progress(event: dict[str, Any]) -> None:
            delivery.put(_sse(str(event.get("type") or "progress"), event).encode())

        def worker() -> None:
            status = 502
            terminal = {
                "type": "error",
                "detail": AGENT_FAILED_DETAIL,
                "request_id": request_id,
            }
            wire = b""
            try:
                # Restore only after admission; rejected work cannot alter state.
                _restore_agent_context(payload, settings, conversation_id)
                if trace.private_capture is not None:
                    trace.private_capture.context(
                        _season_conversation_store(current_season()), conversation_id
                    )
                agent = _build_stats_agent(settings, repo, agent_client)
                from app.agent.overview_tracking import (
                    overview_events,
                    requested_capabilities,
                )

                try:
                    trace.overview_tracking.extend(requested_capabilities(question))
                except Exception:
                    app_logger.warning("overview request tracking failed")
                answer = agent.answer(
                    question,
                    conversation_id=conversation_id,
                    trace=trace,
                    progress_callback=progress if streaming else None,
                    selected_player=_selected_player(payload),
                    provider=payload.provider,
                    model=payload.model,
                )
                if trace.private_capture is not None:
                    trace.private_capture.answer(answer)
                answer["request_id"] = request_id
                answer["season"] = current_season()
                try:
                    trace.overview_tracking.extend(overview_events(answer))
                except Exception:
                    # Best-effort private telemetry cannot break answer delivery.
                    app_logger.warning("overview tracking collection failed")
                answer_streamed = answer.pop("answer_streamed", False)
                if streaming and not answer_streamed:
                    for chunk in _answer_delta_chunks(str(answer.get("answer") or "")):
                        if chunk:
                            progress({"type": "answer_delta", "delta": chunk})
                terminal = {"type": "final", "payload": answer}
                wire = _agent_terminal_bytes(terminal, streaming=streaming)
                _record_agent_history(
                    settings,
                    request_id=request_id,
                    conversation_id=conversation_id,
                    question=question,
                    payload=answer,
                    provider=_selected_agent_provider(payload),
                    model=trace.model,
                )
                status = 200
            except AgentDisabledError as exc:
                _fail_trace(trace, exc)
                agent_logger.warning("agent disabled: %s", type(exc).__name__)
                status = 503
                terminal = {
                    "type": "error",
                    "detail": _agent_unavailable_detail(payload, settings),
                    "request_id": request_id,
                }
            except HTTPException as exc:
                trace.outcome = "error"
                trace.error_type = f"HTTP_{exc.status_code}"
                status = exc.status_code
                terminal = {
                    "type": "error",
                    "detail": exc.detail,
                    "request_id": request_id,
                }
            except Exception as exc:
                _fail_trace(trace, exc)
                agent_logger.error("agent execution failed: %s", type(exc).__name__)
                status = 502
                terminal = {
                    "type": "error",
                    "detail": AGENT_FAILED_DETAIL,
                    "request_id": request_id,
                }
            finally:
                try:
                    trace.capacity.update(delivery.metrics())
                    # Error envelopes are authored locally and always serializable.
                    if status != 200 or not wire:
                        wire = _agent_terminal_bytes(terminal, streaming=streaming)
                    # A reserved single terminal result bypasses interim quotas.
                    delivery.finish(wire, status)
                    # Normal telemetry remains inside worker admission.
                    _emit_agent_trace(trace, private=False)
                finally:
                    lease.complete(worker=True)
                # Only private persistence may outlive admission; globally bounded.
                if trace.private_capture is not None:
                    trace.private_capture.finish(trace)

        Thread(target=request_context.run, args=(worker,), daemon=True).start()
    except BaseException as exc:
        lease.release()
        if not isinstance(exc, Exception):
            raise
        trace.outcome = trace.error_type = "worker_start_failure"
        await _emit_agent_trace_off_loop(trace)
        raise HTTPException(
            status_code=503,
            detail=AGENT_UNAVAILABLE_DETAIL,
            headers={"X-Request-ID": request_id},
        ) from None
    return request_id, conversation_id, delivery


@app.post("/api/agent/ask")
async def api_agent_ask(
    request: Request,
    payload: AgentAskRequest,
    repo: Annotated[WarehouseRepository, Depends(get_repository)],
    settings: Annotated[Settings, Depends(get_settings)],
    agent_client: Annotated[Any | None, Depends(get_agent_client)],
) -> Response:
    request_id, _, delivery = await _start_agent_execution(
        request, payload, repo, settings, agent_client, streaming=False
    )
    response_owns_delivery = False
    try:
        result = await delivery.next()
        assert result is not None
        wire, status = result
        result_response = _AgentJSONResponse(
            wire, delivery, status_code=status, headers={"X-Request-ID": request_id}
        )
        response_owns_delivery = True
        return result_response
    finally:
        if not response_owns_delivery:
            delivery.detach()


@app.post("/api/agent/ask/stream")
async def api_agent_ask_stream(
    request: Request,
    payload: AgentAskRequest,
    repo: Annotated[WarehouseRepository, Depends(get_repository)],
    settings: Annotated[Settings, Depends(get_settings)],
    agent_client: Annotated[Any | None, Depends(get_agent_client)],
) -> StreamingResponse:
    request_id, conversation_id, delivery = await _start_agent_execution(
        request, payload, repo, settings, agent_client, streaming=True
    )

    async def event_stream():
        try:
            yield _sse(
                "meta", {"request_id": request_id, "conversation_id": conversation_id}
            )
            while (result := await delivery.next()) is not None:
                yield result[0]
        finally:
            delivery.detach()

    return _AgentStreamingResponse(
        event_stream(),
        delivery,
        media_type="text/event-stream",
        headers={"X-Request-ID": request_id},
    )


@app.get("/api/agent/history")
def api_agent_history(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    q: str = Query(default="", max_length=500),
    conversation_id: str | None = Query(default=None, max_length=200),
) -> dict:
    require_local_history(request, settings)
    if not settings.agent_history_enabled:
        return {"conversations": []}
    return read_history(
        settings_for_season(settings, current_season()).agent_history_path,
        limit=limit,
        offset=offset,
        query=q,
        conversation_id=conversation_id,
    )


@app.delete("/api/agent/history")
def api_agent_history_clear(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict:
    require_local_history(request, settings)
    if settings.agent_history_enabled:
        clear_history(
            settings_for_season(settings, current_season()).agent_history_path
        )
    return {"status": "ok"}


@app.get("/ask", response_class=HTMLResponse)
def ask_page(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> HTMLResponse:
    context = {
        "request": request,
        "page_title": "Ask NBA Stats",
        "season": current_season(),
        "tracking_cap": TRACKING_CAP,
        "ask_claim_enforcement": settings.ask_claim_enforcement,
        "ask_local_espn_dev": settings.ask_local_espn_dev,
        "agent_enabled": settings.openai_agent_enabled,
        "agent_configured": bool(
            settings.openai_api_key
            or settings.anthropic_api_key
            or settings.openrouter_api_key
        ),
        "agent_model_options": AGENT_MODEL_OPTIONS,
        "agent_default_models": {
            "openai": settings.openai_agent_model,
            "claude": settings.anthropic_agent_model,
            "openrouter": settings.openrouter_agent_model,
        },
    }
    return templates.TemplateResponse(request, "ask.html", context)


@app.get("/visualize", response_class=RedirectResponse)
def visualize_page() -> RedirectResponse:
    return RedirectResponse(
        url="/performance"
        if current_season() == DEFAULT_SEASON
        else f"/performance?season={current_season()}"
    )


@app.get("/performance", response_class=HTMLResponse)
def performance_page(
    request: Request,
) -> HTMLResponse:
    context = {
        "request": request,
        "page_title": "Performance",
        "season": current_season(),
        "tracking_cap": TRACKING_CAP,
    }
    return templates.TemplateResponse(request, "performance.html", context)


@app.get("/what-changed", response_class=HTMLResponse)
def what_changed_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "what_changed.html",
        {
            "request": request,
            "page_title": "Trending",
            "season": current_season(),
        },
    )


@app.get("/api/what-changed")
def api_what_changed(
    response: Response,
    repo: Annotated[WarehouseRepository, Depends(get_repository)],
    period: ComparisonPeriod = "four_games",
    season_type: SeasonPhase = "Regular Season",
    as_of: date | None = None,
) -> dict[str, Any]:
    cutoff = as_of.isoformat() if as_of else None
    try:
        payload = _get_cached_payload(
            (_repo_cache_token(repo), "what_changed", period, season_type, cutoff),
            900,
            lambda: repo.get_what_changed(
                period=period, season_type=season_type, as_of=cutoff
            ),
        )
    except WhatChangedUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="Trending data is temporarily unavailable. Try again later.",
        ) from exc
    _set_public_cache_header(response, 60)
    return payload


@app.get("/api/similarity-map")
def api_similarity_map(
    repo: Annotated[WarehouseRepository, Depends(get_repository)],
) -> dict:
    return repo.get_similarity_map()


@app.get("/api/similarity-map/neighbors/{player_id}")
def api_similarity_map_neighbors(
    player_id: int,
    repo: Annotated[WarehouseRepository, Depends(get_repository)],
    limit: int = Query(default=8, ge=1, le=15),
) -> dict:
    return repo.get_similarity_neighbors(player_id, limit=limit)


@app.get("/similarity-methodology", response_class=HTMLResponse)
def similarity_methodology_page(request: Request) -> HTMLResponse:
    from app.repository import SIMILARITY_FEATURE_COLUMNS

    return templates.TemplateResponse(
        request,
        "similarity_methodology.html",
        {
            "request": request,
            "season": current_season(),
            "page_title": "Statistical Profile Similarity — Methodology",
            "similarity_features": SIMILARITY_FEATURE_COLUMNS,
        },
    )


@app.get("/similarity-map", response_class=HTMLResponse)
def similarity_map_page(
    request: Request,
) -> HTMLResponse:
    context = {
        "request": request,
        "page_title": "Statistical Profile Similarity",
        "season": current_season(),
    }
    return templates.TemplateResponse(request, "similarity_map.html", context)


@app.get("/compare", response_class=HTMLResponse)
def compare_page(
    request: Request,
    repo: Annotated[WarehouseRepository, Depends(get_repository)],
    player_a_id: int | None = None,
    player_b_id: int | None = None,
    window: CompareWindow = Query(default="last_5"),
    focus: CompareFocus = Query(default="balanced"),
) -> HTMLResponse:
    compare_error: str | None = None
    comparison: dict | None = None
    health = repo.get_health()
    player_a_detail = (
        repo.get_player_detail(player_a_id) if player_a_id is not None else None
    )
    if player_a_id is not None and player_a_detail is None:
        raise HTTPException(status_code=404, detail="Player not found")
    if player_a_id is not None and player_b_id is not None:
        if player_a_id == player_b_id:
            compare_error = "Compare players must be different."
        else:
            comparison = repo.get_compare(
                player_a_id,
                player_b_id,
                window=window,
                focus=focus,
            )
    instrument_compare_view(
        route="/compare",
        season=current_season(),
        health=health,
        comparison=comparison,
    )
    context = {
        "request": request,
        "page_title": "Player Compare",
        "season": current_season(),
        "health": health,
        "player_a_detail": player_a_detail,
        "player_a_id": player_a_id,
        "player_b_id": player_b_id,
        "comparison": comparison,
        "compare_error": compare_error,
        "window": window,
        "window_label": next(
            option["label"]
            for option in get_compare_window_options()
            if option["key"] == window
        ),
        "focus": focus,
        "focus_label": next(
            option["label"]
            for option in get_compare_focus_options()
            if option["key"] == focus
        ),
        "compare_window_options": get_compare_window_options(),
        "compare_focus_options": get_compare_focus_options(),
        "tracking_cap": TRACKING_CAP,
    }
    return templates.TemplateResponse(request, "compare.html", context)


@app.get("/players", response_class=HTMLResponse)
@app.get("/research", response_class=HTMLResponse)
def research_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "research.html",
        {"request": request, "page_title": "Players", "season": current_season()},
    )


@app.get("/api/research/players")
def research_players(
    request: Request, settings: Annotated[Settings, Depends(get_settings)]
) -> dict:
    from app.agent.semantic_serving import source_players
    from app.agent.semantics import SemanticError

    _check_research_rate_limit(request, settings)
    try:
        repo = None if settings.research_snapshot_path else get_repository(settings)
        evidence, used = available_research(
            settings,
            repo,
            current_season(),
            fallback="season" not in request.query_params,
            loader=load_research_evidence,
        )
        result = {"players": source_players(evidence), "season": used}
        if used != current_season():
            from app.agent.semantic_serving import fallback_notice

            result["season_fallback"] = {"requested": current_season(), "used": used}
            result["notice"] = fallback_notice(current_season(), used)
        return result
    except SemanticError as exc:
        if exc.code == "unsupported_coverage":
            return {
                "players": [],
                "season": current_season(),
                "status": exc.code,
                "notice": str(exc),
            }
        raise HTTPException(503, "Research source is unavailable") from exc
    except Exception as exc:
        raise HTTPException(503, "Research source is unavailable") from exc


@app.post("/api/research/breakdown")
def research_breakdown(
    payload: ResearchQuery,
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict:
    from app.agent.semantics import SemanticError

    if "season" in payload.model_fields_set and payload.season != current_season():
        raise HTTPException(400, "Request body and selected season disagree")
    _check_research_rate_limit(request, settings)
    try:
        repo = None if settings.research_snapshot_path else get_repository(settings)
        evidence, used = available_research(
            settings,
            repo,
            current_season(),
            loader=load_research_evidence,
            player_ids=payload.player_ids,
            phase=payload.phase,
            fallback=(
                "season" not in payload.model_fields_set
                and "season" not in request.query_params
                and not payload.start
                and not payload.end
            ),
        )
        result = breakdown(
            evidence,
            payload.model_copy(update={"season": used}),
            load_context(settings.research_context_path),
        )
        if used != current_season():
            from app.agent.semantic_serving import fallback_notice

            result["season_fallback"] = {"requested": current_season(), "used": used}
            result["notice"] = fallback_notice(current_season(), used)
        return result
    except SemanticError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(503, "Research evidence is unavailable or invalid") from exc


@app.get("/api/research/studies")
def research_studies(
    request: Request, settings: Annotated[Settings, Depends(get_settings)]
) -> dict:
    _check_research_rate_limit(request, settings)
    try:
        return {"studies": research_catalog(settings.research_studies_path)}
    except Exception as exc:
        raise HTTPException(
            503, "Reviewed study catalog is unavailable or invalid"
        ) from exc


def _check_research_rate_limit(request: Request, settings: Settings) -> None:
    decision = get_agent_rate_limiter(settings.agent_rate_limit_redis_url).check(
        key="research:" + _agent_rate_limit_key(request, settings),
        per_minute=60,
        per_day=0,
    )
    if not decision.allowed:
        raise HTTPException(
            429, "Research request limit exceeded. Try again in a minute."
        )


@app.post("/api/agent/claim-observations")
async def claim_observations(
    request: Request, settings: Annotated[Settings, Depends(get_settings)]
) -> dict:
    from app.agent.claim_rollout import ingest

    return await ingest(request, settings.ask_claim_enforcement)
