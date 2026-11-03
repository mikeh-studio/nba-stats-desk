"""Deterministic, observed-only source decisions with no fallback or serving hook."""

from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from .identity import native_game_id, require, scope
from .ledger import CONTRACTS, record, validate
from .store import parse_json


def timestamp(value):
    require(isinstance(value, str), "Selection time must be explicit")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(result.tzinfo is not None, "Selection time requires timezone")
    return result.astimezone(timezone.utc)


def request_scope(value):
    require(
        isinstance(value, dict) and set(value) == {"season", "phase", "first", "last"},
        "Request scope fields differ",
    )
    require(
        value["season"] in ("2025-26", "2026-27")
        and value["phase"] == "Regular Season",
        "Unsupported selection scope",
    )
    first, last = date.fromisoformat(value["first"]), date.fromisoformat(value["last"])
    year = int(value["season"][:4])
    require(
        str(first) == value["first"]
        and str(last) == value["last"]
        and date(year, 7, 1) <= first <= last <= date(year + 1, 6, 30),
        "Invalid requested date range",
    )
    return dict(value)


def policy_record():
    # Versioned local contract; no browser override or environment-based fallback.
    return record(
        "policy", parse_json((CONTRACTS / "source-selection-policy.json").read_bytes())
    )


def validate_inventory(inventory):
    validate(inventory)
    require(inventory["kind"] == "inventory", "Expected observed inventory")
    refs = inventory["payload"]["observations"]
    require(
        len({(r["game_uid"], r["provider"]) for r in refs}) == len(refs),
        "Duplicate provider game in pinned inventory",
    )
    require(
        len({r["observation_id"] for r in refs}) == len(refs),
        "Duplicate observation in pinned inventory",
    )
    require(
        len({(r["provider"], r["source_game_id"]) for r in refs}) == len(refs),
        "Native game alias collision in inventory",
    )
    require(
        refs
        == sorted(
            refs, key=lambda r: (r["scope"]["season"], r["provider"], r["game_uid"])
        ),
        "Unsorted observation inventory",
    )
    for ref in refs:
        scope(ref["scope"])
        native_game_id(ref["provider"], ref["source_game_id"], ref["scope"])
        require(
            ref["capture_times"] == sorted(set(ref["capture_times"])),
            "Duplicate/unsorted capture times",
        )
        for t in ref["capture_times"]:
            timestamp(t)
    scopes_by_uid: dict[str, tuple[tuple[str, str], ...]] = {}
    provider_scopes = set()
    for ref in refs:
        binding = tuple(sorted(ref["scope"].items()))
        require(
            ref["game_uid"] not in scopes_by_uid
            or scopes_by_uid[ref["game_uid"]] == binding,
            "Canonical game UID has conflicting scopes",
        )
        scopes_by_uid[ref["game_uid"]] = binding
        require(
            (ref["provider"], binding) not in provider_scopes,
            "Duplicate provider game scope requires identity review",
        )
        provider_scopes.add((ref["provider"], binding))
    holds = inventory["payload"]["unresolved_inputs"]
    require(
        len({r["input_id"] for r in holds}) == len(holds), "Duplicate unresolved input"
    )
    return refs, holds


