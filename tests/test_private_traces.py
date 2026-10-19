"""Synthetic local diagnostics, no providers or warehouse access."""

import json
import os
import time
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from app.agent.private_traces import Projection, write_record
from app.main import app
from fastapi.testclient import TestClient
from tests.test_api import _test_settings, build_client


@pytest.fixture(autouse=True)
def workers(monkeypatch):
    from threading import Thread

    active = []

    class TrackedThread(Thread):
        def start(self):
            active.append(self)
            return super().start()

    monkeypatch.setattr("app.main.Thread", TrackedThread)

    def join():
        for thread in active:
            if thread.ident is not None:
                thread.join(5)
                assert not thread.is_alive(), "private trace worker did not finish"

    yield join
    join()


def settings(tmp_path, **overrides):
    return _test_settings(
        agent_trace_enabled=True,
        agent_trace_directory=str(tmp_path / "traces"),
        agent_rate_limit_per_minute=0,
        agent_rate_limit_daily=0,
        **overrides,
    )


def record():
    return dict(
        schema="ask_private_trace/1",
        request_id=uuid4().hex,
        time=time.time(),
        transport="json",
        outcome="answered",
        error_type="",
        code={},
        config_sha256="a",
    )


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("fail", [False, True])
def test_actual_endpoints_capture_success_failure(
    tmp_path, monkeypatch, stream, fail, workers
):
    config = settings(tmp_path)
    build_client(settings=config)

    def answer(question, **kwargs):
        kwargs["trace"].outcome = "answered"
        if fail:
            raise RuntimeError("provider private body")
        return {
            "answer": "synthetic",
            "semantic_plan": {"season": "2025-26"},
            "semantic_evidence": {
                "evidence_id": "synthetic-id",
                "password": "forbidden",
            },
        }

    monkeypatch.setattr(
        "app.main._build_stats_agent", lambda *args: SimpleNamespace(answer=answer)
    )
    client = TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000))
    response = client.post(
        "/api/agent/ask" + ("/stream" if stream else ""),
        json={
            "question": "stats for synthetic@example.com",
            "conversation_id": "private-client-id",
        },
    )
    assert response.status_code == (502 if fail and not stream else 200)
    path = tmp_path / "traces" / (response.headers["x-request-id"] + ".json")
    workers()
    trace = json.loads(path.read_text())
    assert trace["transport"] == ("sse" if stream else "json")
    assert trace["outcome"] == ("error" if fail else "answered")
    assert trace["code"]["source_sha256"]
    assert trace["config_sha256"]
    assert "redacted" in trace["incomplete_reasons"]
    assert "private-client-id" not in path.read_text()
    assert "synthetic@example.com" not in path.read_text()
    assert "provider private body" not in path.read_text()
    if not fail:
        assert trace["answer_projection"]["semantic_plan"]["season"] == "2025-26"
        assert "forbidden" not in path.read_text()
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("mode", ["disabled", "remote", "forwarded", "origin", "host"])
def test_no_capture_outside_boundary(tmp_path, mode):
    config = settings(tmp_path)
    if mode == "disabled":
        config = replace(config, agent_trace_enabled=False)
    build_client(settings=config)
    client = TestClient(
        app,
        base_url="http://localhost" if mode != "host" else "http://example.com",
        client=("192.0.2.1" if mode == "remote" else "127.0.0.1", 50000),
    )
    headers = (
        {"x-forwarded-proto": "http"}
        if mode == "forwarded"
        else {"origin": "http://evil.example"}
        if mode == "origin"
        else {}
    )
    assert (
        client.post(
            "/api/agent/ask",
            json={"question": "stats", "model": "invalid"},
            headers=headers,
        ).status_code
        == 400
    )
    assert not (tmp_path / "traces").exists()


def test_validation_and_storage_failure_preserve_response(
    tmp_path, monkeypatch, caplog
):
    build_client(settings=settings(tmp_path))
    client = TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000))
    response = client.post(
        "/api/agent/ask", json={"question": "stats", "model": "invalid"}
    )
    trace = json.loads(next((tmp_path / "traces").glob("*.json")).read_text())
    assert trace["error_type"] == "HTTP_400"

    def broken(*args):
        raise OSError("sensitive/path/token")

    monkeypatch.setattr("app.agent.private_traces.write_record", broken)
    failed_disk = client.post(
        "/api/agent/ask", json={"question": "stats", "model": "invalid"}
    )
    assert failed_disk.status_code == response.status_code
    assert failed_disk.json() == response.json()
    assert "sensitive/path/token" not in caplog.text
    assert "private_trace_write_failed" in caplog.text


