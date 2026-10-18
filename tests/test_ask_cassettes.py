"""Recording mechanics use synthetic SDK-shaped outputs, never claim live capture."""

import copy
import subprocess
import sys
from datetime import date
from types import SimpleNamespace

import pytest
from app.agent.clock import semantic_date, today
from scripts.ask_eval.cassettes import (
    CassetteClient,
    CassetteError,
    CassetteStore,
    RecordedProviderTimeout,
    call_context,
    encoded,
    response_data,
)

BINDING = dict(
    provider="openai",
    model="test-model",
    clock="2026-10-18",
    cases={"example": "case-hash"},
    source_sha256="source",
    schema_sha256="schema",
    corpus_sha256="corpus",
    adapter_revision="revision",
)
REQUEST = dict(
    model="test-model",
    instructions="Use the exact evidence",
    input=[{"role": "user", "content": "Avery 1/10"}],
    text={"format": {"name": "plan", "schema": {"type": "object"}}},
)


class Provider:
    def __init__(self, options=None, failure=False):
        self.responses = self
        self.options = options if options is not None else []
        self.failure = failure

    def with_options(self, **kwargs):
        self.options.append(kwargs)
        return Provider(self.options, self.failure)

    def create(self, **kwargs):
        callback = kwargs.get("on_output_text_delta")
        if callback:
            callback("first")
            callback("second")
        if self.failure:
            raise TimeoutError("private error text")
        return SimpleNamespace(
            output_text="answer",
            id="resp-id",
            status="completed",
            output=[
                SimpleNamespace(
                    type="function_call",
                    call_id="call-id",
                    name="tool",
                    arguments='{"a":1}',
                ),
                SimpleNamespace(
                    type="message",
                    content=[SimpleNamespace(type="refusal", refusal="No")],
                ),
            ],
            usage=SimpleNamespace(input_tokens=3, output_tokens=2, total_tokens=5),
        )


@pytest.fixture(autouse=True)
def context():
    token = call_context.set(
        dict(case="example", turn=0, transport="json", settings_sha256="settings")
    )
    yield
    call_context.reset(token)


def record(tmp_path, requests=None, provider=None):
    store = CassetteStore(tmp_path / "cassette", "record", BINDING)
    client = CassetteClient(store, "openai", provider or Provider())
    for request in requests or [REQUEST]:
        client.responses.create(**request)
    store.terminal()
    return store.directory


def test_roundtrip_and_forwarded_options(tmp_path):
    provider = Provider()
    store = CassetteStore(tmp_path / "cassette", "record", BINDING)
    chunks = []
    result = (
        CassetteClient(store, "openai", provider)
        .with_options(timeout=1, max_retries=0)
        .responses.create(**REQUEST, on_output_text_delta=chunks.append)
    )
    assert provider.options == [dict(timeout=1, max_retries=0)]
    store.terminal()
    replay = CassetteStore(store.directory, "replay", BINDING)
    replay_chunks = []
    output = (
        CassetteClient(replay, "openai")
        .with_options(timeout=99)
        .responses.create(**REQUEST, on_output_text_delta=replay_chunks.append)
    )
    assert response_data(output) == response_data(result)
    assert output.output[0].call_id == "call-id"
    assert output.output[1].content[0].refusal == "No"
    assert replay_chunks == chunks == ["first", "second"]
    assert not replay.terminal()["errors"]


@pytest.mark.parametrize(
    "field", ["instructions", "input", "text", "temperature", "model"]
)
def test_semantic_mismatch_sticky(tmp_path, field):
    path = record(tmp_path)
    replay = CassetteStore(path, "replay", BINDING)
    changed = copy.deepcopy(REQUEST)
    changed[field] = "changed"
    with pytest.raises(CassetteError):
        CassetteClient(replay, "openai").responses.create(**changed)
    # Even if application catches this optional visualization/planning miss.
    assert replay.terminal()["errors"]


@pytest.mark.parametrize(
    "field", ["clock", "provider", "model", "cases", "source_sha256", "schema_sha256"]
)
def test_manifest_binding(tmp_path, field):
    path = record(tmp_path)
    with pytest.raises(CassetteError, match="manifest_mismatch"):
        CassetteStore(path, "replay", {**BINDING, field: "changed"})


