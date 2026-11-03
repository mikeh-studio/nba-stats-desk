"""Main prose is verified at both API transports using fictional observations."""

import json
import logging
from threading import Event
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from tests import test_performance_overview as overview_tests
from tests.support.semantic import settings


@pytest.fixture
def evidence():
    return overview_tests.evidence.__wrapped__()


@pytest.mark.parametrize("suffix", ["", "/stream"])
@pytest.mark.parametrize(
    "variant",
    ["complete", "missing_scoring", "partial_turnovers", "thin", "missing_baseline"],
)
def test_governed_overview_main_prose_at_endpoints(
    evidence, suffix, variant, caplog, monkeypatch
):
    caplog.set_level(logging.INFO, logger="app.agent")
    from app.agent.overview_tracking import logger

    monkeypatch.setattr(logger, "handlers", [caplog.handler])
    from app.agent.observability import AgentTrace

    emitted = Event()
    original_emit = AgentTrace.emit

    def emit(trace):
        original_emit(trace)
        emitted.set()

    monkeypatch.setattr(AgentTrace, "emit", emit)
    from app.main import app, get_agent_client, get_repository, get_settings
    from app.repository import BigQueryWarehouseRepository

    for row in evidence.rows:
        row.update(min=30, tov=2, blk=0)
        if row["player_id"] == 1 and row["season"] == "2025-26":
            if variant == "missing_scoring":
                row["pts"] = None
            if variant == "partial_turnovers" and row["game_date"] == "2025-11-01":
                row["tov"] = None
        if variant == "missing_baseline" and row["season"] == "2024-25":
            for key in ("pts", "ast", "tov", "reb", "min"):
                row[key] = None
    if variant == "thin":
        evidence.rows[:] = [
            r
            for r in evidence.rows
            if r["season"] != "2025-26" or r["game_date"] <= "2025-11-02"
        ]

    config = settings("2025-26")
    repo = BigQueryWarehouseRepository(config, client=SimpleNamespace())
    repo._governed_warehouse = SimpleNamespace(
        load=lambda seasons: ({"capture": {}}, evidence)
    )
    old = dict(app.dependency_overrides)
    app.dependency_overrides.update(
        {
            get_settings: lambda: config,
            get_repository: lambda: repo,
            get_agent_client: lambda: object(),
        }
    )
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/agent/ask" + suffix,
                json={
                    "question": "Give me an overview of Player 1 in the 2025-26 regular season."
                },
            )
            assert response.status_code == 200
            if suffix:
                events = [
                    json.loads(line[6:])
                    for line in response.text.splitlines()
                    if line.startswith("data: ")
                ]
                data = next(e["payload"] for e in events if e.get("type") == "final")
            else:
                data = response.json()
            assert data["status"] == "ok"
            ev = data["semantic_evidence"]
            assert ev["player_id"] == 1
            assert ev["scope"]["phases"] == ["Regular Season"]
            assert ev["scope"]["start"] == "2025-07-01"
            assert ev["scope"]["end"] == "2026-06-30"
            assert ev["scope"]["previous_start"] == "2024-07-01"
            text = data["answer"]
            assert text == "\n\n".join(ev["summary_paragraphs"])
            assert "4.0 assists" in text
            assert "positioning" not in text and "screens" not in text.lower()
            assert "overview_capability_tracking" not in response.text
            from app.agent.overview_insights import VERSION

            assert ev["insight_version"] == VERSION
            assert 1 <= len(ev["insights"]) <= 3
            if variant == "partial_turnovers":
                assert "2.0 turnovers" not in text
            elif variant != "thin":
                assert "2.0 turnovers" in text
            if variant == "missing_scoring":
                assert "20.0 points" not in text
                assert "Creation" in [
                    n["area"] for n in ev["presentation"]["takeaways"]
                ]
            else:
                assert "20.0 points" in text
            if variant in ("thin", "missing_baseline"):
                assert "scoring rose" not in text and "assists rose" not in text
                assert (
                    next(m for m in ev["metrics"] if m["key"] == "pts")[
                        "display_change"
                    ]
                    is None
                )
            if variant == "thin":
                assert "small sample" in text and "exceeded" not in text
            assert emitted.wait(2), "Worker telemetry did not complete"
            records = [
                json.loads(r.message)
                for r in caplog.records
                if r.name in ("app.agent", "app.overview_tracking")
                and r.message.startswith("{")
            ]
            summary = next(
                r for r in records if r.get("event_name") == "agent_request_summary"
            )
            assert summary["stage_latency_ms"]["overview"] > 0
            assert summary["stage_exclusive_latency_ms"]["overview"] > 0
            tracking = [
                r
                for r in records
                if r.get("event_name") == "overview_capability_tracking"
            ]
            assert any(r["kind"] == "overview_assessed" for r in tracking)
            assert not any(r["kind"] == "explicit_demand" for r in tracking)
            if variant == "partial_turnovers":
                assert any(
                    r["capability"] == "tov" and r["reason"] == "partial_components"
                    for r in tracking
                )
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(old)
