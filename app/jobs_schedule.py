"""Read-only projections of the existing weekly worker's schedule and retries."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


def slot(now, timezone_name, weekday=6, hour=8):
    if now.tzinfo is None or weekday not in range(7) or hour not in range(24):
        raise ValueError("Invalid weekly schedule")
    local = now.astimezone(ZoneInfo(timezone_name))
    candidate = (local - timedelta(days=(local.weekday() - weekday) % 7)).replace(
        hour=hour, minute=0, second=0, microsecond=0
    )
    if candidate > local:
        candidate -= timedelta(days=7)
    return candidate.astimezone(timezone.utc)


def due(state, current_slot, now):
    if state.get("last_success_slot") == current_slot.isoformat():
        return False
    if state.get("attempt_slot") != current_slot.isoformat():
        return True
    if state.get("attempt_count", 0) >= 3:
        return False
    return now - datetime.fromisoformat(state["last_attempt_at"]) >= timedelta(days=1)
