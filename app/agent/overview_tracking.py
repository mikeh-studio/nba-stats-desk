"""Private, bounded capability telemetry. Never include raw questions or answers."""

import json
import logging
import re

from app.agent.overview_insights import VERSION
from app.agent.overview_observations import comparison_change, complete

LOGGER_NAME = "app.overview_tracking"
logger = logging.getLogger(LOGGER_NAME)
logger.setLevel(logging.INFO)
logger.propagate = False
if not logger.handlers:
    # Uvicorn has no default application INFO handler. Emit only structured
    # tracking JSON to the private server stream, independently of root logging.
    logger.addHandler(logging.StreamHandler())


def emit_tracking(request_id, events, outcome):
    for event in deduplicated(events):
        logger.info(
            json.dumps(
                {
                    "event_name": "overview_capability_tracking",
                    "request_id": request_id,
                    "renderer_version": VERSION,
                    "outcome": outcome,
                    **event,
                },
                sort_keys=True,
            )
        )


CAPABILITIES = {
    "game_variation": r"\b(?:spread|fluctuat\w*|consisten\w*|variability|variation)\b",
    "standout_game": r"\b(?:best|highest|peak|standout) game\b",
    "position_comparison": r"\b(?:guards|wings|big men|bigs|similar positions?)\b",
    "screens": r"\bscreens?\b|\bscreen assists?\b",
    "secondary_assists": r"\bsecondary assists?\b|\bhockey assists?\b",
    "potential_assists": r"\bpotential assists?\b",
    "defensive_assignments": r"\bdefensive (?:assignments?|positioning)\b",
    "shot_quality": r"\bshot quality\b|\bshot difficulty\b",
    "lineup_context": r"\blineups?\b|\bteammate absences?\b",
    "opponent_context": r"\bopponent strength\b|\bstrength of (?:schedule|opponents?)\b",
    "rebound_opportunities": r"\bbox[ -]outs?\b|\brebound(?:ing)? (?:chances|opportunities)\b",
}
FEEDBACK = {
    "overview_repeats_table": r"\b(?:repeats?|repeating)\s+(?:the\s+)?table\b|\b(?:just|only)\s+(?:reporting\s+)?numbers\b",
    "overview_missing_context": r"\b(?:needs?|missing|lacks?)\s+(?:league\s+|baseline\s+)?context\b",
    "unwanted_limitation_boilerplate": r"\b(?:don't|do not|stop)\s+(?:list(?:ing)?|mention(?:ing)?)\s+(?:the\s+)?(?:limitations|unavailable metrics)\b",
}


def requested_capabilities(question):
    found = []
    for capability, pattern in CAPABILITIES.items():
        matches = re.finditer(pattern, question[:6000], re.I)
        if any(
            not re.search(
                r"(?:don't|do not|omit|exclude|without)\b[^.!?]{0,50}$",
                question[max(0, m.start() - 60) : m.start()],
                re.I,
            )
            for m in matches
        ):
            found.append(
                {
                    "kind": "explicit_demand",
                    "capability": capability,
                    "reason": "requested",
                }
            )
    for code, pattern in FEEDBACK.items():
        if re.search(pattern, question[:6000], re.I):
            found.append(
                {
                    "kind": "product_feedback",
                    "capability": code,
                    "reason": "user_reported",
                }
            )
    return found


def overview_events(payload):
    evidence = payload.get("semantic_evidence") or {}
    if not isinstance(evidence, dict) or "presentation" not in evidence:
        return []
    metrics = evidence.get("metrics", [])
    scope = evidence.get("scope", {})
    safe_scope = {
        k: scope[k]
        for k in (
            "start",
            "end",
            "phases",
            "previous_start",
            "previous_end",
            "previous_phases",
        )
        if k in scope
    }
    context = {
        "scope": safe_scope,
        "renderer_version": VERSION,
        "profile": evidence["presentation"].get("profile"),
        "evidence_ref": evidence.get("snapshot_id"),
        "player_id": evidence.get("player_id"),
    }
    events = [
        {
            "kind": "overview_assessed",
            "capability": "overview",
            "reason": "evaluated",
            **context,
        }
    ]
    for metric in metrics:
        key = metric.get("key")
        if key not in {
            "pts",
            "ast",
            "tov",
            "reb",
            "stl",
            "blk",
            "min",
            "ts_pct",
            "pts_per36",
            "ast_per36",
        }:
            continue
        if not complete(metric):
            reason = (
                "partial_components"
                if metric.get("missing_component_games")
                else "missing_value"
            )
            events.append(
                {"kind": "evidence_gap", "capability": key, "reason": reason, **context}
            )
        elif comparison_change(metric) is None:
            events.append(
                {
                    "kind": "evidence_gap",
                    "capability": key,
                    "reason": "baseline_ineligible",
                    **context,
                }
            )
        if (
            key in {"pts", "ast", "reb", "stl", "blk"}
            and metric.get("percentile_status") != "ranked"
        ):
            events.append(
                {
                    "kind": "evidence_gap",
                    "capability": key,
                    "reason": "peer_comparison_ineligible",
                    **context,
                }
            )
    fulfillment = evidence.get("request_fulfillment", {})
    if fulfillment.get("requested_group"):
        events.append(
            {
                "kind": "request_fulfillment",
                "capability": "position_comparison",
                "reason": fulfillment.get("reason") or "fulfilled",
                "fulfillment_status": fulfillment["status"],
            }
        )
    for metric in metrics:
        p = metric.get("peer_comparison")
        if not p:
            continue
        n, coverage = p["cohort_size"], p["coverage"]
        events.append(
            {
                "kind": "comparison_opportunity",
                "capability": "position_comparison",
                "reason": p.get("fallback_reason") or "position_available",
                "policy_version": p["version"],
                "mapping_version": p["mapping_version"],
                "cohort_type": p["kind"],
                "presentation_mode": p["mode"],
                "cohort_size_bucket": "20_plus"
                if n >= 20
                else "10_19"
                if n >= 10
                else "under_10",
                "coverage_bucket": "95_plus" if coverage >= 0.95 else "below_95",
            }
        )
    return events


def deduplicated(events):
    # One event per kind/capability/reason in a request, including retry paths.
    return list(
        {(e["kind"], e["capability"], e["reason"]): e for e in events}.values()
    )[:40]