def select(inventory, policy, scopes, selection_as_of):
    validate(policy)
    require(policy["kind"] == "policy", "Expected source selection policy")
    refs, holds = validate_inventory(inventory)
    require(
        isinstance(scopes, list) and 1 <= len(scopes) <= 2,
        "Selection requires one or two explicit scopes",
    )
    requests = sorted([request_scope(s) for s in scopes], key=lambda s: s["season"])
    require(
        len({(s["season"], s["phase"]) for s in requests}) == len(requests),
        "Duplicate season/phase request",
    )
    as_of = timestamp(selection_as_of)
    today = as_of.astimezone(ZoneInfo("America/New_York")).date()
    choices = {
        (s["season"], s["phase"]): s["provider"] for s in policy["payload"]["scopes"]
    }
    results = []
    for requested in requests:
        require(
            date.fromisoformat(requested["last"]) <= today,
            "Requested range extends after selection date",
        )
        provider = choices[(requested["season"], requested["phase"])]
        scoped = [
            r
            for r in refs
            if r["scope"]["season"] == requested["season"]
            and r["scope"]["phase"] == requested["phase"]
            and requested["first"] <= r["scope"]["date"] <= requested["last"]
        ]
        selected = [r for r in scoped if r["provider"] == provider]
        excluded = [r for r in scoped if r["provider"] != provider]
        relevant = [
            h
            for h in holds
            if h["provider"] == provider
            and h["season"] in (None, requested["season"])
            and h["phase"] in (None, requested["phase"])
        ]
        # Hold an unresolved input across its season. A rejected date itself may
        # be wrong; it is not safe evidence for excluding that game from a range.
        reasons = {"coverage_not_evaluated"}
        if relevant:
            reasons.add("unresolved_input")
        if not selected:
            reasons.add("missing_primary_source")
        if any(timestamp(t) > as_of for r in selected for t in r["capture_times"]):
            reasons.add("capture_after_selection_as_of")
        # Date/teams match is a hold, not an automatic game merge. Verified
        # aliases sharing one UID remain safe to select once from one provider.
        if any(
            a["game_uid"] != b["game_uid"] and a["scope"] == b["scope"]
            for a in selected
            for b in excluded
        ):
            reasons.add("unlinked_provider_overlap")
        status = (
            "refused"
            if {"missing_primary_source", "capture_after_selection_as_of"} & reasons
            else "held"
            if reasons - {"coverage_not_evaluated"}
            else "selected_observed"
        )
        dates = [r["scope"]["date"] for r in selected]
        results.append(
            {
                "scope": requested,
                "provider": provider,
                "status": status,
                "reasons": sorted(reasons),
                "selected": sorted(r["observation_id"] for r in selected)
                if status != "refused"
                else [],
                "excluded_other_provider": sorted(
                    r["observation_id"] for r in excluded
                ),
                "relevant_holds": sorted(h["input_id"] for h in relevant),
                "observed_games": len(selected),
                "observed_appearances": sum(r["appearances"] for r in selected),
                "observed_first": min(dates) if dates else None,
                "observed_last": max(dates) if dates else None,
                "validated_cutoff": None,
            }
        )
    status = (
        "refused"
        if any(s["status"] == "refused" for s in results)
        else "held"
        if any(s["status"] == "held" for s in results)
        else "selected_observed"
    )
    payload = {
        "version": "offline_source_selection/1",
        "policy_id": policy["record_id"],
        "inventory_id": inventory["record_id"],
        "selection_as_of": as_of.isoformat(),
        "status": status,
        "scopes": results,
        "coverage_evaluation": "not_evaluated",
        "historical_as_known_at_supported": False,
        "capabilities": {
            "runtime_eligible": False,
            "league_ranks": False,
            "percentiles": False,
            "pdf_corroborated": False,
        },
    }
    return record("selection", payload, (policy["record_id"], inventory["record_id"]))


def pending_check(selection, family, requested_scope, reason):
    """Explicit inert contract for the next increments; never a passed check."""
    validate(selection)
    require(selection["kind"] == "selection", "Check requires a selection")
    matches = [
        s for s in selection["payload"]["scopes"] if s["scope"] == requested_scope
    ]
    require(len(matches) == 1, "Check scope outside pinned selection")
    return record(
        "pending_check",
        {
            "version": "pending_evidence_check/1",
            "family": family,
            "selection_id": selection["record_id"],
            "scope": requested_scope,
            "observation_ids": matches[0]["selected"],
            "state": "not_attempted",
            "reason": reason,
            "capability_granted": False,
        },
        (selection["record_id"],),
    )
