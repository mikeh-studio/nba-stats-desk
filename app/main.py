from __future__ import annotations

from collections import deque
from datetime import UTC, date, datetime
from pathlib import Path
from threading import Lock
from time import monotonic
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from app.agent.service import AgentDisabledError, AgentExecutionError, StatsAgent
from app.config import SUPPORTED_SEASON, Settings, get_settings
from app.repository import (
    BigQueryWarehouseRepository,
    CompareFocus,
    CompareWindow,
    WarehouseRepository,
    get_compare_focus_options,
    get_compare_window_options,
)
from app.telemetry import (
    instrument_compare_view,
    instrument_dashboard_view,
    instrument_player_view,
)

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
TRACKING_CAP = 8


class AgentAskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)
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

app = FastAPI(title="NBA 2025-26 Public API", version="1.0.0")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

_AGENT_RATE_LIMIT_WINDOW_SECONDS = 60.0
_agent_rate_limit_lock = Lock()
_agent_rate_limit_hits: dict[str, deque[float]] = {}


def get_repository(
    settings: Annotated[Settings, Depends(get_settings)],
) -> WarehouseRepository:
    return BigQueryWarehouseRepository(settings)


def get_agent_client() -> Any | None:
    return None


def _agent_rate_limit_key(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        return forwarded_for.split(",", 1)[0].strip() or "unknown"
    if request.client is not None:
        return request.client.host
    return "unknown"


def _check_agent_rate_limit(request: Request, settings: Settings) -> None:
    limit = settings.agent_rate_limit_per_minute
    if limit <= 0 or not settings.openai_agent_enabled:
        return

    key = _agent_rate_limit_key(request)
    now = monotonic()
    cutoff = now - _AGENT_RATE_LIMIT_WINDOW_SECONDS
    with _agent_rate_limit_lock:
        hits = _agent_rate_limit_hits.setdefault(key, deque())
        while hits and hits[0] <= cutoff:
            hits.popleft()
        if len(hits) >= limit:
            raise HTTPException(
                status_code=429,
                detail="Ask NBA Stats rate limit exceeded. Try again in a minute.",
            )
        hits.append(now)


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
def api_health(repo: Annotated[WarehouseRepository, Depends(get_repository)]) -> dict:
    return repo.get_health()


@app.get("/", response_class=HTMLResponse)
def home(
    request: Request,
    repo: Annotated[WarehouseRepository, Depends(get_repository)],
    as_of_date: str | None = Query(default=None),
) -> HTMLResponse:
    dashboard = repo.get_dashboard(as_of_date=as_of_date)
    health = repo.get_health()
    instrument_dashboard_view(
        route="/",
        season=SUPPORTED_SEASON,
        health=health,
        dashboard=dashboard,
    )
    context = {
        "request": request,
        "page_title": "NBA 2025-26 Stats Dashboard",
        "season": SUPPORTED_SEASON,
        "dashboard": dashboard,
        "health": health,
        "tracking_cap": TRACKING_CAP,
    }
    return templates.TemplateResponse(request, "index.html", context)


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


@app.get("/api/performance/dates")
def api_performance_dates(
    repo: Annotated[WarehouseRepository, Depends(get_repository)],
) -> dict:
    return {"season": SUPPORTED_SEASON, "items": repo.get_recent_performance_dates()}


@app.get("/api/performance/games")
def api_performance_games(
    repo: Annotated[WarehouseRepository, Depends(get_repository)],
    game_date: date | None = Query(None),
) -> dict:
    return {
        "season": SUPPORTED_SEASON,
        "game_date": game_date.isoformat() if game_date is not None else None,
        "items": repo.get_recent_performance_games(
            game_date=game_date.isoformat() if game_date is not None else None
        ),
    }


@app.get("/api/performance/players")
def api_performance_players(
    repo: Annotated[WarehouseRepository, Depends(get_repository)],
    game_date: date = Query(...),
    game_id: str | None = Query(default=None, min_length=1, max_length=32),
    limit: int = Query(240, ge=1, le=500),
) -> dict:
    return {
        "season": SUPPORTED_SEASON,
        "game_date": game_date.isoformat(),
        "game_id": game_id,
        "items": repo.get_recent_performance_players(
            game_date=game_date.isoformat(),
            game_id=game_id,
            limit=limit,
        ),
    }


@app.get("/api/performance/players/{player_id}")
def api_performance_player_detail(
    player_id: int,
    repo: Annotated[WarehouseRepository, Depends(get_repository)],
    game_id: str = Query(..., min_length=1, max_length=32),
) -> dict:
    item = repo.get_recent_performance_player(player_id, game_id=game_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Performance row not found")
    return {"season": SUPPORTED_SEASON, "item": item}


@app.post("/api/agent/ask")
def api_agent_ask(
    request: Request,
    payload: AgentAskRequest,
    repo: Annotated[WarehouseRepository, Depends(get_repository)],
    settings: Annotated[Settings, Depends(get_settings)],
    agent_client: Annotated[Any | None, Depends(get_agent_client)],
) -> dict:
    question = payload.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="Question must not be blank")
    _check_agent_rate_limit(request, settings)
    agent = StatsAgent(settings, repo, client=agent_client)
    try:
        answer = agent.answer(question, conversation_id=payload.conversation_id)
    except AgentDisabledError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except AgentExecutionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"season": SUPPORTED_SEASON, **answer}


@app.get("/ask", response_class=HTMLResponse)
def ask_page(
    request: Request,
    repo: Annotated[WarehouseRepository, Depends(get_repository)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> HTMLResponse:
    health = repo.get_health()
    context = {
        "request": request,
        "page_title": "Ask NBA Stats",
        "season": SUPPORTED_SEASON,
        "health": health,
        "tracking_cap": TRACKING_CAP,
        "agent_enabled": settings.openai_agent_enabled,
        "agent_configured": bool(settings.openai_api_key),
    }
    return templates.TemplateResponse(request, "ask.html", context)


@app.get("/visualize", response_class=HTMLResponse)
def visualize_page(
    request: Request,
    repo: Annotated[WarehouseRepository, Depends(get_repository)],
    player_id: int | None = None,
) -> HTMLResponse:
    health = repo.get_health()
    player_detail = None
    if player_id is not None:
        player_detail = repo.get_player_detail(player_id)
    context = {
        "request": request,
        "page_title": "Player Stats Explorer",
        "season": SUPPORTED_SEASON,
        "health": health,
        "player_detail": player_detail,
        "player_id": player_id,
    }
    return templates.TemplateResponse(request, "visualize.html", context)


@app.get("/performance", response_class=HTMLResponse)
def performance_page(
    request: Request,
    repo: Annotated[WarehouseRepository, Depends(get_repository)],
) -> HTMLResponse:
    health = repo.get_health()
    context = {
        "request": request,
        "page_title": "Recent Game Performance",
        "season": SUPPORTED_SEASON,
        "health": health,
        "tracking_cap": TRACKING_CAP,
    }
    return templates.TemplateResponse(request, "performance.html", context)


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
        "page_title": "Compare Players",
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