def test_revision_and_unrelated_corpus_can_change(tmp_path):
    path = record(tmp_path)
    replay = CassetteStore(
        path,
        "replay",
        {**BINDING, "adapter_revision": "new", "corpus_sha256": "unrelated edit"},
    )
    CassetteClient(replay, "openai").responses.create(**REQUEST)
    assert not replay.terminal()["errors"]


def test_unused_extra_out_of_order_settings(tmp_path):
    path = record(tmp_path, [REQUEST, {**REQUEST, "instructions": "second"}])
    replay = CassetteStore(path, "replay", BINDING)
    assert len(replay.terminal()["errors"]) == 2
    client = CassetteClient(replay, "openai")
    with pytest.raises(CassetteError, match="request_sha256"):
        client.responses.create(**{**REQUEST, "instructions": "second"})
    replay = CassetteStore(path, "replay", BINDING)
    token = call_context.set({**call_context.get(), "settings_sha256": "changed"})
    with pytest.raises(CassetteError, match="settings_sha256"):
        CassetteClient(replay, "openai").responses.create(**REQUEST)
    call_context.reset(token)
    replay = CassetteStore(path, "replay", BINDING)
    client = CassetteClient(replay, "openai")
    client.responses.create(**REQUEST)
    client.responses.create(**{**REQUEST, "instructions": "second"})
    with pytest.raises(CassetteError, match="missing_call"):
        client.responses.create(**REQUEST)


def test_failed_attempt_is_retained(tmp_path):
    store = CassetteStore(tmp_path / "cassette", "record", BINDING)
    with pytest.raises(TimeoutError):
        CassetteClient(store, "openai", Provider(failure=True)).responses.create(
            **REQUEST
        )
    store.terminal()
    assert "private error text" not in (store.directory / "call-0000.json").read_text()
    replay = CassetteStore(store.directory, "replay", BINDING)
    with pytest.raises(RecordedProviderTimeout, match="TimeoutError"):
        CassetteClient(replay, "openai").responses.create(**REQUEST)


def test_independent_turn_and_transport_sequences(tmp_path):
    store = CassetteStore(tmp_path / "cassette", "record", BINDING)
    for transport in ("json", "sse"):
        for turn in (0, 1):
            token = call_context.set(
                dict(
                    case="example",
                    turn=turn,
                    transport=transport,
                    settings_sha256="settings",
                )
            )
            CassetteClient(store, "openai", Provider()).responses.create(**REQUEST)
            call_context.reset(token)
    assert set(store.counters.values()) == {1}
    assert len(store.records) == 4


def test_sdk_response_roundtrip():
    from openai.types.responses import Response

    result = Response.model_validate(
        dict(
            id="resp",
            created_at=0,
            model="test",
            object="response",
            parallel_tool_calls=False,
            tool_choice="auto",
            tools=[],
            output=[
                dict(
                    id="msg",
                    type="message",
                    role="assistant",
                    status="completed",
                    content=[dict(type="output_text", text="hello", annotations=[])],
                )
            ],
            status="completed",
        )
    )
    assert response_data(result)["output_text"] == "hello"
    assert response_data(result)["output"][0]["id"] == "msg"


@pytest.mark.parametrize("value", [object(), {1: "bad"}, float("nan")])
def test_strict_serialization(value):
    with pytest.raises((ValueError, TypeError)):
        encoded(value)


def test_network_guard_subprocess():
    code = """import socket
from scripts.ask_eval.cassettes import install_network_guard, CassetteError
errors=[]
def fail(reason):
 errors.append(reason)
 raise CassetteError(reason)
install_network_guard(fail)
for action in [lambda: socket.getaddrinfo("example.com",443),lambda: socket.socket().connect(("8.8.8.8",443))]:
 try: action()
 except CassetteError: pass
 else: raise AssertionError("network escaped")
assert len(errors)==2
socket.getaddrinfo("127.0.0.1",80)
"""
    subprocess.run([sys.executable, "-c", code], check=True)


def test_semantic_clock_does_not_replace_real_date():
    assert today() == date.today()
    token = semantic_date.set(date(2020, 1, 2))
    try:
        assert today() == date(2020, 1, 2)
        from app.agent.time_defaults import resolve_time_defaults

        assert "January 2020" in resolve_time_defaults("Avery in January")[0]
    finally:
        semantic_date.reset(token)
    assert today() == date.today()


