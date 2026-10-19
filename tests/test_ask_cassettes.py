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
    sha,
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
for action in [lambda: socket.getaddrinfo("example.com",443),lambda: socket.socket().connect(("8.8.8.8",443)),lambda: socket.socket().connect(("127.0.0.1",80)),lambda: socket.getaddrinfo("localhost",80),lambda: socket.socket(type=socket.SOCK_DGRAM).sendto(b"x",("8.8.8.8",53))]:
 try: action()
 except CassetteError: pass
 else: raise AssertionError("network escaped")
assert len(errors)==5
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


def test_endpoint_record_then_offline_replay_with_strict_semantic_clock(tmp_path):
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
from app.agent import clock
class MissingContextDate:
 @staticmethod
 def today(): raise RuntimeError("semantic clock context lost")
clock.date=MissingContextDate
try: clock.today()
except RuntimeError: pass
else: raise AssertionError("missing semantic clock context was not detected")
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
    from app.agent.execution import create_response
    from app.agent.visualization import VisualizationAgent, study_charts
    from tests.test_research_insights import presentation_study
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


def test_reviewed_recording_rejects_new_planner_contract_offline(tmp_path):
    """Old request hashes must reject the new planner; capture remains immutable."""
    import json
    import os
    from pathlib import Path

    folder = Path("tests/fixtures/ask/recordings/openai-mini-20261018")
    output = tmp_path / "replay"
    env = {
        **os.environ,
        "OPENAI_API_KEY": "",
        "ANTHROPIC_API_KEY": "",
        "OPENROUTER_API_KEY": "",
    }
    result = subprocess.run(
        [
            sys.executable,
            "scripts/evaluate_ask.py",
            "--replay-cassettes",
            str(folder),
            "--output",
            str(output),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "Cassette manifest mismatch: cases" in result.stderr
    # The migrated controlled inputs cannot reuse historical capture binding.
    assert not (output / "results.json").exists()
    historical = json.loads((folder / "observed-outcomes.json").read_text())
    assert {
        key
        for key, modes in historical.items()
        if not all(t["passed"] for turns in modes.values() for t in turns)
    } == {"date_range", "research_home"}
    import hashlib

    from scripts.ask_eval.legacy_recording import RAW_SHA256

    assert all(
        hashlib.sha256((folder / name).read_bytes()).hexdigest() == value
        for name, value in RAW_SHA256.items()
    )


def test_synthetic_repository_rejects_uncaptured_mixed_seasons():
    from app.agent.semantics import SemanticError
    from scripts.ask_eval.fixtures import FrozenWarehouse

    with pytest.raises(SemanticError, match="covers only"):
        FrozenWarehouse({}, "complete").load(["2024-25", "2025-26"])


@pytest.mark.parametrize(
    "metadata,reason",
    [
        ({"status": "incomplete"}, "response_status:incomplete"),
        ({"status": "failed"}, "response_status:failed"),
        ({"status": "cancelled"}, "response_status:cancelled"),
        ({"status": "queued"}, "response_status:queued"),
        ({"status": "completed", "error": {"code": "server_error"}}, "response_error"),
        (
            {"incomplete_details": {"reason": "max_output_tokens"}},
            "response_incomplete",
        ),
        ({"incomplete_details": {}}, "response_incomplete"),
        ({}, None),
        ({"status": None, "error": None, "incomplete_details": None}, None),
        ({"status": "completed"}, None),
    ],
)
def test_terminal_response_metadata_failure_survives_replay(tmp_path, metadata, reason):
    response = SimpleNamespace(
        output_text='{"answer":"usable fallback"}', output=[], **metadata
    )
    provider = SimpleNamespace(responses=SimpleNamespace(create=lambda **kw: response))
    store = CassetteStore(tmp_path / "cassette", "record", BINDING)
    assert (
        CassetteClient(store, "openai", provider).responses.create(**REQUEST)
        is response
    )
    summary = store.terminal()
    assert summary["failed_attempts"] == int(reason is not None)
    assert [x["reason"] for x in summary["unsuccessful_attempts"]] == (
        [reason] if reason else []
    )
    # App accepting output_text cannot clear the terminal harness gate.
    assert bool(not summary["errors"] and not summary["failed_attempts"]) == (
        reason is None
    )
    replay = CassetteStore(store.directory, "replay", BINDING)
    actual = CassetteClient(replay, "openai").responses.create(**REQUEST)
    assert response_data(actual) == response_data(response)
    assert replay.terminal()["failed_attempts"] == summary["failed_attempts"]


def test_optional_visualization_fallback_retains_incomplete_attempt(tmp_path):
    from app.agent.execution import create_response
    from app.agent.visualization import VisualizationAgent, study_charts
    from tests.test_research_insights import presentation_study
    from tests.test_semantic_migration import settings

    response = SimpleNamespace(
        output_text="truncated JSON",
        status="incomplete",
        incomplete_details={"reason": "max_output_tokens"},
    )
    provider = SimpleNamespace(responses=SimpleNamespace(create=lambda **kw: response))
    provider.with_options = lambda **kw: provider
    store = CassetteStore(tmp_path / "cassette", "record", BINDING)

    class Agent:
        def _get_client(self, provider_name):
            return CassetteClient(store, provider_name, provider)

        def _request_timeout_seconds(self, provider_name):
            return 30

        def _create_response(self, **kwargs):
            kwargs["input"] = kwargs.pop("input_messages")
            return create_response(settings=settings(), **kwargs)

    for mode in ("record", "replay"):
        if mode == "replay":
            store = CassetteStore(store.directory, "replay", BINDING)
        payload = VisualizationAgent().enrich(
            Agent(),
            {
                "_chart_candidates": study_charts(
                    presentation_study(), {"pts", "ast"}, []
                )
            },
            "assists without teammate",
            "openai",
            "test-model",
        )
        assert payload["visualization"]["selection"] == "fallback"
        assert payload["charts"]
        assert store.terminal()["failed_attempts"] == 1


def test_private_capture_and_model_log_permissions_at_creation(tmp_path):
    import os
    import stat

    from scripts.ask_eval.budget import CallBudget
    from scripts.ask_eval.private_io import private_directory, private_write

    previous = os.umask(0)
    try:
        root = tmp_path / "private" / "run"
        private_directory(root)
        store = CassetteStore(root / "cassette", "record", BINDING)
        CassetteClient(store, "openai", Provider()).responses.create(**REQUEST)
        store.terminal()
        budget = CallBudget(2, root / "model-calls.jsonl")
        budget.record({"call": 1})
        budget.record({"call": 2})
        private_write(root / "failure.json", '{"status":"failed"}')
        for path in (tmp_path / "private", root, root / "cassette"):
            assert stat.S_IMODE(path.stat().st_mode) == 0o700
        for path in root.rglob("*"):
            if path.is_file():
                assert stat.S_IMODE(path.stat().st_mode) == 0o600
        with pytest.raises(FileExistsError):
            private_write(root / "failure.json", "overwrite")
    finally:
        os.umask(previous)


def test_semantic_clock_ast_inventory():
    import ast
    from pathlib import Path

    # These clocks record real metadata/history or execute deadlines, not scope.
    allow = {"clock.py", "history.py", "semantic_source.py"}
    paths = [*Path("app/agent").glob("*.py"), Path("app/seasons.py")]
    for path in paths:
        if path.name in allow:
            continue
        tree = ast.parse(path.read_text())
        assert not [
            n.lineno
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr in {"today", "now"}
        ], path


def test_uncaptured_reads_are_sticky_for_both_repositories():
    from app.agent.semantic_source import snapshot_digest
    from scripts.ask_eval.fixtures import frozen_repository
    from scripts.ask_eval.production import production_repository
    from scripts.ask_eval.source_guard import failures
    from tests.test_ask_harness import SOURCE, settings

    snapshot = {
        "rows": SOURCE["rows"],
        "sources": ["synthetic"],
        "coverage": [
            {
                "season": "2024-25",
                "phase": phase,
                "rows": sum(r["season_type"] == phase for r in SOURCE["rows"]),
                "data_through": max(
                    r["game_date"] for r in SOURCE["rows"] if r["season_type"] == phase
                ),
            }
            for phase in ("Regular Season", "Playoffs")
        ],
    }
    snapshot["sha256"] = snapshot_digest(snapshot)
    before = len(failures())
    for repo in (
        frozen_repository(settings(), SOURCE, "complete"),
        production_repository(settings(), {"snapshot": snapshot, "details": {}}),
    ):
        try:
            repo._query("uncaptured")
        except RuntimeError:
            pass  # App fallback still cannot erase the source-side evidence.
    assert [x["repository"] for x in failures()[before:]] == [
        "synthetic",
        "production_snapshot",
    ]
    from scripts.evaluate_ask import harness_passed

    assert not harness_passed(
        {"uncaptured_reads": failures()[before:]}, {"errors": [], "failed_attempts": 0}
    )


@pytest.mark.parametrize(
    "mutation", ["request", "model", "chunks", "both", "neither", "error_kind"]
)
def test_shape_and_request_integrity_rejects_rehashed_tampering(tmp_path, mutation):
    import json

    path = record(tmp_path)
    call = path / "call-0000.json"
    data = json.loads(call.read_text())
    if mutation == "request":
        data["request"]["instructions"] = "tampered"
    elif mutation == "model":
        data["match"]["model"] = "other"
    elif mutation == "chunks":
        data["chunks"] = None
    elif mutation == "both":
        data["error"] = {"kind": "provider", "type": "Error", "status_code": None}
    elif mutation == "neither":
        del data["response"]
    else:
        del data["response"]
        data["error"] = {
            "kind": "arbitrary_class",
            "type": "Error",
            "status_code": None,
        }
    call.write_text(encoded(data))
    terminal = json.loads((path / "terminal.json").read_text())
    terminal["files"][call.name] = sha(data)
    (path / "terminal.json").write_text(encoded(terminal))
    with pytest.raises(CassetteError, match="invalid_record"):
        CassetteStore(path, "replay", BINDING)


def test_manifest_integrity_and_untrusted_legacy(tmp_path):
    import json

    path = record(tmp_path)
    manifest = json.loads((path / "manifest.json").read_text())
    manifest["clock"] = "2020-01-01"
    (path / "manifest.json").write_text(encoded(manifest))
    with pytest.raises(CassetteError, match="manifest_integrity"):
        CassetteStore(path, "replay", {**BINDING, "clock": "2020-01-01"})
    manifest["schema_version"] = 1
    (path / "manifest.json").write_text(encoded(manifest))
    with pytest.raises(CassetteError, match="untrusted_legacy"):
        CassetteStore(path, "replay", {**BINDING, "clock": "2020-01-01"})


def test_unsaved_claim_fails_terminal(tmp_path):
    store = CassetteStore(tmp_path / "cassette", "record", BINDING)
    store.claim("openai", REQUEST, {})
    assert "unsaved_claimed_calls" in store.terminal()["errors"]


def test_execution_failure_preserves_code_and_marks_capture_unsuccessful(tmp_path):
    from app.agent.execution import ExecutionFailure
    from scripts.ask_eval.cassettes import failure_data, replay_failure

    failure = ExecutionFailure("request_timeout")
    assert replay_failure(failure_data(failure)).code == "request_timeout"

    def fail(**kwargs):
        raise failure

    store = CassetteStore(tmp_path / "cassette", "record", BINDING)
    client = CassetteClient(
        store, "openai", SimpleNamespace(responses=SimpleNamespace(create=fail))
    )
    with pytest.raises(ExecutionFailure):
        client.responses.create(**REQUEST)
    terminal = store.terminal()
    assert terminal["failed_attempts"] == 1
    assert "execution_failure_during_capture:request_timeout" in terminal["errors"]


def test_successful_return_over_execution_bound_is_unsuccessful(tmp_path, monkeypatch):
    from scripts.ask_eval import cassettes

    times = iter([10.0, 12.0])
    monkeypatch.setattr(cassettes, "monotonic", lambda: next(times))
    store = CassetteStore(tmp_path / "cassette", "record", BINDING)
    client = CassetteClient(store, "openai", Provider(), {"timeout": 1})
    assert client.responses.create(**REQUEST).output_text == "answer"
    assert store.terminal()["failed_attempts"] == 1
    assert store.records[("example", 0, "json", 0)]["execution"]["latency_ms"] == 2000


@pytest.mark.parametrize(
    "reason", ["max_tokens", "refusal", "pause_turn", "end_turn", "tool_use", None]
)
def test_claude_adapter_preserves_and_classifies_finish_reason(reason):
    from app.agent.claude_client import _AdapterResponse
    from scripts.ask_eval.cassettes import unsuccessful_attempt

    response = _AdapterResponse(
        SimpleNamespace(content=[], usage=None, stop_reason=reason)
    )
    data = response_data(response)
    assert data["stop_reason"] == reason
    assert bool(unsuccessful_attempt({"response": data})) == (
        reason in {"max_tokens", "refusal", "pause_turn"}
    )


def test_credential_loader_ignores_noncredential_settings(tmp_path, monkeypatch):
    from scripts.ask_eval.environment import load_provider_credentials

    for key in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENROUTER_API_KEY",
        "OPENAI_AGENT_MODEL",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "existing-synthetic")
    path = tmp_path / ".env"
    path.write_text(
        "OPENAI_API_KEY=other-synthetic\nANTHROPIC_API_KEY=fake-synthetic\nOPENAI_AGENT_MODEL=changed\n"
    )
    load_provider_credentials(path)
    import os

    assert os.environ["OPENAI_API_KEY"] == "existing-synthetic"
    assert os.environ["ANTHROPIC_API_KEY"] == "fake-synthetic"
    assert "OPENAI_AGENT_MODEL" not in os.environ


def test_dns_and_process_escapes_are_guarded():
    code = """import os,socket,subprocess,sys
from scripts.ask_eval.cassettes import install_network_guard, CassetteError
errors=[]
def fail(reason):
 errors.append(reason)
 raise CassetteError(reason)
install_network_guard(fail)
actions=[lambda: socket.gethostbyname("example.com"),lambda: socket.gethostbyname_ex("example.com"),lambda: socket.gethostbyaddr("8.8.8.8"),lambda: subprocess.Popen([sys.executable,"-c","pass"]),lambda: os.system("true"),lambda: os.execv(sys.executable,[sys.executable,"-c","raise SystemExit(73)"])]
if hasattr(os,"posix_spawn"): actions.append(lambda: os.posix_spawn(sys.executable,[sys.executable,"-c","pass"],os.environ))
for action in actions:
 try: action()
 except CassetteError: pass
 else: raise AssertionError("escape allowed")
assert len(errors)==len(actions)
"""
    subprocess.run([sys.executable, "-c", code], check=True)


def test_cassette_middleware_errors_are_structured_and_reset_clock(tmp_path):
    import os

    code = r"""
import os,json,asyncio
from pathlib import Path
from fastapi import Request
from fastapi.testclient import TestClient
from scripts.ask_eval.fixtures import load_inputs, FixtureCapabilityError
from scripts.ask_eval.cassettes import sha
source,document=load_inputs(Path.cwd())
case=next(c for c in document["cases"] if c["id"]=="points_mean")
binding=dict(provider="openai",model="gpt-5.4-mini",clock="2026-10-18",cases={case["id"]:sha(case)})
os.environ.update(ASK_EVAL_CASSETTE_MODE="record",ASK_EVAL_CASSETTE_DIR=os.environ["TEST_CASSETTE_DIR"],ASK_EVAL_BINDING=json.dumps(binding),ASK_EVAL_MAX_CALLS="1",ASK_EVAL_CLOCK="2026-10-18",ASK_EVAL_LIVE="0")
from scripts.ask_eval import server
from app.agent.clock import semantic_date
def unavailable(base,attachments):
 if attachments: raise FixtureCapabilityError("study","application_setting_missing")
 return base
server.lookup["points_mean"]["attachments"]=["study"]
server._attachments.configure=unavailable
with TestClient(server.app) as client:
 for suffix in ("","/stream"):
  endpoint="/api/agent/ask"+suffix
  assert client.post(endpoint,json={},headers={"X-Evaluation-Case":"unknown","X-Evaluation-Turn":"0"}).status_code==400
  assert client.post(endpoint,json={},headers={"X-Evaluation-Case":"points_mean"}).status_code==400
  response=client.post(endpoint,json={},headers={"X-Evaluation-Case":"points_mean","X-Evaluation-Turn":"0"})
  assert response.status_code==501,response.text
  assert response.json()["detail"]["code"]=="evaluation_fixture_capability_unavailable"
 assert client.get("/__eval/metadata").json()["fixture_capability_failures"]
async def check_reset():
 assert semantic_date.get() is None
 request=Request({"type":"http","path":"/api/agent/ask","headers":[(b"x-evaluation-case",b"points_mean"),(b"x-evaluation-turn",b"0")]})
 async def next_call(request): raise AssertionError("must fail setup")
 result=await server.evaluation_context(request,next_call)
 assert result.status_code==501
 assert semantic_date.get() is None
asyncio.run(check_reset())
"""
    subprocess.run(
        [sys.executable, "-c", code],
        env={**os.environ, "TEST_CASSETTE_DIR": str(tmp_path / "cassette")},
        check=True,
        timeout=60,
    )


def test_trace_only_settings_do_not_change_binding_or_replay(tmp_path):
    from dataclasses import replace

    from scripts.ask_eval.cassettes import settings_binding
    from tests.test_api import _test_settings

    base = _test_settings()
    traced = replace(
        base,
        agent_trace_enabled=True,
        agent_trace_directory=str(tmp_path / "private"),
        agent_trace_max_bytes=2048,
        agent_trace_max_records=1,
        agent_trace_retention_days=1,
    )
    assert settings_binding(base) == settings_binding(traced)
    token = call_context.set(
        dict(
            case="example",
            turn=0,
            transport="json",
            settings_sha256=settings_binding(base),
        )
    )
    try:
        directory = record(tmp_path)
        call_context.set(
            dict(
                case="example",
                turn=0,
                transport="json",
                settings_sha256=settings_binding(traced),
            )
        )
        replay = CassetteStore(directory, "replay", BINDING)
        response = CassetteClient(replay, "openai").responses.create(**REQUEST)
        assert response.output_text == "answer"
        assert replay.terminal()["calls"] == 1
    finally:
        call_context.reset(token)


@pytest.mark.parametrize(
    "changes",
    [
        {"openai_agent_model": "different-model"},
        {"agent_planner_enabled": False},
        {"agent_max_tool_calls": 7},
        {"season": "2024-25"},
        {"agent_request_deadline_seconds": 121.0},
        {"agent_max_concurrent_requests": 2},
        {"agent_stream_buffer_bytes": 2000},
        {"agent_conversation_max_turns": 4},
        {"openai_agent_max_retries": 3},
    ],
)
def test_generation_scope_execution_settings_remain_bound(changes):
    from dataclasses import replace

    from scripts.ask_eval.cassettes import settings_binding
    from tests.test_api import _test_settings

    base = _test_settings()
    assert settings_binding(base) != settings_binding(replace(base, **changes))


def test_trace_only_field_set_requires_review_when_settings_expand():
    from dataclasses import fields

    from app.config import Settings
    from scripts.ask_eval.cassettes import TRACE_ONLY_FIELDS

    assert {
        f.name for f in fields(Settings) if f.name.startswith("agent_trace_")
    } == TRACE_ONLY_FIELDS
