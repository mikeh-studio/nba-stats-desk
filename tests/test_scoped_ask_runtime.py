"""Lifespan and actual JSON/SSE routes, with offline data and SDK HTTP."""

import json
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from app.agent import intelligence as host
from app.agent import intelligence_contract as api
from app.agent.runtime_policy import configure_runtime_policy
from app.repository import BigQueryWarehouseRepository
from fastapi.testclient import TestClient
from tests.test_reference_intelligence import source
from tests.test_runtime_policy import Authority, policy, response, sdk
from tests.test_semantic_migration import settings


class Provider:
    descriptor = api.ProviderDescriptor(
        "synthetic", "1", api.API_VERSION, ("nba_semantics/0.1",), ("metrics",), True
    )

    def prompts(self):
        return api.PromptBundle("synthetic/1", "Synthetic fixture")

    def classify(self, context):
        return api.IntentSelection("metrics", False)

    async def plan(self, context, intent, *, prompts, budget, model):
        text = await model.generate_plan(
            system_prompt=prompts.planning_system, user_payload="{}"
        )
        json.loads(text)
        return api.QueryPlan(
            "metrics",
            "query",
            (
                api.QueryProposal(
                    "pts",
                    api.Scope("2024-25", "Regular Season"),
                    "summary",
                    "average",
                    player_name="Alex Example",
                    aggregation_source="explicit",
                ),
            ),
        )


@pytest.fixture
def installed(monkeypatch):
    import app.main as main

    previous = dict(main.app.dependency_overrides)
    authority = Authority(maximum=2)
    runtime = policy(authority)
    config = replace(
        settings(),
        season="2026-27",
        ask_intelligence_provider="synthetic",
        performance_cache_prewarm_enabled=True,
    )
    repo = BigQueryWarehouseRepository(config, client=SimpleNamespace())
    repo._governed_warehouse = source()
    sent = []
    scenario = {"status": 200, "text": "{}"}

    def http(request):
        assert any(event[0] == "reserve" for event in authority.events)
        sent.append(json.loads(request.content))
        if scenario["status"] == "timeout":
            raise httpx.ReadTimeout("synthetic", request=request)
        body = (
            response(scenario["text"])
            if scenario["status"] == 200
            else {"error": {"message": "synthetic"}}
        )
        return httpx.Response(scenario["status"], json=body)

    model = sdk(http)
    monkeypatch.setattr(host, "_load", lambda name: Provider())
    monkeypatch.setattr(
        main,
        "_prewarm_performance_flow",
        lambda repo: pytest.fail("prewarm must not run"),
    )
    main.app.dependency_overrides.update(
        {
            main.get_settings: lambda: config,
            main.get_repository: lambda: repo,
            main.get_agent_client: lambda: model,
        }
    )
    configure_runtime_policy(runtime)
    try:
        with TestClient(main.app) as client:
            yield client, authority, sent, scenario, main
    finally:
        configure_runtime_policy(None)
        main.app.dependency_overrides.clear()
        main.app.dependency_overrides.update(previous)
        model.close()


QUESTION = "Alex Example points per game in the 2024-25 regular season"


def test_json_and_sse_share_real_worker_wire_budget_and_historical_scope(installed):
    client, authority, sent, _, _ = installed
    normal = client.post("/api/agent/ask", json={"question": QUESTION})
    stream = client.post("/api/agent/ask/stream", json={"question": QUESTION})
    assert normal.status_code == stream.status_code == 200, normal.text
    assert normal.json()["status"] == "ok", normal.text
    final = next(
        json.loads(line[6:])["payload"]
        for line in stream.text.splitlines()
        if line.startswith("data: ") and json.loads(line[6:]).get("type") == "final"
    )
    assert final["status"] == "ok"
    assert normal.json()["season"] == final["season"] == "2026-27"
    assert normal.json()["query_plan"]["queries"][0]["season"] == "2024-25"
    assert len(sent) == authority.attempts == 2
    exhausted = client.post("/api/agent/ask", json={"question": QUESTION})
    assert exhausted.status_code == 502
    assert len(sent) == 2
    assert [
        event[1]["outcome"] for event in authority.events if event[0] == "finish"
    ] == ["success", "success", "error"]


@pytest.mark.parametrize(
    "status,text", [(429, "{}"), (503, "{}"), ("timeout", "{}"), (200, "malformed")]
)
@pytest.mark.parametrize("streaming", [False, True])
def test_terminal_errors_consume_attempt_without_retry(
    installed, status, text, streaming
):
    client, authority, sent, scenario, _ = installed
    scenario.update(status=status, text=text)
    result = client.post(
        "/api/agent/ask" + ("/stream" if streaming else ""), json={"question": QUESTION}
    )
    if streaming:
        assert '"type": "error"' in result.text or '"type":"error"' in result.text
        assert '"type": "final"' not in result.text
    else:
        assert result.status_code == 502
    assert len(sent) == authority.attempts == 1
    assert authority.events[-1] == ("finish", {"outcome": "error"})
    assert "malformed" not in result.text


