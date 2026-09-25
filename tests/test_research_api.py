"""Actual JSON/SSE routes share deterministic evidence with the page endpoint."""

import json

import pytest
from app import main
from app.agent import research_ask
from app.research import ResearchQuery
from tests.test_api import _test_settings, build_client
from tests.test_research import evidence
from tests.test_teammate_ask import Client


@pytest.fixture
def research_client(monkeypatch):
    monkeypatch.setattr(main, "load_research_evidence", lambda *args: evidence())
    monkeypatch.setattr(
        research_ask, "load_research_evidence", lambda *args: evidence()
    )
    query = ResearchQuery(
        player_ids=[1, 2], home_away="home", metrics=["pts", "fg_pct"]
    ).model_dump(mode="json")
    planner = Client(dict(kind="breakdown", query=query, pair_id=None, message=""))
    client = build_client(
        settings=_test_settings(
            openai_api_key="test-key",
            research_snapshot_path="fixture",
            agent_rate_limit_per_minute=0,
            agent_rate_limit_daily=0,
        ),
        agent_client=planner,
    )
    yield client, query, planner
    main.app.dependency_overrides.clear()


def test_page_api_ask_json_and_sse_match(research_client):
    client, query, _ = research_client
    direct = client.post("/api/research/breakdown", json=query)
    assert direct.status_code == 200, direct.text
    result = direct.json()
    answer = client.post(
        "/api/agent/ask", json={"question": "Research Player 1 and Player 2 at home"}
    )
    assert answer.status_code == 200, answer.text
    assert answer.json()["research"] == result
    stream = client.post(
        "/api/agent/ask/stream",
        json={"question": "Research Player 1 and Player 2 at home"},
    )
    assert stream.status_code == 200, stream.text
    events = [
        json.loads(line[6:])
        for line in stream.text.splitlines()
        if line.startswith("data: ")
    ]
    assert any(
        e.get("research") == result or e.get("payload", {}).get("research") == result
        for e in events
    ), events
    for url in ("/research", "/players/7", "/compare?player_a_id=7&player_b_id=8"):
        page = client.get(url)
        assert page.status_code == 200, page.text
        assert "data-research-root" in page.text
    assert len(client.get("/api/research/studies").json()["studies"]) == 3


def test_unsupported_scope_and_missing_context_never_substitute(research_client):
    client, query, planner = research_client
    assert (
        client.post(
            "/api/research/breakdown", json={**query, "metrics": ["invented"]}
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/research/breakdown", json={**query, "season": "2024-25"}
        ).status_code
        == 400
    )
    response = client.post(
        "/api/research/breakdown",
        json={
            **query,
            "player_ids": [1],
            "teammate_id": 2,
            "teammate_status": "participated",
        },
    )
    assert response.status_code == 422
    planner.result["query"]["start"] = "2025-10-01"
    answer = client.post(
        "/api/agent/ask", json={"question": "Research Player 1 from 2025-11-01"}
    ).json()
    assert answer["research_status"] == "unsupported"
    assert not answer["tables"]
