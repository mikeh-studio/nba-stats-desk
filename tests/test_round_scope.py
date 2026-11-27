"""Unsupported series scope cannot be silently broadened by any Ask route."""

import json
from types import SimpleNamespace

import pytest
from app.agent.performance_overview import overview_scope
from app.agent.question_intent import round_scope_message
from app.agent.scope_policy import requested_playoff_round
from app.agent.semantic_answer import SemanticAsk
from app.agent.semantics import SemanticError
from app.agent.service import StatsAgent
from tests.test_semantic_migration import settings


@pytest.mark.parametrize(
    "scope",
    [
        "the final",
        "conference final",
        "championship series",
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
    "scope",
    [
        "outside the first round",
        "outside of the 2024-25 first round",
        "except the first round",
        "except in the first round",
        "excluding the first round",
        "excluding games from the first round",
        "in game 2 of the first round",
        "in game two of the first round",
        "in the first round's second game",
        "in the NBA Cup Finals",
        "in the In-Season Tournament Finals",
        "in the Eastern Conference Finals",
        "in the Western Conference Finals",
        "in the Eastern Conference Semifinals",
        "in the West Conference Semis",
        "outside the Eastern Conference Finals",
        "before the 2024 Western Conference Finals",
        "not counting the NBA Finals",
        "excluding the Finals",
        "except the Finals",
        "in the Finals opener",
        "in the series opener",
        "in the game opener",
        "in games 1 and 2 of the first round",
        "in games 1-3 of the NBA Finals",
        "in the first two games of the NBA Finals",
    ],
)
def test_qualified_round_is_refused_on_governed_route(scope):
    def fail(*args, **kwargs):
        pytest.fail("Unsupported round qualifiers must not load evidence or plan")

    config = settings()
    warehouse = SimpleNamespace(load=fail)
    planner = SimpleNamespace(responses=SimpleNamespace(create=fail))
    agent = StatsAgent(
        config, SimpleNamespace(), client=planner, semantic_warehouse=warehouse
    )
    question = f"How many total points did Regular Player score {scope}?"
    assert requested_playoff_round(question) is None
    result = agent.answer(question)
    assert result["status"] == "unsupported_scope"
    assert result["ask_route"]["reason_code"] == "unsupported_round"
    assert not result["semantic_evidence"]


@pytest.mark.parametrize(
    "question",
    [
        "Curry points this regular season",
        "Curry final 10 games",
        "What was the final score?",
        "How did the first-round pick play this season?",
        "Which first round pick led rookies in scoring?",
        "How did the 1st-round pick perform?",
    ],
)
def test_round_guard_does_not_match_unrelated_final_usage(question):
    assert requested_playoff_round(question) is None
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
