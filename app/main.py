from __future__ import annotations

import json
import logging
import time
from collections.abc import MutableMapping
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import UTC, date, datetime
from functools import lru_cache
from pathlib import Path
from queue import Queue
from threading import Event, Lock, Thread
from typing import Annotated, Any
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from app.agent.conversation import get_conversation_store
from app.agent.observability import LOGGER_NAME, AgentTrace
from app.agent.service import AgentDisabledError, AgentExecutionError, StatsAgent
from app.config import SUPPORTED_SEASON, Settings, get_settings
from app.rate_limit import get_agent_rate_limiter
from app.repository import (
    BigQueryWarehouseRepository,
    CompareFocus,
    CompareWindow,
    WarehouseRepository,
    get_compare_focus_options,
    get_compare_window_options,
)
from app.telemetry import instrument_compare_view, instrument_player_view

BASE_DIR = Path(__file__).resolve().parent
STATIC_VERSION = "20260609-performance-flow-v5"
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.globals["static_version"] = STATIC_VERSION
TRACKING_CAP = 8
HEALTH_CACHE_TTL_SECONDS = 60
PERFORMANCE_CACHE_TTL_SECONDS = 900
PERFORMANCE_STALE_TTL_SECONDS = 3600
PERFORMANCE_INITIAL_DEFAULT_LIMIT = 240
_health_cache: dict[int, tuple[float, dict[str, Any]]] = {}
_payload_cache: dict[tuple[Any, ...], tuple[float, Any]] = {}
_payload_cache_lock = Lock()
_payload_refreshing: set[tuple[Any, ...]] = set()
_payload_building: dict[tuple[Any, ...], Event] = {}
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


class AgentAskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    conversation_id: str | None = Field(default=None, max_length=80)


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


app = FastAPI(title="NBA 2025-26 Public API", version="1.0.0", lifespan=_lifespan)
app.mount(
    "/static",
    CacheControlledStaticFiles(directory=str(BASE_DIR / "static")),
    name="static",
)
agent_logger = logging.getLogger(LOGGER_NAME)


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
    return _cached_repository(settings)


def get_agent_client() -> Any | None:
    return None


def _get_cached_health(repo: WarehouseRepository) -> dict[str, Any]:
    key = id(repo)
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
    return (id(repo), "performance_initial", game_date, game_id, limit)


def _clone_payload(payload: Any) -> Any:
    return deepcopy(payload)


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
                return _clone_payload(payload)
            if stale_ttl_seconds and now - cached_at < stale_ttl_seconds:
                if key not in _payload_refreshing:
                    _payload_refreshing.add(key)
                    Thread(
                        target=_refresh_cached_payload,
                        args=(key, builder),
                        daemon=True,
                    ).start()
                return _clone_payload(payload)

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
                return _clone_payload(cached[1])
        return _get_cached_payload(
            key,
            ttl_seconds,
            builder,
            stale_ttl_seconds=stale_ttl_seconds,
        )

    try:
        payload = builder()
        with _payload_cache_lock:
            _payload_cache[key] = (time.monotonic(), _clone_payload(payload))
        return payload
    finally:
        with _payload_cache_lock:
            building.set()
            _payload_building.pop(key, None)


def _refresh_cached_payload(key: tuple[Any, ...], builder: Any) -> None:
    try:
        payload = builder()
        with _payload_cache_lock:
            _payload_cache[key] = (time.monotonic(), _clone_payload(payload))
    except Exception:
        return
    finally:
        with _payload_cache_lock:
            _payload_refreshing.discard(key)


def _set_public_cache_header(response: Response, ttl_seconds: int) -> None:
    response.headers["Cache-Control"] = f"public, max-age={ttl_seconds}"


def _agent_rate_limit_key(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        return forwarded_for.split(",", 1)[0].strip() or "unknown"
    if request.client is not None:
        return request.client.host
    return "unknown"


def _check_agent_rate_limit(request: Request, settings: Settings) -> None:
    if not settings.openai_agent_enabled:
        return

    key = _agent_rate_limit_key(request)
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


def _request_id(request: Request) -> str:
    return request.headers.get("x-request-id") or uuid4().hex


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


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


@app.get("/api/leaderboard")
def api_leaderboard(
    repo: Annotated[WarehouseRepository, Depends(get_repository)],
) -> dict:
    return {"season": SUPPORTED_SEASON, "items": repo.get_leaderboard()}


@app.get("/api/trends")
def api_trends(repo: Annotated[WarehouseRepository, Depends(get_repository)]) -> dict:
    return {"season": SUPPORTED_SEASON, "items": repo.get_trends()}


@app.get("/api/analysis/latest")
def api_analysis_latest(
    repo: Annotated[WarehouseRepository, Depends(get_repository)],
) -> dict:
    return {"season": SUPPORTED_SEASON, "item": repo.get_latest_analysis()}


@app.get("/api/recommendations")
def api_recommendations(
    repo: Annotated[WarehouseRepository, Depends(get_repository)],
    limit: int = Query(10, ge=1, le=50),
    insight_type: str | None = Query(default=None),
) -> dict:
    return {
        "season": SUPPORTED_SEASON,
        "items": repo.get_recommendations(limit=limit, insight_type=insight_type),
    }


@app.get("/api/rankings")
def api_rankings(
    repo: Annotated[WarehouseRepository, Depends(get_repository)],
    limit: int = Query(25, ge=1, le=100),
) -> dict:
    return {"season": SUPPORTED_SEASON, "items": repo.get_rankings(limit=limit)}


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
        "season": SUPPORTED_SEASON,
        "query": query,
        "items": repo.search_players(query, limit=settings.max_search_results),
    }