def test_projection_is_bounded_allowlisted_and_redacted():
    secret = "sk-" + "x" * 40
    p = Projection()
    value = p.project(
        {
            "question": secret + " test@example.com password=hunter2 user_id=abc",
            "unknown": secret,
            "context": [{"question": "x" * 10000}] * 100,
        }
    )
    serialized = json.dumps(value)
    assert (
        secret not in serialized
        and "hunter2" not in serialized
        and "abc" not in serialized
    )
    assert "unknown" not in value
    assert len(value["context"]) == 20
    assert {"redacted", "truncated", "fields_omitted"} <= p.reasons


def test_retention_duplicates_and_private_permissions(tmp_path):
    config = settings(tmp_path, agent_trace_max_records=2)
    first = record()
    write_record(config, first)
    with pytest.raises(FileExistsError):
        write_record(config, first)
    directory = tmp_path / "traces"
    arbitrary = directory / "keep.txt"
    arbitrary.write_text("not owned")
    old = directory / (first["request_id"] + ".json")
    os.utime(old, (0, 0))
    write_record(config, record())
    assert not old.exists()
    for _ in range(4):
        write_record(config, record())
    assert len(list(directory.glob("*.json"))) == 2
    assert arbitrary.exists()


def test_symlink_and_untrusted_directory_refused(tmp_path):
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    (tmp_path / "traces").symlink_to(real, target_is_directory=True)
    with pytest.raises(OSError):
        write_record(settings(tmp_path), record())
    assert not list(real.iterdir())
    (tmp_path / "traces").unlink()
    (tmp_path / "traces").mkdir(mode=0o755)
    (tmp_path / "traces").chmod(0o755)
    with pytest.raises(ValueError):
        write_record(settings(tmp_path), record())


def test_record_byte_limit(tmp_path):
    config = settings(tmp_path, agent_trace_max_bytes=2048)
    data = record()
    data["question"] = "x" * 5000
    write_record(config, data)
    path = next((tmp_path / "traces").glob("*.json"))
    assert path.stat().st_size <= 2048
    assert "record_size_limit" in json.loads(path.read_text())["incomplete_reasons"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("agent_trace_max_bytes", 1),
        ("agent_trace_max_records", 0),
        ("agent_trace_retention_days", 366),
    ],
)
def test_config_bounds(tmp_path, field, value):
    with pytest.raises(ValueError):
        settings(tmp_path, **{field: value})


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("failure", ["capacity", "worker", "rate"])
def test_rejections_record_safe_outcome(tmp_path, monkeypatch, stream, failure):
    from app.agent.capacity import CapacityExceeded
    from fastapi import HTTPException

    build_client(settings=settings(tmp_path))
    if failure == "capacity":

        def reject(*args):
            raise CapacityExceeded("capacity_exceeded")

        monkeypatch.setattr("app.main.admission.acquire", reject)
    elif failure == "worker":

        class BrokenThread:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                raise RuntimeError("private worker error")

        monkeypatch.setattr("app.main.Thread", BrokenThread)
    else:

        def limit(*args):
            raise HTTPException(429, "limited")

        monkeypatch.setattr("app.main._check_agent_rate_limit", limit)
    client = TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000))
    response = client.post(
        "/api/agent/ask" + ("/stream" if stream else ""), json={"question": "stats"}
    )
    assert response.status_code == (429 if failure == "rate" else 503)
    path = tmp_path / "traces" / (response.headers["x-request-id"] + ".json")
    trace = json.loads(path.read_text())
    assert (
        trace["error_type"]
        == {
            "capacity": "capacity_exceeded",
            "worker": "worker_start_failure",
            "rate": "HTTP_429",
        }[failure]
    )
    assert "private worker error" not in path.read_text()


@pytest.mark.parametrize("stream", [False, True])
def test_success_unchanged_on_storage_failure(tmp_path, monkeypatch, stream, workers):
    from threading import Event

    attempted = Event()
    build_client(settings=settings(tmp_path))

    def answer(question, **kwargs):
        kwargs["trace"].outcome = "answered"
        return {"answer": "stable synthetic answer"}

    def fail(*args):
        attempted.set()
        raise OSError("private storage detail")

    monkeypatch.setattr(
        "app.main._build_stats_agent", lambda *args: SimpleNamespace(answer=answer)
    )
    monkeypatch.setattr("app.agent.private_traces.write_record", fail)
    client = TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000))
    response = client.post(
        "/api/agent/ask" + ("/stream" if stream else ""), json={"question": "stats"}
    )
    assert response.status_code == 200
    workers()
    assert attempted.is_set()
    assert "stable synthetic answer" in response.text
    assert "private storage detail" not in response.text
    assert not (tmp_path / "traces").exists()


