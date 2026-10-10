"""Common Ask envelope; evidence extensions stay owned by their route."""

from typing import Any


def answer_payload(
    answer: str, *, status: str | None = None, **extensions: Any
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "answer": answer,
        "assumptions": [],
        "tables": [],
        "charts": [],
        "metric_definitions": [],
        "followups": [],
        "player_profile": None,
        "clarification_options": [],
        "tool_calls": [],
        "semantic_evidence": None,
    }
    if status is not None:
        payload["status"] = status
    payload.update(extensions)
    return payload


def complete_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Add defaults without converting route-specific refusals into successes."""
    return {**answer_payload(""), **payload}
