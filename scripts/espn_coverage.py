"""Small ESPN-reported coverage checks for the weekly refresh report.

This ledger measures the provider's listed finals and retained appearances. It
does not certify NBA completeness or authorize a serving source publication.
"""

from __future__ import annotations

import math
from collections import Counter
from datetime import date, timedelta

from app.agent.semantics import load_contract

VERSION = "espn_weekly_coverage/1"
SUCCESS = {"published", "noop", "validated_noop"}


def number(value, minutes=False):
    try:
        if minutes and ":" in str(value):
            whole, seconds = str(value).split(":")
            if not 0 <= float(seconds) < 60 or float(whole) < 0:
                return None
            value = float(whole) + float(seconds) / 60
        value = float(value)
        return value if math.isfinite(value) and (value >= 0 or not minutes) else None
    except (TypeError, ValueError):
        return None


def check_players(candidate, mappings):
    """Compare raw played IDs with normalized rows, then mapped destinations."""
    expected = [
        str(a["athlete"]["id"])
        for team in candidate["input"]["boxscore"]["players"]
        for group in team["statistics"]
        for a in group["athletes"]
        if not a["didNotPlay"]
    ]
    played = [p for p in candidate["players"] if p["played"]]
    retained = [str(p["source_player_id"]) for p in played]
    mapped = {str(m["source_player_id"]): str(m["nba_player_id"]) for m in mappings}
    destinations = [mapped[p] for p in retained if p in mapped]
    fields = {"tov": "TO", "plus_minus": "PLUS_MINUS"}
    missing = Counter()
    components = {
        key: metric.components for key, metric in load_contract().metrics.items()
    }
    for player in played:
        for key, required in components.items():
            if any(
                number(player["stats"].get(fields.get(c, c.upper())), c == "min")
                is None
                for c in required
            ):
                missing[key] += 1
    return {
        "played_rows": len(expected),
        "retained_rows": len(retained),
        "dropped_rows": sum((Counter(expected) - Counter(retained)).values()),
        "unexpected_rows": sum((Counter(retained) - Counter(expected)).values()),
        "unresolved_rows": sum(p not in mapped for p in retained),
        "duplicate_rows": len(retained)
        - len(set(retained))
        + len(destinations)
        - len(set(destinations)),
        "missing_inputs": dict(missing),
        "identities": {p: mapped[p] for p in retained if p in mapped},
    }