def test_context_is_allowlisted_and_secrets_redacted(tmp_path):
    from app.agent.conversation import InMemoryConversationStore
    from app.agent.observability import AgentTrace
    from app.agent.private_traces import PrivateCapture

    trace = AgentTrace(uuid4().hex, "synthetic", "model", "private-id")
    capture = PrivateCapture(
        settings(tmp_path),
        trace,
        provider="openai",
        transport="json",
        static_version="test",
    )
    store = InMemoryConversationStore()
    store.append_turn(
        "private-id",
        question="email@example.com",
        answer="raw answer excluded",
        max_turns=2,
        context={"season": "2025-26", "unknown": "private"},
    )
    store.set_pending_clarification(
        "private-id", question="select", query_plan={"player_id": 7}
    )
    capture.context(store, "private-id")
    capture.finish(trace)
    payload = next((tmp_path / "traces").glob("*.json")).read_text()
    assert "email@example.com" not in payload and "raw answer excluded" not in payload
    assert (
        json.loads(payload)["context_before"]["pending"]["query_plan"]["player_id"] == 7
    )


def test_held_storage_lock_drops_capture_without_waiting(tmp_path):
    import fcntl

    from app.agent.observability import AgentTrace
    from app.agent.private_traces import PrivateCapture

    config = settings(tmp_path)
    write_record(config, record())
    with (tmp_path / "traces" / ".lock").open("rb") as locked:
        fcntl.flock(locked, fcntl.LOCK_EX | fcntl.LOCK_NB)
        trace = AgentTrace(uuid4().hex, "synthetic", "model")
        capture = PrivateCapture(
            config, trace, provider="openai", transport="json", static_version="test"
        )
        start = time.monotonic()
        capture.finish(trace)
        assert time.monotonic() - start < 1
    assert len(list((tmp_path / "traces").glob("*.json"))) == 1


def test_failed_publication_preserves_prior_records(tmp_path, monkeypatch):
    config = settings(tmp_path, agent_trace_max_records=1)
    first = record()
    write_record(config, first)
    path = tmp_path / "traces" / (first["request_id"] + ".json")
    os.utime(path, (0, 0))
    original = path.read_bytes()

    def broken(*args, **kwargs):
        raise OSError("disk failure")

    monkeypatch.setattr("app.agent.private_traces.os.fsync", broken)
    with pytest.raises(OSError):
        write_record(config, record())
    assert path.read_bytes() == original
    assert not list(path.parent.glob("*.tmp"))


def test_recognizable_local_paths_are_redacted():
    p = Projection()
    value = p.project(
        {
            "source": "/Users/example/privatefile.json",
            "evidence_id": r"C:\Users\example\privatefile.json",
        }
    )
    assert value == {"source": "[REDACTED]", "evidence_id": "[REDACTED]"}
    assert "redacted" in p.reasons


@pytest.mark.parametrize("stream", [False, True])
def test_slow_persistence_does_not_hold_admission(tmp_path, monkeypatch, stream):
    from threading import Event

    from app.main import admission

    entered, release = Event(), Event()
    writes = []
    build_client(settings=settings(tmp_path, agent_max_concurrent_requests=1))

    def answer(question, **kwargs):
        kwargs["trace"].outcome = "answered"
        return {"answer": "synthetic"}

    def slow(*args):
        writes.append(1)
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(
        "app.main._build_stats_agent", lambda *args: SimpleNamespace(answer=answer)
    )
    monkeypatch.setattr("app.agent.private_traces.write_record", slow)
    client = TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000))
    endpoint = "/api/agent/ask" + ("/stream" if stream else "")
    try:
        first = client.post(
            endpoint,
            json={"question": "stats", "conversation_id": "same-local-conversation"},
        )
        assert first.status_code == 200
        assert entered.wait(1)
        second = client.post(
            endpoint,
            json={
                "question": "stats again",
                "conversation_id": "same-local-conversation",
            },
        )
        assert second.status_code == 200
        assert len(writes) == 1
        lease = admission.acquire("2025-26", "same-local-conversation", 1)
        lease.release()
    finally:
        release.set()


@pytest.mark.parametrize("rollback", [False, True])
def test_newest_record_survives_equal_or_rollback_mtime(
    tmp_path, monkeypatch, rollback
):
    config = settings(tmp_path, agent_trace_max_records=1)
    first = record()
    first["request_id"] = "f" * 32
    write_record(config, first)
    original_link = os.link

    def link(*args, **kwargs):
        original_link(*args, **kwargs)
        os.utime(args[1], (100, 100), dir_fd=kwargs["dst_dir_fd"])

    monkeypatch.setattr("app.agent.private_traces.os.link", link)
    os.utime(
        tmp_path / "traces" / (first["request_id"] + ".json"),
        (200 if rollback else 100,) * 2,
    )
    newest = record()
    newest["request_id"] = "0" * 32
    write_record(config, newest)
    assert [p.name for p in (tmp_path / "traces").glob("*.json")] == [
        newest["request_id"] + ".json"
    ]


