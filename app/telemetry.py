from __future__ import annotations

import json
import logging
from typing import Any

from app.repository import (
    STATE_FRESH,
    STATE_INSUFFICIENT_SAMPLE,
    STATE_UNAVAILABLE,
)

LOGGER_NAME = "app.telemetry"
logger = logging.getLogger(LOGGER_NAME)


def _emit_panel_event(
    *,
    route: str,
    surface: str,
    panel: str,
    state: str,
    reason: str,
    season: str,
    player_id: int | None = None,
    window: str | None = None,
    focus: str | None = None,
    slot: str | None = None,
    as_of_date: str | None = None,
) -> None:
    payload: dict[str, Any] = {
        "event_name": "panel_degraded",
        "route": route,
        "surface": surface,
        "panel": panel,
        "state": state,
        "reason": reason,
        "season": season,
    }
    if player_id is not None:
        payload["player_id"] = player_id
    if window is not None:
        payload["window"] = window
    if focus is not None:
        payload["focus"] = focus
    if slot is not None:
        payload["slot"] = slot
    if as_of_date is not None:
        payload["as_of_date"] = as_of_date
    logger.info(json.dumps(payload, sort_keys=True))


def _detail_panel_reason(panel: str, state: str, player_detail: dict[str, Any]) -> str:
    if state == STATE_INSUFFICIENT_SAMPLE:
        if panel in {"archetype", "similarity"}:
            return "insufficient_similarity_sample"
        return "insufficient_window_sample"
    if player_detail.get("availability_state") == STATE_UNAVAILABLE:
        return "player_not_ranked"
    if panel == "opportunity":
        return "missing_schedule_context"
    if panel == "category_profile":
        return "missing_category_profile"
    if panel == "archetype":
        return "missing_similarity_profile"
    if panel == "similarity":
        return "missing_similarity_matches"
    return "window_unavailable"


def instrument_player_view(
    *, route: str, season: str, health: dict[str, Any], player_detail: dict[str, Any]
) -> None:
    player_id = player_detail["player"]["player_id"]
    for panel, state in player_detail.get("panel_states", {}).items():
        if state == STATE_FRESH:
            continue
        _emit_panel_event(
            route=route,
            surface="player_detail",
            panel=str(panel),
            state=str(state),
            reason=_detail_panel_reason(str(panel), str(state), player_detail),
            season=season,
            player_id=player_id,
        )


def _compare_side_reason(side: dict[str, Any]) -> str:
    if side.get("state") == STATE_INSUFFICIENT_SAMPLE:
        return "insufficient_window_sample"
    if (
        side.get("state_reason") == "Player not found"
        or side.get("player_name") is None
    ):
        return "player_not_found"
    if side.get("availability_state") == STATE_UNAVAILABLE:
        return "player_not_ranked"
    return "window_unavailable"


def instrument_compare_view(
    *,
    route: str,
    season: str,
    health: dict[str, Any],
    comparison: dict[str, Any] | None,
) -> None:
    if comparison is None:
        return
    for slot in ("player_a", "player_b"):
        side = comparison["comparison"][slot]
        if side.get("state") == STATE_FRESH:
            continue
        _emit_panel_event(
            route=route,
            surface="compare",
            panel="compare_side",
            state=str(side.get("state")),
            reason=_compare_side_reason(side),
            season=season,
            player_id=side.get("player_id"),
            window=str(comparison.get("window")),
            focus=str(comparison.get("focus")),
            slot=slot,
        )
