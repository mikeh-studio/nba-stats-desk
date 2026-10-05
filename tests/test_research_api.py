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
    profile = payload["player_profile"]
    assert profile["player"]["player_id"] == pair["player_id"]
    assert profile["player"]["player_name"] == pair["player_name"]
    assert profile["player"]["headshot_url"].endswith(f"/{pair['player_id']}.png")
    assert profile["profile_url"] == f"/players/{pair['player_id']}"
    assert profile["player"]["team_abbr"] is None
    assert profile["scopeLabel"] == (
        "2025-26 regular season and playoffs · 2025-11-01 through 2025-11-30"
    )
    assert "significance" not in payload["research_highlights"][0]
    assert "research_assessments" not in payload
    assert "study" not in payload
    assert payload["research_highlights"][1]["estimate"] is None
    assert "4.0 more points" in payload["answer"]
    assert "insufficient evidence" not in json.dumps(payload).lower()
    stream = client.post("/api/agent/ask/stream", json=request)
    assert stream.status_code == 200
    events = [
        json.loads(line[6:])
        for line in stream.text.splitlines()
        if line.startswith("data: ")
    ]
    final = next(e["payload"] for e in events if e.get("type") == "final")
    assert final["player_profile"] == profile
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
        assert not answer.get("player_profile")
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


@pytest.mark.parametrize("stream", [False, True])
def test_missing_named_study_stops_before_warehouse_and_model(
    research_client, monkeypatch, stream
):
    from app.research_studies import PAIRS, pending

    client, _, planner = research_client
    monkeypatch.setattr(research_ask, "catalog", lambda path: [pending(PAIRS[0])])

    def unexpected_load(*args):
        pytest.fail("Missing study should not query the warehouse")

    monkeypatch.setattr(research_ask, "load_research_evidence", unexpected_load)
    response = client.post(
        "/api/agent/ask" + ("/stream" if stream else ""),
        json={"question": "Tell me how LeBron James played while Luka was out"},
    )
    assert response.status_code == 200
    if stream:
        events = [
            json.loads(line[6:])
            for line in response.text.splitlines()
            if line.startswith("data: ")
        ]
        payload = next(e["payload"] for e in events if e.get("type") == "final")
    else:
        payload = response.json()
    assert payload["research_error_code"] == "study_coverage_missing"
    assert "LeBron James / Luka Doncic" in payload["answer"]
    assert "built or reconnected" in payload["answer"]
    assert not payload["tables"] and not payload.get("player_profile")
    assert planner.calls == 0


@pytest.mark.parametrize("failure", [FileNotFoundError, ValueError, PermissionError])
def test_broken_study_configuration_is_specific_and_private(
    research_client, monkeypatch, failure
):
    client, _, planner = research_client

    def broken_catalog(path):
        raise failure("private-artifact-location-and-content")

    monkeypatch.setattr(research_ask, "catalog", broken_catalog)
    payload = client.post(
        "/api/agent/ask", json={"question": "LeBron without Luka"}
    ).json()
    assert payload["research_error_code"] == "study_catalog_unavailable"
    assert "configuration needs repair" in payload["answer"]
    assert "private-artifact" not in json.dumps(payload)
    assert planner.calls == 0


@pytest.mark.parametrize("metric", ["pts_per36", "ast_per36"])
def test_plan_schema_binds_metrics_to_route_and_rejects_invalid_model_output(
    research_client, monkeypatch, metric
):
    import jsonschema
    from app.research_studies import PAIRS, pending

    client, _, planner = research_client
    study = pending(PAIRS[0])
    study["scope"] = dict(
        season="2025-26",
        phase="Both",
        window="full_season",
        start="2025-10-21",
        end="2026-06-13",
    )
    monkeypatch.setattr(research_ask, "catalog", lambda path: [study])
    query = ResearchQuery(player_ids=[2544], metrics=[metric]).model_dump(mode="json")
    query["teammate_id"] = 1629029
    planner.result = dict(kind="study", query=query, pair_id="lebron-luka", message="")
    schema = research_ask.research_plan_schema()
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"request": planner.result}, schema)
    jsonschema.validate({"request": {**planner.result, "kind": "breakdown"}}, schema)
    # Even a provider that ignores the output schema cannot reach the presenter.
    payload = client.post(
        "/api/agent/ask", json={"question": "LeBron without Luka"}
    ).json()
    assert payload["research_error_code"] == "invalid_research_plan"
    assert "per-36" in payload["answer"]
    assert not payload["tables"]