def test_endpoint_record_then_offline_replay_and_swallowed_miss(tmp_path):
    # Mechanism-only controlled recording generated within the test. It is never
    # published or described as a real provider capture.
    code = r"""
import os, json
from pathlib import Path
from fastapi.testclient import TestClient
from scripts.ask_eval.cassettes import call_context, sha, CassetteStore, install_network_guard
from scripts.ask_eval.fixtures import load_inputs, ControlledModel
root=Path.cwd()
source,doc=load_inputs(root)
case=next(c for c in doc["cases"] if c["id"]=="points_mean")
binding=dict(provider="openai",model="gpt-5.4-mini",clock="2026-10-18",cases={case["id"]:sha(case)},source_sha256=sha(source),schema_sha256="test",adapter_revision="test",corpus_sha256=sha(doc))
os.environ.update(ASK_EVAL_CASSETTE_MODE="record",ASK_EVAL_CASSETTE_DIR=os.environ["TEST_CASSETTE_DIR"],ASK_EVAL_BINDING=json.dumps(binding),ASK_EVAL_MAX_CALLS="24",ASK_EVAL_CLOCK="2026-10-18",ASK_EVAL_LIVE="0")
from scripts.ask_eval import server
server._get_client=lambda self,provider: ControlledModel(case["turns"][call_context.get()["turn"]])
def run(client, suffix):
 for transport in ("json","sse"):
  response=client.post("/api/agent/ask"+("/stream" if transport=="sse" else "")+"?season=2024-25",json={"question":case["turns"][0]["question"],"provider":"openai","model":"gpt-5.4-mini","conversation_id":suffix+transport},headers={"X-Evaluation-Case":case["id"],"X-Evaluation-Turn":"0"})
  assert response.status_code==200,response.text
with TestClient(server.app) as client:
 run(client,"record")
 status=client.post("/__eval/finalize").json()
 assert status["calls"]>0 and not status["errors"],status
 server.cassette=CassetteStore(os.environ["TEST_CASSETTE_DIR"],"replay",binding)
 def no_provider(*args,**kwargs): raise AssertionError("provider touched")
 server._get_client=no_provider
 for name in ("OPENAI_API_KEY","ANTHROPIC_API_KEY","OPENROUTER_API_KEY"): os.environ.pop(name,None)
 install_network_guard(server.cassette.fail)
 client.post("/__eval/reset-conversations")
 run(client,"replay")
 assert not client.post("/__eval/finalize").json()["errors"]
 assert server.budget.calls==status["calls"]

"""
    import os

    subprocess.run(
        [sys.executable, "-c", code],
        env={**os.environ, "TEST_CASSETTE_DIR": str(tmp_path / "recording")},
        check=True,
        timeout=60,
    )


def test_budget_exhaustion_is_sticky(tmp_path):
    from scripts.ask_eval.budget import BudgetClient, CallBudget

    store = CassetteStore(tmp_path / "cassette", "record", BINDING)
    client = CassetteClient(store, "openai", BudgetClient(Provider(), CallBudget(0)))
    with pytest.raises(CassetteError, match="budget_exhausted"):
        client.responses.create(**REQUEST)
    assert store.terminal()["errors"]


def test_visualization_fallback_cannot_hide_miss(tmp_path):
    from app.agent.visualization import VisualizationAgent, study_charts
    from tests.test_research_insights import presentation_study
    from app.agent.execution import create_response
    from tests.test_semantic_migration import settings

    path = record(tmp_path)
    replay = CassetteStore(path, "replay", BINDING)

    class Agent:
        def _get_client(self, provider):
            return CassetteClient(replay, provider)

        def _request_timeout_seconds(self, provider):
            return 30

        def _create_response(self, **kwargs):
            kwargs["input"] = kwargs.pop("input_messages")
            return create_response(settings=settings(), **kwargs)

    payload = VisualizationAgent().enrich(
        Agent(),
        {"_chart_candidates": study_charts(presentation_study(), {"pts", "ast"}, [])},
        "assists without teammate",
        "openai",
        "test-model",
    )
    assert payload["visualization"]["selection"] == "fallback"
    assert replay.terminal()["errors"]