def audit(report, first_date, previous=None):
    """Merge a bounded replay into prior date checks; never fill unknown dates."""
    first, last = (
        date.fromisoformat(first_date),
        date.fromisoformat(report["last_date"]),
    )
    batch_first = date.fromisoformat(report["first_date"])
    if not first <= batch_first <= last or (last - first).days > 365:
        raise ValueError("Invalid coverage dates")
    previous = previous or {}
    reusable = (
        previous.get("version") == VERSION
        and previous.get("season") == report["season"]
        and previous.get("first_date") == first_date
    )
    days = dict(previous.get("days", {})) if reusable else {}
    identities = dict(previous.get("identities", {})) if reusable else {}
    conflicts = set(previous.get("identity_conflicts", [])) if reusable else set()
    discovery = {r["date"]: r for r in report["discovery"]}
    games = {r["source_game_id"]: r for r in report["games"]}
    listed = {gid for row in report["discovery"] for gid in row.get("game_ids", [])}
    unexpected_games = sorted(set(games) - listed)
    metric_keys = list(load_contract().metrics)
    for offset in range((last - batch_first).days + 1):
        day = str(batch_first + timedelta(days=offset))
        observed = discovery.get(day, {})
        expected = observed.get("game_ids")
        discovery_ok = (
            observed.get("status") == "captured"
            and isinstance(expected, list)
            and len(expected) == len(set(expected))
            and observed.get("finals") == len(expected)
        )
        expected = expected if isinstance(expected, list) else []
        accepted, players, missing = [], Counter(), Counter()
        for gid in expected:
            game = games.get(gid, {})
            layers = {r["layer"]: r for r in game.get("layers", [])}
            check = game.get("player_check")
            if not check:
                continue
            if (
                all(
                    layers.get(layer, {}).get("status") in SUCCESS
                    for layer in ("result", "box")
                )
                and game.get("mappings_status") in SUCCESS
            ):
                accepted.append(gid)
            players.update(
                {
                    k: check[k]
                    for k in (
                        "played_rows",
                        "retained_rows",
                        "dropped_rows",
                        "unexpected_rows",
                        "unresolved_rows",
                        "duplicate_rows",
                    )
                }
            )
            missing.update(check["missing_inputs"])
            for pid, nba_id in check["identities"].items():
                if pid in identities and identities[pid] != nba_id:
                    conflicts.add(pid)
                identities[pid] = nba_id
        days[day] = {
            "discovery_ok": discovery_ok,
            "expected_games": expected,
            "accepted_games": accepted,
            "missing_games": sorted(set(expected) - set(accepted)),
            "players": dict(players),
            "missing_inputs": dict(missing),
        }
    reverse = Counter(identities.values())
    conflicts.update(pid for pid, nid in identities.items() if reverse[nid] > 1)
    dates = [str(first + timedelta(days=n)) for n in range((last - first).days + 1)]
    scoped = [days.get(d, {}) for d in dates]
    missing_dates = [
        d for d, check in zip(dates, scoped) if not check.get("discovery_ok")
    ]
    missing_games = sorted(
        {gid for check in scoped for gid in check.get("missing_games", [])}
    )
    game_counts = Counter(
        gid for check in scoped for gid in check.get("expected_games", [])
    )
    duplicate_games = sorted(gid for gid, count in game_counts.items() if count > 1)
    players, missing = Counter(), Counter()
    through = None
    contiguous = True
    for day, check in zip(dates, scoped):
        players.update(check.get("players", {}))
        missing.update(check.get("missing_inputs", {}))
        okay = (
            check.get("discovery_ok")
            and not check.get("missing_games")
            and not any(
                check.get("players", {}).get(k, 0)
                for k in (
                    "dropped_rows",
                    "unexpected_rows",
                    "unresolved_rows",
                    "duplicate_rows",
                )
            )
        )
        contiguous = bool(contiguous and okay)
        if contiguous:
            through = day
    issues = [
        key
        for key, condition in {
            "missing_dates": bool(missing_dates),
            "missing_games": bool(missing_games),
            "dropped_rows": players["dropped_rows"] > 0,
            "unexpected_rows": players["unexpected_rows"] > 0,
            "unresolved_rows": players["unresolved_rows"] > 0,
            "duplicate_rows": players["duplicate_rows"] > 0,
            "identity_conflicts": bool(conflicts),
            "refresh_failed": report.get("status") != "success",
            "duplicate_games": bool(duplicate_games),
            "unexpected_games": bool(unexpected_games),
        }.items()
        if condition
    ]
    common_ready = not issues
    if report.get("status") != "success":
        through = previous.get("games_checked_through") if reusable else None
    return {
        "version": VERSION,
        "provider": "espn",
        "season": report["season"],
        "first_date": first_date,
        "checked_through": str(last),
        "games_checked_through": through if not conflicts else None,
        "dates_expected": len(dates),
        "dates_checked": len(dates) - len(missing_dates),
        "expected_final_games": sum(len(c.get("expected_games", [])) for c in scoped),
        "accepted_games": sum(len(c.get("accepted_games", [])) for c in scoped),
        "missing_dates": missing_dates,
        "missing_games": missing_games,
        "duplicate_games": duplicate_games,
        "unexpected_games": unexpected_games,
        "player_checks": dict(players),
        "metric_checks": {
            key: {
                "coverage_ready": common_ready and missing[key] == 0,
                "missing_input_rows": missing[key],
            }
            for key in metric_keys
        },
        "issues": issues,
        "status": "passed" if common_ready else "blocked",
        "qualification": {
            "min_games": load_contract().min_games,
            "policy": load_contract().version,
        },
        "label": "ESPN-reported",
        "rankings_enabled": False,
        "limitations": "Checks cover ESPN-listed finals and appearances, not independent NBA completeness. Metric readiness is a coverage prerequisite; serving source and qualification guards remain in force.",
        "days": {d: days[d] for d in dates if d in days},
        "identities": identities,
        "identity_conflicts": sorted(conflicts),
    }


def summary(ledger):
    return {
        k: v
        for k, v in ledger.items()
        if k not in {"days", "identities", "identity_conflicts"}
    }
