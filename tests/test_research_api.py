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
    for url in ("/players/7/content", "/compare?player_a_id=7&player_b_id=8"):
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


@pytest.mark.parametrize("pair_index", [0, 1, 2])
@pytest.mark.parametrize("supported", [True, False])
def test_study_json_stream_and_significance_followup(
    research_client, monkeypatch, pair_index, supported
):
    from app.agent.conversation import InMemoryConversationStore
    from app.research_studies import PAIRS, pending
    from tests.test_research_insights import metric

    client, _, planner = research_client
    pair = PAIRS[pair_index]
    study = pending(pair)
    study["scope"] = dict(
        season="2025-26",
        start="2025-11-01",
        end="2025-11-30",
        window="full_season",
        phase="Both",
    )
    study["metrics"][0] = metric()
    study["metrics"][0]["observed_uncertainty"]["holm_p_value"] = (
        0.01 if supported else 0.4
    )
    monkeypatch.setattr(research_ask, "catalog", lambda path: [study])
    planner.result = dict(
        kind="study",
        pair_id=pair["pair_id"],
        message="",
        query=ResearchQuery(
            player_ids=[pair["player_id"]],
            metrics=["pts", "plus_minus"],
        ).model_dump(mode="json"),
    )
    planner.result["query"]["teammate_id"] = pair["teammate_id"]
    request = dict(
        question=f"How does {pair['player_name']} play without {pair['teammate_name']}?"
    )
    result = client.post("/api/agent/ask", json=request)
    assert result.status_code == 200, result.text
    payload = result.json()
    assert "significance" not in payload["research_highlights"][0]
    assert "research_assessments" not in payload
    assert "study" not in payload
    assert payload["research_highlights"][1]["estimate"] is None
    assert "4.0 more points" in payload["answer"]
    assert "insufficient evidence" not in json.dumps(payload).lower()
    stream = client.post("/api/agent/ask/stream", json=request)
    assert stream.status_code == 200
    assert (
        payload["answer"] in stream.text
        or json.dumps(payload["answer"])[1:-1] in stream.text
    )
    # Exercise shared service routing with the same actual endpoint agent.
    store = InMemoryConversationStore()
    monkeypatch.setattr(main, "_season_conversation_store", lambda season: store)
    first = client.post(
        "/api/agent/ask", json={**request, "conversation_id": "research-followup"}
    )
    assert first.status_code == 200
    followup = client.post(
        "/api/agent/ask",
        json={
            "question": "Is that significant?",
            "conversation_id": "research-followup",
        },
    )
    assert followup.status_code == 200
    assert followup.json()["study_id"] == pair["pair_id"]


@pytest.mark.parametrize("explicit_dates", [False, True])
def test_partial_study_never_silently_answers_full_season(
    research_client, monkeypatch, explicit_dates
):
    from app.research_studies import PAIRS, pending

    client, _, planner = research_client
    pair = PAIRS[0]
    study = pending(pair)
    study["scope"] = dict(
        season="2025-26",
        phase="Both",
        start="2025-10-22",
        end="2025-12-31",
        window="bounded",
    )
    monkeypatch.setattr(research_ask, "catalog", lambda path: [study])
    query = ResearchQuery(player_ids=[pair["player_id"]], metrics=["pts"]).model_dump(
        mode="json"
    )
    query["teammate_id"] = pair["teammate_id"]
    if explicit_dates:
        query.update(start="2025-10-22", end="2025-12-31")
    planner.result = dict(
        kind="study", pair_id=pair["pair_id"], message="", query=query
    )
    question = "How did LeBron play without Luka?"
    if explicit_dates:
        question += " From 2025-10-22 through 2025-12-31"
    answer = client.post("/api/agent/ask", json={"question": question}).json()
    if explicit_dates:
        assert answer["research_scope"]["phase"] == "Both"
        assert answer["tables"]
    else:
        assert not answer["tables"]
        assert "full-season comparison is not available" in answer["answer"]


@pytest.mark.parametrize("arm", [None, "participated", "reported_out_no_appearance"])
def test_full_season_answer_discloses_playoffs_and_preserves_explicit_phase(
    research_client, monkeypatch, arm
):
    from tests.test_research_insights import presentation_study

    client, _, planner = research_client
    study = presentation_study()
    study["scope"].update(
        phase="Both", window="full_season", start="2025-10-21", end="2026-06-13"
    )
    monkeypatch.setattr(research_ask, "catalog", lambda path: [study])
    query = ResearchQuery(player_ids=[study["player_id"]], metrics=["pts"]).model_dump(
        mode="json"
    )
    query["teammate_id"] = study["teammate_id"]
    query["teammate_status"] = arm
    planner.result = dict(
        kind="study", pair_id=study["pair_id"], message="", query=query
    )
    answer = client.post(
        "/api/agent/ask", json={"question": "How did LeBron play without Luka?"}
    ).json()
    assert "regular season and playoffs" in answer["answer"]
    assert "2026-06-13" in answer["answer"]
    assert answer["research_scope"]["phase"] == "Both"
    answer = client.post(
        "/api/agent/ask",
        json={"question": "How did LeBron play without Luka in the regular season?"},
    ).json()
    assert not answer["tables"]
    assert "requested season phase" in answer["answer"]