@app.get("/api/players/{player_id}")
def api_player_detail(
    player_id: int,
    repo: Annotated[WarehouseRepository, Depends(get_repository)],
) -> dict:
    detail = repo.get_player_detail(player_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Player not found")
    return {"season": SUPPORTED_SEASON, "item": detail}


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
    return RedirectResponse(url="/ask")


@app.get("/players/{player_id}", response_class=HTMLResponse)
def player_page(
    player_id: int,
    request: Request,
    repo: Annotated[WarehouseRepository, Depends(get_repository)],
) -> HTMLResponse:
    player_detail = repo.get_player_detail(player_id)
    if player_detail is None:
        raise HTTPException(status_code=404, detail="Player not found")
    health = repo.get_health()
    instrument_player_view(
        route="/players/{player_id}",
        season=SUPPORTED_SEASON,
        health=health,
        player_detail=player_detail,
    )
    context = {
        "request": request,
        "page_title": f"{player_detail['player']['player_name']} Stats Outlook",
        "season": SUPPORTED_SEASON,
        "player_detail": player_detail,
        "health": health,
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
    return {"season": SUPPORTED_SEASON, "item": result}


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
        (id(repo), "performance_dates"),
        PERFORMANCE_CACHE_TTL_SECONDS,
        lambda: {
            "season": SUPPORTED_SEASON,
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
        (id(repo), "performance_games", game_date_value),
        PERFORMANCE_CACHE_TTL_SECONDS,
        lambda: {
            "season": SUPPORTED_SEASON,
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
        (id(repo), "performance_players", game_date_value, game_id, limit),
        PERFORMANCE_CACHE_TTL_SECONDS,
        lambda: {
            "season": SUPPORTED_SEASON,
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
        (id(repo), "performance_player_detail", player_id, game_id),
        PERFORMANCE_CACHE_TTL_SECONDS,
        lambda: repo.get_recent_performance_player(player_id, game_id=game_id),
        stale_ttl_seconds=PERFORMANCE_STALE_TTL_SECONDS,
    )
    if item is None:
        raise HTTPException(status_code=404, detail="Performance row not found")
    return {"season": SUPPORTED_SEASON, "item": item}


@app.post("/api/agent/ask")
def api_agent_ask(
    request: Request,
    response: Response,
    payload: AgentAskRequest,
    repo: Annotated[WarehouseRepository, Depends(get_repository)],
    settings: Annotated[Settings, Depends(get_settings)],
    agent_client: Annotated[Any | None, Depends(get_agent_client)],
) -> dict:
    request_id = _request_id(request)
    conversation_id = _conversation_id(payload.conversation_id)
    trace = AgentTrace(
        request_id=request_id,
        question=payload.question,
        model=settings.openai_agent_model,
        conversation_id=conversation_id,
    )
    try:
        question = _validate_agent_question(payload.question, settings)
        _check_agent_rate_limit(request, settings)
        agent = StatsAgent(
            settings,
            repo,
            client=agent_client,
            conversation_store=get_conversation_store(),
        )
        answer = agent.answer(
            question,
            conversation_id=conversation_id,
            trace=trace,
        )
    except AgentDisabledError as exc:
        trace.outcome = "error"
        trace.error_type = type(exc).__name__
        agent_logger.warning("agent disabled: %s", exc)
        raise HTTPException(
            status_code=503,
            detail="Ask NBA Stats is unavailable.",
            headers={"X-Request-ID": request_id},
        ) from exc
    except AgentExecutionError as exc:
        trace.outcome = "error"
        trace.error_type = type(exc).__name__
        agent_logger.exception("agent execution failed", exc_info=exc)
        raise HTTPException(
            status_code=502,
            detail="Ask NBA Stats failed while generating an answer. Try again shortly.",
            headers={"X-Request-ID": request_id},
        ) from exc
    except HTTPException as exc:
        trace.outcome = "error"
        trace.error_type = f"HTTP_{exc.status_code}"
        exc.headers = {**(exc.headers or {}), "X-Request-ID": request_id}
        raise
    finally:
        trace.emit()
    response.headers["X-Request-ID"] = request_id
    return {"season": SUPPORTED_SEASON, "request_id": request_id, **answer}


@app.post("/api/agent/ask/stream")
def api_agent_ask_stream(
    request: Request,
    payload: AgentAskRequest,
    repo: Annotated[WarehouseRepository, Depends(get_repository)],
    settings: Annotated[Settings, Depends(get_settings)],
    agent_client: Annotated[Any | None, Depends(get_agent_client)],
) -> StreamingResponse:
    request_id = _request_id(request)
    conversation_id = _conversation_id(payload.conversation_id)
    trace = AgentTrace(
        request_id=request_id,
        question=payload.question,
        model=settings.openai_agent_model,
        conversation_id=conversation_id,
    )
    try:
        question = _validate_agent_question(payload.question, settings)
        _check_agent_rate_limit(request, settings)
    except HTTPException as exc:
        trace.outcome = "error"
        trace.error_type = f"HTTP_{exc.status_code}"
        exc.headers = {**(exc.headers or {}), "X-Request-ID": request_id}
        trace.emit()
        raise

    def event_stream():
        queue: Queue[dict[str, Any] | None] = Queue()

        def progress(event: dict[str, Any]) -> None:
            queue.put(event)

        def worker() -> None:
            try:
                agent = StatsAgent(
                    settings,
                    repo,
                    client=agent_client,
                    conversation_store=get_conversation_store(),
                )
                answer = agent.answer(
                    question,
                    conversation_id=conversation_id,
                    trace=trace,
                    progress_callback=progress,
                )
                answer["request_id"] = request_id
                answer["season"] = SUPPORTED_SEASON
                for token in str(answer.get("answer") or "").split(" "):
                    if token:
                        queue.put({"type": "answer_delta", "delta": f"{token} "})
                queue.put({"type": "final", "payload": answer})
            except AgentDisabledError as exc:
                trace.outcome = "error"
                trace.error_type = type(exc).__name__
                agent_logger.warning("agent stream disabled: %s", exc)
                queue.put(
                    {
                        "type": "error",
                        "detail": "Ask NBA Stats is unavailable.",
                        "request_id": request_id,
                    }
                )
            except AgentExecutionError as exc:
                trace.outcome = "error"
                trace.error_type = type(exc).__name__
                agent_logger.exception("agent stream execution failed", exc_info=exc)
                queue.put(
                    {
                        "type": "error",
                        "detail": (
                            "Ask NBA Stats failed while generating an answer. "
                            "Try again shortly."
                        ),
                        "request_id": request_id,
                    }
                )
            finally:
                trace.emit()
                queue.put(None)

        Thread(target=worker, daemon=True).start()
        yield _sse(
            "meta",
            {"request_id": request_id, "conversation_id": conversation_id},
        )
        while True:
            event = queue.get()
            if event is None:
                break
            event_name = str(event.get("type") or "progress")
            yield _sse(event_name, event)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"X-Request-ID": request_id},
    )


@app.get("/ask", response_class=HTMLResponse)
def ask_page(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> HTMLResponse:
    context = {
        "request": request,
        "page_title": "Ask NBA Stats",
        "season": SUPPORTED_SEASON,
        "tracking_cap": TRACKING_CAP,
        "agent_enabled": settings.openai_agent_enabled,
        "agent_configured": bool(settings.openai_api_key),
    }
    return templates.TemplateResponse(request, "ask.html", context)


@app.get("/visualize", response_class=RedirectResponse)
def visualize_page() -> RedirectResponse:
    return RedirectResponse(url="/performance")


@app.get("/performance", response_class=HTMLResponse)
def performance_page(
    request: Request,
) -> HTMLResponse:
    context = {
        "request": request,
        "page_title": "Player Trends",
        "season": SUPPORTED_SEASON,
        "tracking_cap": TRACKING_CAP,
    }
    return templates.TemplateResponse(request, "performance.html", context)


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


@app.get("/similarity-map", response_class=HTMLResponse)
def similarity_map_page(
    request: Request,
) -> HTMLResponse:
    context = {
        "request": request,
        "page_title": "Similar Players",
        "season": SUPPORTED_SEASON,
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
        season=SUPPORTED_SEASON,
        health=health,
        comparison=comparison,
    )
    context = {
        "request": request,
        "page_title": "Player Compare",
        "season": SUPPORTED_SEASON,
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