@pytest.mark.parametrize(
    "path",
    [
        "/api/health",
        "/api/players/search?q=Alex",
        "/api/agent/history",
        "/api/research/studies",
        "/api/performance/initial",
        "/api/unknown",
    ],
)
def test_deny_background_routes_before_repository_dependency(installed, path):
    client, authority, sent, _, main = installed
    main.app.dependency_overrides[main.get_repository] = lambda: pytest.fail(
        "denied request created repository"
    )
    assert client.get(path).status_code == 403
    assert sent == [] and authority.events == []


@pytest.mark.parametrize(
    "extra",
    [
        {"provider": "claude"},
        {"provider": "openrouter"},
        {"model": "gpt-5.6-sol"},
        {"selected_player_id": 1, "selected_player_name": "Alex Example"},
    ],
)
def test_http_cannot_select_alternate_runtime_or_identity(installed, extra):
    client, authority, sent, _, _ = installed
    assert (
        client.post("/api/agent/ask", json={"question": QUESTION, **extra}).status_code
        == 403
    )
    assert sent == [] and authority.events == []


def test_shell_keeps_default_season_and_prewarm_is_disabled(installed):
    client, authority, sent, _, _ = installed
    shell = client.get("/ask")
    assert shell.status_code == 200 and "2026" in shell.text
    assert sent == [] and authority.events == []


@pytest.mark.parametrize("streaming", [False, True])
def test_cancel_or_disconnect_closes_admitted_worker_before_send(
    installed, monkeypatch, streaming
):
    import asyncio
    from threading import Event

    from tests.test_agent_capacity_endpoints import ASGIRequest

    _, authority, sent, _, main = installed
    started, release, finished = Event(), Event(), Event()
    build = main._build_stats_agent
    finish = authority.finish

    def held_build(*args):
        started.set()
        assert release.wait(5)
        return build(*args)

    def terminal(*args, **kwargs):
        finish(*args, **kwargs)
        finished.set()

    monkeypatch.setattr(main, "_build_stats_agent", held_build)
    monkeypatch.setattr(authority, "finish", terminal)

    async def run():
        request = ASGIRequest("/api/agent/ask" + ("/stream" if streaming else ""))
        request.payload = {"question": QUESTION}
        task = asyncio.create_task(request.run())
        assert await asyncio.to_thread(started.wait, 5)
        if streaming:
            await request.meta_sent.wait()
            await request.disconnect()
            await asyncio.wait_for(task, 5)
        else:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        release.set()
        assert await asyncio.to_thread(finished.wait, 5)

    try:
        asyncio.run(run())
    finally:
        release.set()
    assert sent == [] and authority.attempts == 0
    assert authority.events[-1] == ("finish", {"outcome": "cancelled"})


def test_worker_start_failure_finishes_durable_admission(installed, monkeypatch):
    client, authority, sent, _, main = installed

    def fail(*args):
        raise RuntimeError("synthetic")

    monkeypatch.setattr(main, "Thread", lambda **kwargs: SimpleNamespace(start=fail))
    result = client.post("/api/agent/ask", json={"question": QUESTION})
    assert result.status_code == 503
    assert sent == []
    assert authority.events[-1] == ("finish", {"outcome": "admission_error"})


def test_scoped_source_and_real_routes_deny_wrong_scope_before_model(monkeypatch):
    import app.main as main
    from tests.test_scoped_source_policy import CapturingClient, regular_rows
    from tests.test_scoped_source_policy import policy as source_policy

    previous = dict(main.app.dependency_overrides)
    config = replace(
        settings(), season="2026-27", ask_intelligence_provider="synthetic"
    )
    rows = regular_rows()
    for row in rows:
        row["player_name"] = {1: "Alex Example", 2: "Blair Sample", 3: "Casey Orbit"}[
            row["player_id"]
        ]
    data = CapturingClient(rows=rows)
    repo = BigQueryWarehouseRepository(config, client=data)
    sent = []
    model = sdk(
        lambda request: sent.append(request) or httpx.Response(200, json=response())
    )
    runtime = policy(source_policy=source_policy())
    monkeypatch.setattr(host, "_load", lambda name: Provider())
    main.app.dependency_overrides.update(
        {
            main.get_settings: lambda: config,
            main.get_repository: lambda: repo,
            main.get_agent_client: lambda: model,
        }
    )
    configure_runtime_policy(runtime)
    try:
        with TestClient(main.app) as client:
            denied = client.post(
                "/api/agent/ask",
                json={
                    "question": "Alex Example points per game in the 2023-24 regular season"
                },
            )
            assert denied.status_code == 200
            assert denied.json()["status"] != "ok"
            assert sent == [] and data.calls == []
            good = client.post("/api/agent/ask", json={"question": QUESTION})
            assert good.status_code == 200 and good.json()["status"] == "ok", good.text
            assert len(sent) == 1 and len(data.calls) == 1
            assert good.json()["query_plan"]["queries"][0]["season"] == "2024-25"
    finally:
        configure_runtime_policy(None)
        main.app.dependency_overrides.clear()
        main.app.dependency_overrides.update(previous)
        model.close()


