"""Ask route labels for payloads, service logs, stored context and route evaluation.

A label names the answer path that produced a payload. Handler refusals and
clarifications keep their handler label and carry a ``reason_code``; ``clarify``
and ``unsupported`` are reserved for decisions made before any answer handler.
Labels and reason codes never contain question text.
"""

from __future__ import annotations

from typing import Any

from app.agent.legacy_types import AgentRoute

ROUTE_LABELS = (
    "unsupported",
    "clarify",
    "event_participation",
    "appearances",
    "availability",
    # Research planner reached, sub-route not chosen (planner refusal or failure).
    "research",
    "research_breakdown",
    "teammate_study",
    "teammate_study_v1",
    "similarity",
    "league_baseline",
    "award",
    "player_split",
    "player_comparison",
    "player_overview",
    "governed_metrics",
)
LEGACY_LABELS = tuple(f"legacy:{route.value}" for route in AgentRoute)
# Intended routes with no handler yet; valid only as route-evaluation expectations.
PLANNED_LABELS = ("opponent_splits",)
ROUTE_SOURCES = ("deterministic", "prior_route", "pending_clarification", "model")

# Registry keys whose payload label differs; other keys are already labels.
DISPATCH_LABELS = {
    "unsupported_round": "unsupported",
    "teammate_study": "teammate_study_v1",
    "overview": "player_overview",
    "metrics": "governed_metrics",
    "legacy_repository": "legacy",
}


def is_route_label(value: Any) -> bool:
    return value in ROUTE_LABELS or value in LEGACY_LABELS


def route_hint(payload: dict[str, Any], **fields: str | None) -> dict[str, Any]:
    """Record what a handler knows about its route; the dispatcher fills the rest."""
    values = {key: value for key, value in fields.items() if value is not None}
    if values:
        payload.setdefault("ask_route", {}).update(values)
    return payload


def status_reason(payload: dict[str, Any]) -> str | None:
    """A refusal or clarification code already present in the payload, if any."""
    status = payload.get("status")
    if status and status != "ok":
        return str(status)
    if payload.get("research_status") == "unsupported":
        return str(payload.get("research_error_code") or "unsupported")
    study = payload.get("study_status")
    if study and study != "answered":
        return str(study)
    return None


def finish_route(
    payload: dict[str, Any], *, route: str, source: str, trace: Any = None
) -> dict[str, Any]:
    """Complete ``ask_route`` from dispatch defaults and record it on the trace."""
    hint = payload.get("ask_route") or {}
    label = hint.get("route") or route
    if label == "legacy":
        legacy = (payload.get("agent_plan") or {}).get("route") or "clarify"
        label = f"legacy:{legacy}"
    if not is_route_label(label):
        raise ValueError(f"Unknown Ask route label: {label}")
    chosen = hint.get("source") or source
    if chosen not in ROUTE_SOURCES:
        raise ValueError(f"Unknown Ask route source: {chosen}")
    payload["ask_route"] = {
        "route": label,
        "source": chosen,
        "reason_code": hint.get("reason_code") or status_reason(payload),
    }
    if trace is not None:
        trace.ask_route = dict(payload["ask_route"])
        if not label.startswith("legacy:"):
            # Legacy traces keep their planner route; ask_route carries the label.
            trace.set_plan(
                route=label,
                confidence=trace.confidence if trace.confidence is not None else 1.0,
            )
    return payload
