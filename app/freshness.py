"""Asset publication health and the explicitly configured NBA offseason."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.config import Settings
from app.repository._helpers import _parse_iso_datetime, build_freshness_payload
from app.seasons import DEFAULT_SEASON

ASSET_LABELS = {"stats": "Stats", "injuries": "Injuries", "similarity": "Similarity"}


def build_publication_health(
    latest_run: dict[str, Any] | None,
    asset_rows: list[dict[str, Any]] | None,
    coverage: dict[str, Any] | None,
    *,
    settings: Settings,
    now: datetime,
) -> dict[str, Any]:
    payload = build_freshness_payload(
        latest_run,
        now=now,
        freshness_threshold_hours=settings.freshness_threshold_hours,
    )
    local_now = now.astimezone(ZoneInfo("America/New_York"))
    offseason = (
        settings.freshness_offseason_start
        <= local_now.date()
        < settings.next_regular_season_start
    )
    resume = datetime.combine(
        settings.next_regular_season_start, time(), local_now.tzinfo
    )
    opening_grace = (
        resume
        <= local_now
        < resume + timedelta(hours=settings.freshness_threshold_hours)
    )
    rows = {row["asset"]: row for row in asset_rows or []}
    assets = {}
    for name, label in ASSET_LABELS.items():
        row = rows.get(name, {})
        refreshed_at = row.get("last_successful_finished_at_utc")
        source_date = row.get("latest_source_date")
        # Legacy logs lack asset metadata. Only core stats can be inferred.
        if (
            name == "stats"
            and not refreshed_at
            and latest_run
            and (coverage or {}).get("latest_game_date")
        ):
            refreshed_at = latest_run.get("finished_at_utc")
            source_date = (coverage or {}).get("latest_game_date")
        asset = build_freshness_payload(
            {"finished_at_utc": refreshed_at} if refreshed_at else None,
            now=now,
            freshness_threshold_hours=settings.freshness_threshold_hours,
        )
        asset.update(
            {
                "label": label,
                "latest_source_date": source_date,
                "last_attempt_status": row.get("last_attempt_status", "unknown"),
                "last_attempt_at_utc": row.get("last_attempt_at_utc"),
                "serving_previous_version": bool(refreshed_at)
                and row.get("last_attempt_status") == "failed_non_blocking",
            }
        )
        source_at = _parse_iso_datetime(str(source_date)) if source_date else None
        # Calendar dates have day precision; compare dates to avoid declaring
        # yesterday's completed game stale just because it has no time component.
        if source_at and not offseason and not opening_grace:
            source_days = (local_now.date() - source_at.date()).days
            if source_days > max(1, settings.freshness_threshold_hours / 24):
                asset.update(status="stale", is_fresh=False)
        if row.get("last_attempt_status") == "failed_non_blocking":
            asset.update(status="refresh_failed", is_fresh=False)
        elif refreshed_at and offseason:
            asset.update(status="offseason", is_fresh=None)
        elif opening_grace and (
            source_at is None or source_at.date() < settings.next_regular_season_start
        ):
            asset.update(status="awaiting_refresh", is_fresh=None)
        assets[name] = asset

    payload.update(
        {
            "season": settings.season,
            "season_phase": "offseason" if offseason else "in_season",
            "updates_expected": not offseason,
            "next_regular_season_start": settings.next_regular_season_start.isoformat(),
            "assets": assets,
            "asset_metadata_available": asset_rows is not None,
            "latest_source_date": (coverage or {}).get("latest_game_date"),
        }
    )
    if coverage is not None:
        payload["season_coverage"] = coverage
    if any(asset["status"] == "refresh_failed" for asset in assets.values()):
        payload.update(status="partially_updated", is_fresh=False)
    elif offseason and latest_run:
        payload.update(status="offseason", is_fresh=None)
    elif opening_grace and any(
        asset["status"] == "awaiting_refresh" for asset in assets.values()
    ):
        payload.update(status="awaiting_refresh", is_fresh=None)
    elif any(asset["status"] == "stale" for asset in assets.values()):
        payload.update(status="stale", is_fresh=False)
    elif any(
        asset["status"] in {"missing", "unavailable"} for asset in assets.values()
    ):
        payload.update(
            status="partially_updated" if latest_run else "missing", is_fresh=False
        )
    if settings.season != DEFAULT_SEASON:
        payload.update(
            status="historical" if coverage else "missing",
            is_fresh=None,
            season_phase="historical",
            updates_expected=False,
            next_regular_season_start=None,
        )
        for name, asset in assets.items():
            asset.update(
                status="historical"
                if (name == "stats" and coverage)
                or asset.get("last_successful_finished_at_utc")
                else "unavailable",
                is_fresh=None,
            )
    return payload