def test_missing_study_after_planning_is_not_a_table_of_unavailable_values(
    research_client, monkeypatch
):
    from app.research_studies import PAIRS, pending

    client, _, planner = research_client
    monkeypatch.setattr(research_ask, "catalog", lambda path: [pending(PAIRS[0])])
    query = ResearchQuery(player_ids=[2544], metrics=["pts"]).model_dump(mode="json")
    query["teammate_id"] = 1629029
    planner.result = dict(kind="study", query=query, pair_id="lebron-luka", message="")
    payload = client.post(
        "/api/agent/ask", json={"question": "Research the teammate comparison"}
    ).json()
    assert payload["research_error_code"] == "study_coverage_missing"
    assert not payload["tables"]
    assert planner.calls == 1


def test_missing_study_trace_is_classified_without_sensitive_content(
    research_client, monkeypatch
):
    from types import SimpleNamespace

    from app.agent.observability import AgentTrace
    from app.research_studies import PAIRS, pending
    from tests.test_agent_service import _settings

    trace = AgentTrace("fixture", "private-question", "fixture-model")
    monkeypatch.setattr(research_ask, "catalog", lambda path: [pending(PAIRS[0])])
    agent = SimpleNamespace(settings=_settings(), conversation_store=None)
    payload = research_ask.answer_research(
        agent, "LeBron without Luka", "openai", "fixture-model", trace=trace
    )
    assert trace.route == "research"
    assert trace.outcome == "unsupported"
    assert (
        trace.error_type == payload["research_error_code"] == "study_coverage_missing"
    )
    assert trace.total_tokens == 0


@pytest.mark.parametrize(
    "question",
    [
        "Tell me how LeBron James played while Luka was out",
        "How did LeBron play without Luka?",
        "How did Jalen Johnson perform when Trae Young was absent?",
        "How does Brunson play without Hart?",
    ],
)
def test_direct_study_plan_recognizes_complete_questions(question):
    plan = research_ask.direct_study_plan(question, "2025-26")
    assert plan["kind"] == "study"
    assert plan["query"]["phase"] == "Both"
    assert plan["query"]["start"] is None
    assert len(plan["query"]["metrics"]) == 9


@pytest.mark.parametrize(
    "suffix",
    [
        " in 2024-25",
        " in the playoffs",
        " at home",
        " over the last five games",
        " from 2025-11-01 to 2025-12-01",
        " using points per 36",
        " and compare Austin Reaves",
    ],
)
def test_direct_study_plan_never_drops_extra_scope(suffix):
    assert (
        research_ask.direct_study_plan(
            "Tell me how LeBron James played while Luka was out" + suffix, "2025-26"
        )
        is None
    )
    assert (
        research_ask.direct_study_plan("How did Luka play without LeBron?", "2025-26")
        is None
    )


def test_exact_absence_question_json_sse_uses_catalog_without_model_or_warehouse(
    research_client, monkeypatch
):
    from app.research_studies import PAIRS, pending
    from tests.test_research_insights import metric

    client, _, planner = research_client
    study = pending(PAIRS[0])
    study["scope"] = dict(
        season="2025-26",
        start="2025-10-21",
        end="2026-05-11",
        phase="Both",
        window="full_season",
    )
    study["metrics"][0] = metric()
    monkeypatch.setattr(research_ask, "catalog", lambda path: [study])

    def no_warehouse(*args):
        pytest.fail("A published study should not require warehouse access")

    monkeypatch.setattr(research_ask, "load_research_evidence", no_warehouse)
    request = dict(question="Tell me how LeBron James played while Luka was out")
    response = client.post("/api/agent/ask", json=request)
    assert response.status_code == 200
    payload = response.json()
    assert payload["tables"]
    assert payload["research_scope"]["pair_id"] == "lebron-luka"
    assert payload["research_scope"]["end"] == "2026-05-11"
    stream = client.post("/api/agent/ask/stream", json=request)
    assert stream.status_code == 200
    events = [
        json.loads(line[6:])
        for line in stream.text.splitlines()
        if line.startswith("data: ")
    ]
    assert any(
        e.get("tables") == payload["tables"]
        or e.get("payload", {}).get("tables") == payload["tables"]
        for e in events
    )
    assert planner.calls == 0
