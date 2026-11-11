"""Validated prior aggregation across reference plans and actual Ask endpoints."""

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from app import main
from app.agent.conversation import InMemoryConversationStore
from app.agent.followup import analysis_context
from app.agent.intelligence import planning_context
from app.agent.service import StatsAgent
from fastapi.testclient import TestClient
from scripts.ask_eval.fixtures import frozen_repository
from tests.test_reference_intelligence import config

SOURCE = json.loads((Path(__file__).parent / "fixtures/ask/source.json").read_text())


def settings(**changes):
    overrides = dict(
        season="2024-25",
        agent_history_enabled=False,
        agent_rate_limit_per_minute=0,
        agent_rate_limit_daily=0,
        performance_cache_prewarm_enabled=False,
    )
    overrides.update(changes)
    return replace(config(), **overrides)


def endpoint_client(monkeypatch, current_settings):
    store = {"value": InMemoryConversationStore()}
    monkeypatch.setattr(
        main, "_season_conversation_store", lambda season: store["value"]
    )
    repo = frozen_repository(current_settings, SOURCE, "complete")
    previous = dict(main.app.dependency_overrides)
    main.app.dependency_overrides.update(
        {
            main.get_settings: lambda: current_settings,
            main.get_repository: lambda: repo,
            main.get_agent_client: lambda: None,
        }
    )
    return (
        TestClient(
            main.app,
            base_url="http://127.0.0.1",
            client=("127.0.0.1", 50000),
        ),
        store,
        previous,
    )


