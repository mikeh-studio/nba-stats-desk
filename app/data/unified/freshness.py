"""Frozen weekly freshness rules using original captures and correction milestones."""

from __future__ import annotations

import hashlib
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from scripts import espn_corrections as layers
from scripts import espn_sources as source

from .artifacts import source_text
from .identity import evidence, require
from .selection import timestamp
from .store import parse_json

NY, LA = ZoneInfo("America/New_York"), ZoneInfo("America/Los_Angeles")
HARD_LIMIT = 10 * 86400
WARNING = 8 * 86400


def next_weekly_refresh(due):
    local = due.astimezone(LA)
    day = local.date() + timedelta(days=(6 - local.weekday()) % 7)
    result = datetime.combine(day, time(8), LA)
    if result < due:
        result += timedelta(days=7)
    return result.astimezone(timezone.utc)


def revalidation_times(observations, checks, as_of):
    by_id = {o["observation_id"]: o for o in observations}
    require(
        len({c["observation_id"] for c in checks}) == len(checks),
        "Duplicate correction revalidation",
    )
    result = {}
    for check in checks:
        require(
            check["observation_id"] in by_id,
            "Correction check outside pinned observations",
        )
        obs = by_id[check["observation_id"]]
        require(
            obs["semantic"]["provider"] == "espn",
            "Current correction adapter supports ESPN only",
        )
        receipt = evidence(check["receipt"])
        require(
            receipt["kind"] in ("provider_capture", "synthetic_fixture")
            and receipt["captured_at"] is not None,
            "Correction validation needs an original source capture",
        )
        captured = timestamp(receipt["captured_at"])
        require(captured <= as_of, "Correction capture postdates selection")
        payload = parse_json(source_text(check))
        s = obs["semantic"]["scope"]
        game = source.event_game(
            payload["header"], s["season"], date.fromisoformat(s["date"])
        )
        box = layers.normalize(payload, game, "box")
        require(
            game["source_game_id"] == obs["source_game_id"]
            and layers.validate(box) == obs["source_chain"]["box_revision"]
            and box["result_revision"] == obs["source_chain"]["result_revision"],
            "Correction capture belongs to changed or different facts",
        )
        result[check["observation_id"]] = captured
    return result


def assess(requested, authority, observations, revalidations, as_of, unresolved=False):
    if requested["season"] == "2025-26":
        require(
            not revalidations,
            "Historical carry-forward does not accept current-only revalidations",
        )
        return "not_required", []
    verified = revalidation_times(observations, revalidations, as_of)
    proof = authority["payload"]["authority"]
    discovery = proof.get("discovery") if proof else None
    if discovery is None:
        return "not_established", ["upstream_discovery_capture_missing"]
    require(
        discovery["inventory_sha256"] == proof["attested_inventory_sha256"],
        "Discovery capture is not attested to the same inventory",
    )
    receipt = evidence(discovery["receipt"])
    require(
        receipt["kind"] in ("provider_capture", "synthetic_fixture")
        and receipt["captured_at"] is not None,
        "Discovery freshness needs a source capture, not a review clock",
    )
    require(
        discovery["body"]
        and hashlib.sha256(discovery["body"].encode()).hexdigest() == receipt["sha256"],
        "Discovery capture hash differs",
    )
    captured = timestamp(receipt["captured_at"])
    require(captured <= as_of, "Discovery capture postdates selection")
    reasons = set()
    age = (as_of - captured).total_seconds()
    if age > HARD_LIMIT:
        reasons.add("upstream_discovery_stale")
    elif age > WARNING:
        reasons.add("upstream_discovery_age_warning")
    end = datetime.combine(
        date.fromisoformat(requested["last"]) + timedelta(days=1), time(), NY
    )
    if (as_of - end).total_seconds() > HARD_LIMIT:
        reasons.add("current_cutoff_lag_exceeded")
    for obs in observations:
        captured = obs.get("source_chain", {}).get("source", {}).get("captured_at")
        last_checked = timestamp(captured) if captured else None
        last_checked = max(
            [
                t
                for t in (last_checked, verified.get(obs["observation_id"]))
                if t is not None
            ],
            default=None,
        )
        if last_checked is None:
            reasons.add("correction_capture_clock_missing")
            continue
        require(last_checked <= as_of, "Original source capture postdates selection")
        played = date.fromisoformat(obs["semantic"]["scope"]["date"])
        for offset in (*range(1, 8), 14, *([21, 28] if unresolved else []), 30):
            due = datetime.combine(played + timedelta(days=offset), time(12), NY)
            scheduled = next_weekly_refresh(due)
            if last_checked < due and (as_of - scheduled).total_seconds() > HARD_LIMIT:
                reasons.add("correction_milestone_overdue")
        if unresolved and as_of > datetime.combine(
            played + timedelta(days=30), time(12), NY
        ):
            reasons.add("unresolved_after_final_milestone_needs_review")
    hard_reasons = reasons - {"upstream_discovery_age_warning"}
    return ("stale" if hard_reasons else "fresh"), sorted(reasons)


def correction_reviews(observations, checks, disputes, mismatches, as_of):
    """Open discrepancy milestones are scoped to their observation and components."""
    from .historical import COUNTS

    verified = revalidation_times(observations, checks, as_of)
    result = []
    for obs in observations:
        if obs["semantic"]["scope"]["season"] == "2025-26":
            continue
        selected = [d for d in disputes if d["observation_id"] == obs["observation_id"]]
        structural = obs["semantic"]["game_uid"] in mismatches or any(
            d["kind"] in ("identity", "membership", "result") for d in selected
        )
        held = (
            set(COUNTS) | {"min"}
            if structural
            else {
                c
                for d in selected
                for c in (d["components"] if d["kind"] == "component" else ["min"])
            }
        )
        if not held:
            continue
        original = obs.get("source_chain", {}).get("source", {}).get("captured_at")
        clocks = [
            t
            for t in (
                timestamp(original) if original else None,
                verified.get(obs["observation_id"]),
            )
            if t is not None
        ]
        checked = max(clocks, default=None)
        played = date.fromisoformat(obs["semantic"]["scope"]["date"])
        reasons = []
        for offset in (21, 28):
            due = datetime.combine(played + timedelta(days=offset), time(12), NY)
            if (checked is None or checked < due) and (
                as_of - next_weekly_refresh(due)
            ).total_seconds() > HARD_LIMIT:
                reasons.append("unresolved_correction_milestone_overdue")
        if as_of > datetime.combine(played + timedelta(days=30), time(12), NY):
            reasons.append("unresolved_after_final_milestone_needs_review")
        result.append(
            {
                "observation_id": obs["observation_id"],
                "membership_hold": structural,
                "components": sorted(held),
                "state": "needs_review"
                if "unresolved_after_final_milestone_needs_review" in reasons
                else "overdue"
                if reasons
                else "open",
                "reasons": sorted(set(reasons)),
            }
        )
    return sorted(result, key=lambda r: r["observation_id"])
