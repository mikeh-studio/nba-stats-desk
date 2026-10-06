"""Unsupported series scope cannot be silently broadened by any Ask route."""

import json
from types import SimpleNamespace

import pytest
from app.agent.performance_overview import overview_scope
from app.agent.question_intent import round_scope_message
from app.agent.semantic_answer import SemanticAsk
from app.agent.semantics import SemanticError
from tests.test_semantic_migration import settings


@pytest.mark.parametrize(
    "scope",
    [
        "the final",
        "NBA Finals",
        "conference final",
        "championship series",
        "first round",
        "first-round",
        "second-round playoffs",
        "final round",
        "earlier playoff rounds",
        "round 2",
    ],
)
def test_round_scope_withholds_before_warehouse_or_planner(scope):
    def fail(*args):
        pytest.fail("Unsupported rounds must not load evidence")

    question = f"How well did Jalen Brunson played in {scope}?"
    result = SemanticAsk(settings("2025-26"), SimpleNamespace(load=fail)).answer(
        question, client=None, model="none"
    )
    assert result["status"] == "unsupported_scope"
    assert not result["semantic_evidence"]
    assert "cannot be isolated" in result["answer"]
    with pytest.raises(SemanticError, match="cannot be isolated"):
        overview_scope(question, "2025-26")


@pytest.mark.parametrize(
    "question",
    [
        "Curry points this regular season",
        "Curry final 10 games",
        "What was the final score?",
    ],
)
def test_round_guard_does_not_match_unrelated_final_usage(question):
    assert round_scope_message(question) is None


def test_final_scope_json_and_stream_endpoints():
    from app.main import app, get_agent_client, get_repository, get_settings
    from app.repository import BigQueryWarehouseRepository
    from fastapi.testclient import TestClient

    config = settings("2025-26")
    repo = BigQueryWarehouseRepository(config, client=SimpleNamespace())
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
            body = {"question": "How well did Jalen Brunson played in the final?"}
            direct = client.post("/api/agent/ask", json=body)
            assert direct.status_code == 200
            assert direct.json()["status"] == "unsupported_scope"
            assert not direct.json().get("semantic_evidence")
            streamed = client.post("/api/agent/ask/stream", json=body)
            assert streamed.status_code == 200
            events = [
                json.loads(line[6:])
                for line in streamed.text.splitlines()
                if line.startswith("data: ")
            ]
            final = next(e for e in events if e.get("type") == "final")
            result = final.get("payload", final.get("response", final))
            assert result["answer"] == direct.json()["answer"]
            assert result["status"] == "unsupported_scope"
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(old)