def test_cleanup_error_closes_lock_and_directory(tmp_path, monkeypatch):
    config = settings(tmp_path)
    real_open, real_unlink = os.open, os.unlink
    fds = []

    def opened(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        fds.append(fd)
        return fd

    def unlink(path, *args, **kwargs):
        if str(path).endswith(".tmp"):
            raise PermissionError("private cleanup path")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr("app.agent.private_traces.os.open", opened)
    monkeypatch.setattr("app.agent.private_traces.os.unlink", unlink)
    with pytest.raises(PermissionError):
        write_record(config, record())
    for fd in set(fds):
        with pytest.raises(OSError):
            os.fstat(fd)


@pytest.mark.parametrize(
    "identifier",
    [
        "ask_question_layout",
        "task_bundle_v1",
        "basketball_reference",
        "risk_adjusted_score",
        "https://example.test/users/123",
        "https://example.test/Users/person/private.json",
    ],
)
def test_redaction_preserves_identifiers_and_urls(identifier):
    projection = Projection()
    assert projection.text(identifier) == identifier
    assert "redacted" not in projection.reasons


@pytest.mark.parametrize(
    "text",
    [
        '"api_key": "synthetic credential"',
        "Authorization: Basic c3ludGhldGlj",
        "~/private/file",
        '"/Users/example person/private file.json"',
        "sk-" + "x" * 24,
        "ghp_" + "x" * 24,
    ],
)
def test_redaction_recognizes_additional_secrets_and_paths(text):
    projection = Projection()
    assert "[REDACTED]" in projection.text(text)
    assert "redacted" in projection.reasons


def test_empty_trace_environment_uses_defaults(monkeypatch):
    from app.config import get_settings

    for key in ("DIRECTORY", "MAX_BYTES", "MAX_RECORDS", "RETENTION_DAYS"):
        monkeypatch.setenv("AGENT_TRACE_" + key, "")
    monkeypatch.setenv("AGENT_TRACE_ENABLED", "false")
    config = get_settings()
    assert config.agent_trace_directory == "local_notes/ask_traces"
    assert (
        config.agent_trace_max_bytes,
        config.agent_trace_max_records,
        config.agent_trace_retention_days,
    ) == (65536, 100, 7)


def test_code_digest_failure_is_cached(monkeypatch):
    from pathlib import Path

    from app.agent.private_traces import code_digest

    calls = []

    def broken(*args):
        calls.append(1)
        raise OSError("private path")

    code_digest.cache_clear()
    try:
        with monkeypatch.context() as patch:
            patch.setattr(Path, "read_bytes", broken)
            assert code_digest() is None
            assert code_digest() is None
            assert len(calls) == 1
    finally:
        code_digest.cache_clear()


def test_context_capture_readers_do_not_mutate_store(tmp_path):
    from app.agent.conversation import InMemoryConversationStore

    store = InMemoryConversationStore()
    store.append_turn("id", question="synthetic", answer="synthetic", max_turns=2)
    store.set_pending_clarification("id", question="which", query_plan={"player_id": 7})
    import copy

    before = copy.deepcopy(store._conversations)
    store.get_turns("id", max_turns=2)
    store.get_pending_clarification("id")
    assert store._conversations == before


def test_process_writer_limit_bounds_concurrent_captures(tmp_path, monkeypatch):
    from threading import Barrier, Event, Lock, Thread

    from app.agent.observability import AgentTrace
    from app.agent.private_traces import PrivateCapture

    entrants = 12
    start = Barrier(entrants)
    entered, dropped, release = Event(), Event(), Event()
    guard = Lock()
    writes = []
    skips = []

    def slow(*args):
        writes.append(1)
        entered.set()
        assert release.wait(5)

    def diagnostic(code):
        # A stalled diagnostic sink must never be reached by dropped captures.
        assert release.wait(5)

    monkeypatch.setattr("app.agent.private_traces.write_record", slow)
    monkeypatch.setattr("app.agent.private_traces.diagnostic", diagnostic)

    def run():
        trace = AgentTrace(uuid4().hex, "synthetic", "model")
        capture = PrivateCapture(
            settings(tmp_path),
            trace,
            provider="openai",
            transport="json",
            static_version="test",
        )
        start.wait(5)
        capture.finish(trace)
        with guard:
            skips.append(1)
            if len(skips) == entrants - 1:
                dropped.set()

    threads = [Thread(target=run) for _ in range(entrants)]
    try:
        for thread in threads:
            thread.start()
        assert entered.wait(5)
        assert dropped.wait(5)
        assert len(writes) == 1
    finally:
        release.set()
        for thread in threads:
            thread.join(5)
            assert not thread.is_alive()