def test_deterministic_bypass_admits_case_but_consumes_no_model_attempt(installed):
    client, authority, sent, _, _ = installed
    result = client.post(
        "/api/agent/ask",
        json={
            "question": "Who led the league in points in the 2024-25 regular season?"
        },
    )
    assert result.status_code == 200
    assert result.json()["status"] == "ok"
    assert sent == [] and authority.attempts == 0
    assert [event[0] for event in authority.events] == ["admit", "finish"]


def test_missing_openai_auth_fails_lifespan_without_probe(monkeypatch):
    import app.main as main

    previous = dict(main.app.dependency_overrides)
    config = replace(
        settings(),
        ask_intelligence_provider="synthetic",
        openai_api_key=None,
        anthropic_api_key="synthetic-other-provider",
    )
    monkeypatch.setattr(host, "_load", lambda name: Provider())
    main.app.dependency_overrides[main.get_settings] = lambda: config
    main.app.dependency_overrides[main.get_repository] = lambda: pytest.fail(
        "startup touched repository"
    )
    runtime = policy()
    configure_runtime_policy(runtime)
    try:
        with pytest.raises(host.ProviderConfigurationError):
            with TestClient(main.app):
                pytest.fail("missing authentication accepted")
        assert runtime.authority.events == []
    finally:
        configure_runtime_policy(None)
        main.app.dependency_overrides.clear()
        main.app.dependency_overrides.update(previous)


def test_missing_source_policy_denies_production_repository_before_creation(
    installed, monkeypatch
):
    client, authority, sent, _, main = installed
    main.app.dependency_overrides.pop(main.get_repository)
    monkeypatch.setattr(
        main,
        "_cached_repository",
        lambda settings: pytest.fail("unscoped repository constructed"),
    )
    result = client.post("/api/agent/ask", json={"question": QUESTION})
    assert result.status_code == 403
    assert sent == []
    assert authority.events[-1] == ("finish", {"outcome": "admission_error"})


@pytest.mark.parametrize("streaming", [False, True])
def test_denied_trusted_admission_precedes_repository_and_client_dependencies(
    installed, monkeypatch, streaming
):
    client, authority, sent, _, main = installed

    def denied(**kwargs):
        raise ValueError("synthetic refusal")

    monkeypatch.setattr(authority, "admit", denied)
    main.app.dependency_overrides[main.get_repository] = lambda: pytest.fail(
        "denied admission constructed repository"
    )
    main.app.dependency_overrides[main.get_agent_client] = lambda: pytest.fail(
        "denied admission constructed client"
    )
    result = client.post(
        "/api/agent/ask" + ("/stream" if streaming else ""), json={"question": QUESTION}
    )
    assert result.status_code == 403
    assert sent == [] and authority.events == []


def test_runtime_never_reads_or_restores_previous_conversation(installed, monkeypatch):
    client, authority, sent, _, main = installed
    monkeypatch.setattr(
        main,
        "_season_conversation_store",
        lambda *a: pytest.fail("strict profile read conversation state"),
    )
    monkeypatch.setattr(
        main,
        "_restore_agent_context",
        lambda *a: pytest.fail("strict profile restored conversation state"),
    )
    result = client.post(
        "/api/agent/ask",
        json={"question": QUESTION, "conversation_id": "existing-chat"},
    )
    assert result.status_code == 200 and result.json()["status"] == "ok"
    assert len(sent) == authority.attempts == 1


@pytest.mark.parametrize("streaming", [False, True])
def test_position_context_is_refused_before_durable_admission(installed, streaming):
    client, authority, sent, _, main = installed
    config = main.app.dependency_overrides[main.get_settings]()
    main.app.dependency_overrides[main.get_settings] = lambda: replace(
        config, agent_position_context_path="synthetic-position-context.csv"
    )
    result = client.post(
        "/api/agent/ask" + ("/stream" if streaming else ""),
        json={"question": QUESTION},
    )
    assert result.status_code == 403
    assert authority.events == []
    assert sent == []
