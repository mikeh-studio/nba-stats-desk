import json
import logging

from app.agent.observability import AgentTrace
from app.agent.overview_tracking import (
    deduplicated,
    overview_events,
    requested_capabilities,
)
from scripts.summarize_overview_tracking import summarize


def test_explicit_demand_is_separate_and_deduplicated():
    events = requested_capabilities(
        "Show screens, screen assists and secondary assists."
    )
    assert {e["capability"] for e in events} == {"screens", "secondary_assists"}
    assert all(e["kind"] == "explicit_demand" for e in events)
    assert len(deduplicated(events + events)) == 2
    assert requested_capabilities("An overview of Example") == []
    assert requested_capabilities("Don't include screens.") == []


def test_evidence_gaps_do_not_invent_demand():
    events = overview_events(
        {
            "semantic_evidence": {
                "presentation": {"profile": "general"},
                "snapshot_id": "fixture-hash",
                "scope": {
                    "start": "2025-07-01",
                    "end": "2026-06-30",
                    "question": "secret",
                },
                "metrics": [
                    {"key": "ast", "value": None, "missing_component_games": 2}
                ],
            }
        }
    )
    assert events[0]["kind"] == "overview_assessed"
    assert {e["reason"] for e in events[1:]} == {
        "partial_components",
        "peer_comparison_ineligible",
    }
    assert all(e["kind"] != "explicit_demand" for e in events)
    assert "secret" not in json.dumps(events)
    assert overview_events({"status": "unsupported_scope"}) == []


def test_quality_feedback_is_separate_from_capability_demand():
    events = requested_capabilities(
        "This repeats the table. Do not mention limitations."
    )
    assert {e["kind"] for e in events} == {"product_feedback"}
    assert {e["capability"] for e in events} == {
        "overview_repeats_table",
        "unwanted_limitation_boilerplate",
    }


def test_trace_logs_private_codes_without_raw_text(caplog, monkeypatch):
    from app.agent.overview_tracking import logger

    monkeypatch.setattr(logger, "handlers", [caplog.handler])
    trace = AgentTrace("test-request", "Private question about screens", "test-model")
    trace.overview_tracking = requested_capabilities(trace.question) * 2
    with caplog.at_level(logging.INFO, logger="app.agent"):
        trace.emit()
    records = [
        json.loads(r.message)
        for r in caplog.records
        if r.name in ("app.agent", "app.overview_tracking")
    ]
    tracking = [r for r in records if r["event_name"] == "overview_capability_tracking"]
    assert len(tracking) == 1
    assert tracking[0]["request_id"] == "test-request"
    assert tracking[0]["capability"] == "screens"
    assert "Private question" not in caplog.text
    assert "overview_tracking" not in trace.to_log_dict()


def test_monitor_deduplicates_exports_and_separates_counts():
    records = [
        dict(
            event_name="overview_capability_tracking",
            request_id="a",
            kind="explicit_demand",
            capability="screens",
            reason="requested",
        ),
        dict(
            event_name="overview_capability_tracking",
            request_id="b",
            kind="evidence_gap",
            capability="ast",
            reason="missing_value",
        ),
        dict(
            event_name="overview_capability_tracking",
            request_id="b",
            kind="overview_assessed",
            capability="overview",
            reason="evaluated",
        ),
    ]
    result = summarize(records * 3)
    assert result["tracked_requests"] == 2 and result["overview_requests"] == 1
    assert all(c["requests"] == 1 for c in result["counts"])
    assert {c["kind"] for c in result["counts"]} == {"explicit_demand", "evidence_gap"}
