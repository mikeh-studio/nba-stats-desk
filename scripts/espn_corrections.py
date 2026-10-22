"""Independent ESPN layer contracts and a bounded, league-local correction policy."""

from __future__ import annotations

import copy
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from scripts import espn_only_store as only
from scripts import espn_sources as source
from scripts.gamebook_sources import season_bounds
from scripts.gamebook_store import canonical, require
from scripts.parse_gamebooks import sha

VERSION = "espn-layers/1"
LAYERS = ("result", "box", "pbp")
LEAGUE_TZ = ZoneInfo("America/New_York")


def timestamp(value):
    result = datetime.fromisoformat(value) if isinstance(value, str) else value
    require(
        isinstance(result, datetime) and result.tzinfo is not None, "Timezone required"
    )
    return result.astimezone(timezone.utc)


def revision(candidate):
    return sha(canonical(candidate).encode())


def normalize(payload, game, layer):
    require(layer in LAYERS, "Unknown layer")
    # Result and box capture must not inspect malformed/missing downstream fields.
    selected = {"header": payload["header"], "boxscore": {"players": []}, "plays": []}
    if layer != "result":
        selected["boxscore"] = payload["boxscore"]
    if layer == "pbp":
        selected["plays"] = payload["plays"]
    captured = only.source_input(selected)
    confirmed = source.event_game(
        captured["header"], game["season"], date.fromisoformat(game["date"])
    )
    require(
        confirmed == game and game["final"],
        "Summary scope/status/score differs from discovery",
    )
    result = {
        "schema_version": VERSION,
        "layer": layer,
        "game": copy.deepcopy(game),
        "input": captured,
        "result_revision": None,
        "box_revision": None,
    }
    if layer != "result":
        result["result_revision"] = revision(normalize(payload, game, "result"))
        result["players"] = source.normalize_box(captured, game, [])
    if layer == "pbp":
        reconciled = only.normalize(captured, game)
        result.update(
            box_revision=revision(normalize(payload, game, "box")),
            assists=reconciled["assists"],
            play_count=reconciled["play_count"],
            legacy_revision=only.validate(reconciled),
        )
    return result


def validate(candidate):
    require(candidate["schema_version"] == VERSION, "Wrong layered schema")
    require(
        candidate
        == normalize(candidate["input"], candidate["game"], candidate["layer"]),
        "Layer differs from recomputed source",
    )
    return revision(candidate)


def changes(previous, candidate):
    """Small, deterministic field diff; full revisions remain the source of truth."""

    def flatten(value, prefix=""):
        if isinstance(value, dict):
            return {
                k: v
                for key, child in value.items()
                for k, v in flatten(child, prefix + "/" + str(key)).items()
            }
        if isinstance(value, list):
            return {
                k: v
                for index, child in enumerate(value)
                for k, v in flatten(child, prefix + "/" + str(index)).items()
            }
        return {prefix: value}

    before, after = flatten(previous or {}), flatten(candidate)
    return [
        {"path": k, "before": before.get(k), "after": after.get(k)}
        for k in sorted(before.keys() | after.keys())
        if before.get(k) != after.get(k)
    ]


def next_check(game_date, last_checked, unresolved):
    """Noon Eastern on D+1..7,14,30; unresolved games also D+21,28.

    Return the earliest unfulfilled milestone, including an overdue one. A new
    fetch satisfies elapsed milestones; replay uses the original capture time.
    These are our intervals, not a provider finality guarantee.
    """
    day = date.fromisoformat(game_date) if isinstance(game_date, str) else game_date
    if last_checked is None:
        return datetime.combine(day, time(), LEAGUE_TZ).astimezone(timezone.utc)
    checked = timestamp(last_checked)
    days = [*range(1, 8), 14, *([21, 28] if unresolved else []), 30]
    for offset in days:
        due = datetime.combine(
            day + timedelta(days=offset), time(12), LEAGUE_TZ
        ).astimezone(timezone.utc)
        if due > checked:
            return due
    return None


def plan(season, now, coverage):
    """Read-only date plan: daily discovery plus due known games, bounded to 31 dates."""
    now = timestamp(now)
    today = now.astimezone(LEAGUE_TZ).date()
    low, high = season_bounds(season)
    require(
        low <= today <= high + timedelta(days=30),
        "Correction planning outside requested season/window",
    )
    dates = {
        today - timedelta(days=n)
        for n in range(8)
        if low <= today - timedelta(days=n) <= high
    }
    due_games, exceptions = [], []
    for row in coverage:
        day = date.fromisoformat(str(row["game_date"]))
        require(
            row["season"] == season and low <= day <= high, "Coverage scope differs"
        )
        unresolved = any(
            row.get(k) != "accepted"
            for k in ("result_status", "box_status", "pbp_status")
        )
        due = next_check(day, row.get("last_checked_at"), unresolved)
        age = (today - day).days
        if age > 30:
            if unresolved or due is not None:
                exceptions.append(
                    {
                        "source_game_id": row["source_game_id"],
                        "reason": "unresolved_or_missed_check_after_window",
                    }
                )
        elif due is not None and due <= now:
            dates.add(day)
            due_games.append(row["source_game_id"])
    require(len(dates) <= 31 and len(coverage) <= 1500, "Correction plan exceeds bound")
    return {
        "as_of": now.isoformat(),
        "dates": [str(d) for d in sorted(dates)],
        "due_games": sorted(due_games),
        "exceptions": exceptions,
    }
