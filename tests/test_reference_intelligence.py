"""Standalone reference acceptance against governed fixture evidence, no keys."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from app.agent.conversation import InMemoryConversationStore
from app.agent.observability import AgentTrace
from app.agent.service import StatsAgent
from fastapi.testclient import TestClient
from tests.test_semantic_migration import Warehouse, settings


def source():
    warehouse = Warehouse()
    for row in warehouse.rows:
        row["player_name"] = {1: "Alex Example", 2: "Blair Sample", 3: "Casey Orbit"}[
            row["player_id"]
        ]
    return warehouse


def config():
    return replace(
        settings(),
        openai_api_key=None,
        anthropic_api_key=None,
        openrouter_api_key=None,
        ask_intelligence_provider="reference",
    )


def agent(warehouse=None):
    return StatsAgent(
        config(),
        SimpleNamespace(),
        semantic_warehouse=warehouse or source(),
        conversation_store=InMemoryConversationStore(),
    )


@pytest.mark.parametrize(
    "question,route,status",
    [
        (
            "Show Alex Example's overview for the 2024-25 regular season.",
            "player_overview",
            "ok",
        ),
        (
            "Compare Alex Example and Blair Sample for the 2024-25 regular season.",
            "player_comparison",
            "ok",
        ),
        (
            "Compare Alex Example's points per game in the last 5 games versus the prior 5 games in the 2024-25 regular season.",
            "player_split",
            "ok",
        ),
        (
            "Compare Alex Example's points per game in the 2024-25 regular season versus the 2023-24 regular season.",
            "governed_metrics",
            "ok",
        ),
        (
            "Top 5 players by points per game in the 2024-25 regular season, minimum 10 games.",
            "governed_metrics",
            "empty_cohort",
        ),
        (
            "Top 5 players by field goal percentage in the 2024-25 regular season.",
            "governed_metrics",
            "clarification_required",
        ),
        (
            "Show Alex Example's overview for the 2024-25 regular season, only fourth quarters.",
            "player_overview",
            "unsupported_coverage",
        ),
    ],
)
def test_reference_acceptance_without_model(question, route, status):
    trace = AgentTrace("reference-test", "", "fixture")
    payload = agent().answer(question, trace=trace)
    assert payload["status"] == status, payload
    assert payload["ask_route"]["route"] == route
    assert trace.model_calls == 0
    if status == "ok":
        assert (
            payload.get("semantic_evidence")
            or payload.get("overview_evidence")
            or payload.get("comparison_evidence")
            or payload.get("split_evidence")
        )


def test_metric_change_requires_fresh_plan_and_keeps_authorized_scope():
    service = agent()
    service.answer(
        "Alex Example points per game in the 2024-25 regular season",
        conversation_id="a",
    )
    result = service.answer("What about rebounds instead?", conversation_id="a")
    assert result["status"] == "ok", result
    query = result["query_plan"]["queries"][0]
    assert (query["metric"], query["player_id"], query["season"]) == (
        "reb",
        1,
        "2024-25",
    )


def test_season_comparison_keeps_both_coverage_scopes():
    service = agent()
    question = "Compare Alex Example's points per game in the 2024-25 regular season versus the 2023-24 regular season."
    result = service.answer(question)
    assert result["status"] == "ok"
    assert [q["season"] for q in result["query_plan"]["queries"]] == [
        "2024-25",
        "2023-24",
    ]
    warehouse = source()
    warehouse.coverage = [c for c in warehouse.coverage if c[0] != "2023-24"]
    missing = agent(warehouse).answer(question)
    assert missing["status"] != "ok"
    assert not missing.get("tables")


def test_ambiguous_overview_does_not_guess_identity():
    warehouse = source()
    for row in warehouse.rows:
        if row["player_id"] == 3:
            row["player_name"] = "Casey Example"
    payload = agent(warehouse).answer(
        "Show Example's overview for the 2024-25 regular season."
    )
    assert payload["status"] == "clarification_required"
    assert len(payload["clarification_options"]) == 2


def test_unrecognized_modifier_is_not_dropped():
    payload = agent().answer(
        "Alex Example points per game in the 2024-25 regular season while wearing blue shoes"
    )
    assert payload["status"] != "ok"
    assert not payload.get("tables")


def test_json_sse_parity_without_credentials():
    from app.main import app, get_agent_client, get_repository, get_settings
    from app.repository import BigQueryWarehouseRepository

    repo = BigQueryWarehouseRepository(config(), client=SimpleNamespace())
    repo._governed_warehouse = source()
    previous = dict(app.dependency_overrides)
    app.dependency_overrides.update(
        {
            get_settings: config,
            get_repository: lambda: repo,
            get_agent_client: lambda: None,
        }
    )
    try:
        with TestClient(app) as client:
            question = "Alex Example points per game in the 2024-25 regular season"
            normal = client.post(
                "/api/agent/ask?season=2024-25", json={"question": question}
            )
            stream = client.post(
                "/api/agent/ask/stream?season=2024-25", json={"question": question}
            )
        assert normal.status_code == stream.status_code == 200
        final = next(
            json.loads(line[6:])["payload"]
            for line in stream.text.splitlines()
            if line.startswith("data: ") and json.loads(line[6:]).get("type") == "final"
        )
        for field in (
            "status",
            "tables",
            "semantic_evidence",
            "query_plan",
            "ask_route",
        ):
            assert normal.json()[field] == final[field]
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


@pytest.mark.parametrize(
    "question",
    [
        "Should I drop Alex Example in fantasy?",
        "Should I trade Alex Example for Blair Sample in fantasy?",
        "Show Alex Example overview; should I drop him in fantasy?",
    ],
)
def test_fantasy_scope_precedes_public_handlers(question):
    result = agent().answer(question)
    assert result["status"] == "clarification_required"
    assert "scoring categories" in str(result)
    assert "attempt threshold" not in str(result)