def post(client, transport, question, conversation_id, **extra):
    response = client.post(
        "/api/agent/ask"
        + ("/stream" if transport == "sse" else "")
        + "?season=2024-25",
        json={"question": question, "conversation_id": conversation_id, **extra},
    )
    assert response.status_code == 200, response.text
    if transport == "json":
        return response.json()
    events = [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    return next(e["payload"] for e in events if e.get("type") == "final")


@pytest.mark.parametrize("transport", ["json", "sse"])
@pytest.mark.parametrize(
    "first,first_value,follow_value,aggregation",
    [
        ("Avery Example total points in 2024-25", 30, 15, "total"),
        ("Avery Example total turnovers in 2024-25", 12, 15, "total"),
        ("Avery Example points per game in 2024-25", 5, 2.5, "average"),
    ],
)
def test_metric_followup_inherits_validated_aggregation(
    monkeypatch, transport, first, first_value, follow_value, aggregation
):
    client, _, previous = endpoint_client(monkeypatch, settings())
    try:
        with client:
            first_payload = post(client, transport, first, "aggregation-chat")
            second = post(client, transport, "What about assists?", "aggregation-chat")
        assert first_payload["status"] == second["status"] == "ok"
        assert first_payload["semantic_evidence"]["rows"][0]["value"] == first_value
        assert second["semantic_evidence"]["rows"][0]["value"] == follow_value
        assert second["query_plan"]["queries"][0]["aggregation"] == aggregation
        assert (
            second["query_plan"]["aggregation_resolution"][0]["source"] == "inherited"
        )
        scope = second["semantic_evidence"]["scope"]
        assert (scope["player_id"], scope["season"], scope["season_type"]) == (
            101,
            "2024-25",
            "Regular Season",
        )
        assert second["semantic_evidence"]["rows"][0]["valid_games"] == 6
    finally:
        main.app.dependency_overrides.clear()
        main.app.dependency_overrides.update(previous)


@pytest.mark.parametrize("transport", ["json", "sse"])
def test_explicit_new_aggregation_and_ratio_convention(monkeypatch, transport):
    client, _, previous = endpoint_client(monkeypatch, settings())
    try:
        with client:
            first = post(
                client, transport, "Avery Example total points in 2024-25", "change"
            )
            average = post(client, transport, "What about assists per game?", "change")
            rebounds = post(
                client, transport, "What about total rebounds per game?", "change"
            )
            ratio = post(client, transport, "What about FG%?", "change")
            conflict = post(
                client, transport, "What about total assists per game?", "change"
            )
        assert first["status"] == average["status"] == ratio["status"] == "ok"
        assert average["semantic_evidence"]["rows"][0]["value"] == 2.5
        assert average["query_plan"]["queries"][0]["aggregation"] == "average"
        assert rebounds["status"] == "ok"
        assert rebounds["query_plan"]["queries"][0]["aggregation"] == "average"
        assert ratio["semantic_evidence"]["rows"][0]["value"] == 0.1
        assert ratio["query_plan"]["queries"][0]["aggregation"] == "ratio"
        assert (
            ratio["query_plan"]["aggregation_resolution"][0]["source"]
            == "metric_convention"
        )
        assert conflict["status"] == "clarification_required"
        assert not conflict["tables"]
    finally:
        main.app.dependency_overrides.clear()
        main.app.dependency_overrides.update(previous)


def test_validated_summary_requires_one_executed_matching_summary():
    cfg = settings()
    repo = frozen_repository(cfg, SOURCE, "complete")
    agent = StatsAgent(
        cfg,
        SimpleNamespace(),
        semantic_warehouse=repo._governed_warehouse,
        conversation_store=InMemoryConversationStore(),
    )
    payload = agent.answer("Avery Example total points in 2024-25")
    context = analysis_context("Avery Example total points in 2024-25", payload)
    assert context["validated_summary"]["aggregation"] == "total"
    assert context["validated_summary"]["aggregation_source"] == "explicit"
    players = [{"player_id": 101, "player_name": "Avery Example"}]
    prior = planning_context(
        "What about assists?", "2024-25", players, [], context
    ).prior
    assert prior.aggregation == "total"
    assert prior.aggregation_source == "explicit"
    assert prior.scopes[0].as_of == "2025-04-15"
    for mutation in (
        lambda p: p["query_plan"]["queries"].append(
            dict(p["query_plan"]["queries"][0])
        ),
        lambda p: p["query_plan"]["queries"][0].update(operation="rank"),
        lambda p: p["query_plan"]["queries"][0].update(aggregation=[]),
        lambda p: p["query_plan"]["aggregation_resolution"][0].update(source=[]),
        lambda p: p["semantic_evidence"]["scope"].update(player_id=202),
    ):
        changed = json.loads(json.dumps(payload))
        mutation(changed)
        assert "validated_summary" not in analysis_context("prior", changed)
    legacy = dict(context, validated_summary={"aggregation": "total"})
    assert (
        planning_context(
            "What about assists?", "2024-25", players, [], legacy
        ).prior.aggregation
        is None
    )
    recovered = dict(context, browser_recovered=True)
    assert (
        planning_context(
            "What about assists?", "2024-25", players, [], recovered
        ).prior.aggregation
        is None
    )
    malformed = dict(
        context,
        validated_summary=dict(context["validated_summary"], aggregation=[]),
    )
    assert (
        planning_context(
            "What about assists?", "2024-25", players, [], malformed
        ).prior.aggregation
        is None
    )
    for field, value in (
        ("window", "last_n_games"),
        ("start_date", "2025-04-10"),
        ("as_of", "2025-04-14"),
        ("home_away", "HOME"),
        ("team_abbr", "BBB"),
    ):
        changed_scope = dict(context["scope"], **{field: value})
        altered = dict(context, scope=changed_scope)
        assert (
            planning_context(
                "What about assists?", "2024-25", players, [], altered
            ).prior.aggregation
            is None
        )


def test_validated_prior_keeps_window_dates_and_filters():
    cfg = settings()
    repo = frozen_repository(cfg, SOURCE, "complete")
    agent = StatsAgent(
        cfg,
        SimpleNamespace(),
        semantic_warehouse=repo._governed_warehouse,
        conversation_store=InMemoryConversationStore(),
    )
    payload = agent.answer("Avery Example total points in 2024-25")
    payload.pop("conversation_context", None)
    query = payload["query_plan"]["queries"][0]
    scope = payload["semantic_evidence"]["scope"]
    filters = dict(
        window="date_range",
        start_date="2025-04-11",
        as_of="2025-04-13",
        team_abbr="AAA",
        opponent_abbr="BBB",
        home_away="HOME",
    )
    query.update(filters)
    scope.update(filters)
    prior_context = analysis_context("prior", payload)
    prior = planning_context(
        "What about assists?",
        "2024-25",
        [{"player_id": 101, "player_name": "Avery Example"}],
        ["AAA", "BBB"],
        prior_context,
    ).prior
    assert prior.aggregation == "total"
    assert prior.scopes[0].window == "date_range"
    assert (prior.scopes[0].start_date, prior.scopes[0].as_of) == (
        "2025-04-11",
        "2025-04-13",
    )
    assert (
        prior.scopes[0].team_abbr,
        prior.scopes[0].opponent_abbr,
        prior.scopes[0].home_away,
    ) == ("AAA", "BBB", "HOME")


@pytest.mark.parametrize("transport", ["json", "sse"])
def test_history_rebuild_preserves_validated_total_and_browser_hint_does_not(
    monkeypatch, tmp_path, transport
):
    cfg = settings(
        agent_history_enabled=True, agent_history_path=str(tmp_path / "history.jsonl")
    )
    client, store, previous = endpoint_client(monkeypatch, cfg)
    try:
        with client:
            first = post(
                client, transport, "Avery Example total points in 2024-25", "history"
            )
            store["value"] = InMemoryConversationStore()
            from app.seasons import settings_for_season

            history_path = settings_for_season(cfg, "2024-25").agent_history_path
            stored = main.read_history(history_path, conversation_id="history")
            assert (
                stored["conversations"][0]["turns"][0]["payload"]["request_id"]
                == first["request_id"]
            )
            restored = post(client, transport, "What about assists?", "history")
            store["value"] = InMemoryConversationStore()
            browser = post(
                client,
                transport,
                "What about assists?",
                "browser",
                previous_context=first["conversation_context"],
            )
        assert restored["status"] == "ok"
        assert restored["semantic_evidence"]["rows"][0]["value"] == 15
        assert restored["query_plan"]["queries"][0]["aggregation"] == "total"
        assert browser["status"] in {"clarification_required", "unsupported"}
        assert not browser["tables"]
    finally:
        main.app.dependency_overrides.clear()
        main.app.dependency_overrides.update(previous)


@pytest.mark.parametrize(
    "change,expected",
    [
        ({"min_games": 7}, "ok"),
        ({"direction": "lower"}, "clarification_required"),
        ({"limit": 3}, "clarification_required"),
    ],
)
def test_metric_followup_keeps_qualifications_or_requests_clarification(
    change, expected
):
    from dataclasses import asdict

    from app.agent.intelligence import plan_question
    from app.agent.plan_execution import execute_plan
    from app.agent.semantics import Query, run_query

    cfg = settings()
    repo = frozen_repository(cfg, SOURCE, "complete")
    evidence = repo._governed_warehouse.load(["2024-25"])[1]
    prior_query = Query("pts", "2024-25", "total", player_id=101, **change)
    players = [{"player_name": "Avery Example", "player_id": 101}]
    prior_payload = {
        "status": "ok",
        "player_profile": {"player": players[0]},
        "query_plan": {
            "route": "governed_metrics",
            "queries": [asdict(prior_query)],
            "aggregation_resolution": [
                {"source": "explicit", "chosen_aggregation": "total"}
            ],
        },
        "semantic_evidence": run_query(evidence, prior_query),
    }
    previous = analysis_context("original points total", prior_payload)
    assert previous["validated_summary"]["aggregation"] == "total"
    proposal = plan_question(
        None,
        settings=cfg,
        model="",
        question="What about assists?",
        selected_season="2024-25",
        players=players,
        conversation_context=previous,
    )
    answer = execute_plan(proposal, evidence, players)
    assert answer["status"] == expected
    if expected == "ok":
        row = answer["evidence"]["rows"][0]
        assert row["value"] == 15 and row["rank"] is None
        assert row["exclusion_reasons"] == ["insufficient_games"]


@pytest.mark.parametrize(
    "question, key, mode, proposed_date",
    [
        ("What about assists per game?", "ast", "average", None),
        ("What about assists per game?", "ast", "average", "2025-04-14"),
        ("What about free throw percentage?", "ft_pct", "ratio", "2025-04-14"),
    ],
)
def test_metric_switch_inherits_only_an_omitted_cutoff(
    monkeypatch, question, key, mode, proposed_date
):
    from app.agent import intelligence as host
    from tests.test_intelligence import FixtureProvider, plan, query

    client, _, previous = endpoint_client(monkeypatch, settings())
    try:
        with client:
            first = post(
                client, "json", "Avery Example total points in 2024-25", "cutoff"
            )
            assert first["semantic_evidence"]["scope"]["as_of"] == "2025-04-15"
            proposed = query(
                metric=key,
                aggregation=mode,
                player_name="Avery Example",
                scope=host.api.Scope("2024-25", "Regular Season", as_of=proposed_date),
            )
            monkeypatch.setattr(
                host, "get_provider", lambda _: FixtureProvider(plan(proposed))
            )
            second = post(client, "json", question, "cutoff")
        if proposed_date is None:
            assert second["status"] == "ok"
            assert second["semantic_evidence"]["scope"]["as_of"] == "2025-04-15"
            assert second["semantic_evidence"]["rows"][0]["value"] == 2.5
        else:
            assert second["status"] == "clarification_required"
            assert not second["tables"]
    finally:
        main.app.dependency_overrides.clear()
        main.app.dependency_overrides.update(previous)


def test_explicit_parenthesized_playoffs_keeps_the_phase(monkeypatch):
    client, _, previous = endpoint_client(monkeypatch, settings())
    try:
        with client:
            first = post(
                client,
                "json",
                "Avery Example points per game in the 2024-25 playoffs",
                "phase",
            )
            second = post(
                client,
                "json",
                "Avery Example points per game in 2024-25 (playoffs)",
                "phase",
            )
        assert first["status"] == second["status"] == "ok"
        assert second["semantic_evidence"]["scope"]["season_type"] == "Playoffs"
        assert second["semantic_evidence"]["rows"] == first["semantic_evidence"]["rows"]
    finally:
        main.app.dependency_overrides.clear()
        main.app.dependency_overrides.update(previous)
