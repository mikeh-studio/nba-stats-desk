"""Real ASGI request/delivery faults with offline, controlled governed workers."""

import asyncio
import json
import sys
from pathlib import Path
from threading import Event

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.capacity import Admission
from app.agent.conversation import InMemoryConversationStore
from app.agent.execution import ExecutionFailure, governed_request
from app.main import app
from app.seasons import current_season
from tests.support.agent import _settings
from tests.support.api import build_client


class ASGIRequest:
    def __init__(self, endpoint, conversation="capacity-chat", season="2024-25"):
        self.endpoint, self.conversation, self.season = endpoint, conversation, season
        self.incoming = asyncio.Queue()
        self.chunks = []
        self.headers = {}
        self.status = None
        self.meta_sent = asyncio.Event()
        self.body_sent = asyncio.Event()
        self.fail_send = False
        self.hold_send = None
        self.spec_version = "2.3"
        self.payload = None

    async def run(self):
        body = json.dumps(
            self.payload
            or {
                "question": "private synthetic request",
                "conversation_id": self.conversation,
            }
        ).encode()
        await self.incoming.put(
            {"type": "http.request", "body": body, "more_body": False}
        )
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": self.spec_version},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": self.endpoint,
            "raw_path": self.endpoint.encode(),
            "query_string": f"season={self.season}".encode(),
            "headers": [
                (b"content-type", b"application/json"),
                (b"host", b"localhost"),
            ],
            "server": ("localhost", 80),
            "client": ("127.0.0.1", 50000),
        }
        await app(scope, self.incoming.get, self.send)

    async def send(self, message):
        if message["type"] == "http.response.start":
            self.status = message["status"]
            self.headers = dict(message["headers"])
            return
        body = message.get("body", b"")
        self.chunks.append(body)
        if b"event: meta" in body:
            self.meta_sent.set()
        if body:
            self.body_sent.set()
            if self.hold_send:
                await self.hold_send.wait()
            if self.fail_send:
                raise OSError("private disconnected socket")

    async def disconnect(self):
        await self.incoming.put({"type": "http.disconnect"})

    @property
    def wire(self):
        return b"".join(self.chunks).decode()


@pytest.fixture
def controlled(monkeypatch, tmp_path):
    import app.main as main

    settings = _settings(
        agent_max_concurrent_requests=1,
        agent_stream_buffer_bytes=1,
        agent_stream_buffer_events=1,
        agent_rate_limit_per_minute=0,
        agent_rate_limit_daily=0,
        agent_history_enabled=True,
        agent_history_path=str(tmp_path / "history.jsonl"),
    )
    build_client(settings=settings)
    gate, started, finished = Event(), Event(), Event()
    stores = {}
    deliveries = []
    factory = main.Delivery

    def delivery(**kwargs):
        value = factory(**kwargs)
        deliveries.append(value)
        return value

    worker_done = []

    class TrackedAdmission(Admission):
        def acquire(self, *args, **kwargs):
            lease = super().acquire(*args, **kwargs)
            done = Event()
            worker_done.append(done)
            complete = lease.complete
            release = lease.release

            def mark_complete(*, worker=False, delivery=False):
                complete(worker=worker, delivery=delivery)
                if worker:
                    done.set()

            def mark_release():
                release()
                done.set()

            lease.complete = mark_complete
            lease.release = mark_release
            return lease

    monkeypatch.setattr(main, "Delivery", delivery)
    monkeypatch.setattr(main, "admission", TrackedAdmission())
    monkeypatch.setattr(
        main,
        "_season_conversation_store",
        lambda season: stores.setdefault(season, InMemoryConversationStore()),
    )

    class Agent:
        def __init__(self):
            self.settings = settings

        @governed_request
        def answer(
            self, question, *, conversation_id, trace, progress_callback=None, **kwargs
        ):
            store = main._season_conversation_store(current_season())
            store.clear_pending_clarification(conversation_id)
            store.set_pending_clarification(
                conversation_id, question="new", query_plan=None
            )
            store.append_turn(
                conversation_id, question=question, answer="successful", max_turns=6
            )
            if progress_callback:
                for _ in range(100):
                    progress_callback({"type": "answer_delta", "delta": "partial "})
            started.set()
            assert gate.wait(10), "test worker was not released"
            if state["fail"]:
                raise ExecutionFailure("provider_failure")
            trace.outcome = "answered"
            return {
                "answer": "successful",
                "conversation_id": conversation_id,
                "status": "ok",
            }

    monkeypatch.setattr(main, "_build_stats_agent", lambda *args: Agent())
    original_record = main._record_agent_history

    def record(*args, **kwargs):
        original_record(*args, **kwargs)
        finished.set()

    monkeypatch.setattr(main, "_record_agent_history", record)
    state = {
        "fail": False,
        "gate": gate,
        "started": started,
        "finished": finished,
        "stores": stores,
        "deliveries": deliveries,
        "settings": settings,
        "main": main,
        "worker_done": worker_done,
    }
    yield state
    gate.set()
    for done in worker_done:
        assert done.wait(5), "worker did not finish before fixture teardown"
    app.dependency_overrides.clear()


async def wait_started(state):
    assert await asyncio.to_thread(state["started"].wait, 5)


async def wait_workers(state):
    # Response delivery and worker finalization have independent lifetimes.
    # Wait on actual completion, never assume a scheduler ordering or sleep.
    for done in state["worker_done"]:
        assert await asyncio.to_thread(done.wait, 5)


@pytest.mark.parametrize(
    "held,rejected",
    [
        ("/api/agent/ask", "/api/agent/ask/stream"),
        ("/api/agent/ask/stream", "/api/agent/ask"),
    ],
)
def test_json_and_sse_share_capacity_before_restoring_rejected_context(
    held, rejected, controlled, caplog
):
    async def scenario():
        active = ASGIRequest(held)
        task = asyncio.create_task(active.run())
        await wait_started(controlled)
        busy = ASGIRequest(rejected, "other-chat")
        await busy.run()
        assert busy.status == 503
        assert b"x-request-id" in busy.headers
        assert "busy" in busy.wire
        assert controlled["stores"]["2024-25"].get_turns("other-chat", 6) == []
        conflict = ASGIRequest(rejected)
        await conflict.run()
        assert conflict.status == 409
        controlled["gate"].set()
        await asyncio.wait_for(task, 5)
        await wait_workers(controlled)
        assert "successful" in active.wire
        retry = ASGIRequest(rejected, "other-chat")
        await asyncio.wait_for(retry.run(), 5)
        await wait_workers(controlled)
        assert retry.status == 200

    with caplog.at_level("INFO", logger="app.agent"):
        asyncio.run(scenario())
    assert "private synthetic request" not in caplog.text
    summaries = [
        json.loads(r.message)
        for r in caplog.records
        if "agent_request_summary" in r.message
    ]
    assert len(summaries) == 4
    assert {r["outcome"] for r in summaries} == {
        "answered",
        "capacity_exceeded",
        "conversation_busy",
    }


def test_sse_disconnect_clears_delivery_but_retains_capacity_until_success(controlled):
    async def scenario():
        request = ASGIRequest("/api/agent/ask/stream")
        task = asyncio.create_task(request.run())
        await request.meta_sent.wait()
        await wait_started(controlled)
        await request.disconnect()
        await asyncio.wait_for(task, 5)
        delivery = controlled["deliveries"][0]
        assert delivery.detached
        assert delivery.metrics()["stream_peak_bytes"] <= 1
        busy = ASGIRequest("/api/agent/ask", "other")
        await busy.run()
        assert busy.status == 503
        controlled["gate"].set()
        assert await asyncio.to_thread(controlled["finished"].wait, 5)
        # Completion does not require the disconnected event loop.
        await wait_workers(controlled)
        assert not controlled["main"].admission._active
        turns = controlled["stores"]["2024-25"].get_turns("capacity-chat", 6)
        assert len(turns) == 1 and turns[0].answer == "successful"
        # Season-specific history is inspected through the application's path helper.
        from pathlib import Path

        from app.seasons import settings_for_season

        path = settings_for_season(controlled["settings"], "2024-25").agent_history_path
        assert "successful" in Path(path).read_text()

    asyncio.run(scenario())


@pytest.mark.parametrize("endpoint", ["/api/agent/ask", "/api/agent/ask/stream"])
def test_failure_after_pending_change_preserves_context_and_saves_no_success(
    endpoint, controlled, caplog
):
    controlled["fail"] = True
    controlled["gate"].set()
    store = InMemoryConversationStore()
    store.set_pending_clarification(
        "capacity-chat", question="original", query_plan=None
    )
    controlled["stores"]["2024-25"] = store

    async def scenario():
        request = ASGIRequest(endpoint)
        await asyncio.wait_for(request.run(), 5)
        assert request.status == (200 if endpoint.endswith("stream") else 502)
        if endpoint.endswith("stream"):
            assert request.wire.count("event: error") == 1
            assert "event: final" not in request.wire
        assert "private synthetic request" not in request.wire

    asyncio.run(scenario())
    assert store.get_turns("capacity-chat", 6) == []
    assert store.get_pending_clarification("capacity-chat").question == "original"
    from pathlib import Path

    assert not list(
        Path(controlled["settings"].agent_history_path).parent.glob("*.jsonl")
    )


@pytest.mark.parametrize("endpoint", ["/api/agent/ask", "/api/agent/ask/stream"])
def test_completed_worker_keeps_slot_while_full_answer_delivery_is_blocked(
    endpoint, controlled
):
    async def scenario():
        request = ASGIRequest(endpoint)
        request.hold_send = asyncio.Event()
        controlled["gate"].set()
        task = asyncio.create_task(request.run())
        await request.body_sent.wait()
        busy = ASGIRequest("/api/agent/ask/stream", "other")
        await busy.run()
        assert busy.status == 503
        request.hold_send.set()
        await asyncio.wait_for(task, 5)
        await wait_workers(controlled)
        assert not controlled["main"].admission._active

    asyncio.run(scenario())


def test_asgi24_failed_send_detaches_and_does_not_cancel_worker(controlled):
    async def scenario():
        request = ASGIRequest("/api/agent/ask/stream")
        request.spec_version = "2.4"
        request.fail_send = True
        with pytest.raises(Exception):
            await request.run()
        assert controlled["deliveries"][0].detached
        assert controlled["main"].admission._active
        controlled["gate"].set()
        assert await asyncio.to_thread(controlled["finished"].wait, 5)

    asyncio.run(scenario())


def test_thread_start_failure_releases_admission_with_safe_failure(
    controlled, monkeypatch, caplog
):
    def fail(*args):
        raise RuntimeError("private thread-start failure")

    monkeypatch.setattr(
        controlled["main"],
        "Thread",
        lambda **kwargs: type("BrokenThread", (), {"start": fail})(),
    )

    async def scenario():
        request = ASGIRequest("/api/agent/ask/stream")
        await request.run()
        assert request.status == 503
        assert "private" not in request.wire
        await wait_workers(controlled)
        assert not controlled["main"].admission._active

    with caplog.at_level("INFO", logger="app.agent"):
        asyncio.run(scenario())
    summaries = summaries_from(caplog)
    assert len(summaries) == 1
    assert (
        summaries[0]["outcome"] == summaries[0]["error_type"] == "worker_start_failure"
    )
    assert summaries[0]["capacity"] == {
        "concurrency_limit": 1,
        "active_at_admission": 1,
    }
    assert "private thread-start failure" not in caplog.text


def test_terminal_serialization_failure_does_not_strand_consumer_or_capacity(
    controlled, monkeypatch
):
    class BrokenAnswer:
        def answer(self, *args, **kwargs):
            cyclic = []
            cyclic.append(cyclic)
            return {"answer": "invalid", "charts": cyclic}

    monkeypatch.setattr(
        controlled["main"], "_build_stats_agent", lambda *args: BrokenAnswer()
    )

    async def scenario():
        for endpoint in ["/api/agent/ask", "/api/agent/ask/stream"]:
            request = ASGIRequest(endpoint)
            await asyncio.wait_for(request.run(), 5)
            assert request.status == (502 if endpoint.endswith("ask") else 200)
            assert "failed while generating" in request.wire
            assert "invalid" not in request.wire
            await wait_workers(controlled)
            assert not controlled["main"].admission._active

    asyncio.run(scenario())


def test_logging_sink_failure_cannot_prevent_successful_terminal_or_release(
    controlled, monkeypatch
):
    from app.agent.observability import AgentTrace

    def fail(self):
        raise RuntimeError("private logging sink failure")

    monkeypatch.setattr(AgentTrace, "emit", fail)
    controlled["gate"].set()

    async def scenario():
        request = ASGIRequest("/api/agent/ask/stream")
        await asyncio.wait_for(request.run(), 5)
        assert request.wire.count("event: final") == 1
        assert "private logging" not in request.wire
        await wait_workers(controlled)
        assert not controlled["main"].admission._active

    asyncio.run(scenario())


def test_default_admission_uses_current_framework_capacity(controlled, monkeypatch):
    from dataclasses import replace
    from types import SimpleNamespace

    from app.config import get_settings

    app.dependency_overrides[get_settings] = lambda: replace(
        controlled["settings"], agent_max_concurrent_requests=None
    )
    monkeypatch.setattr(
        controlled["main"],
        "current_default_thread_limiter",
        lambda: SimpleNamespace(total_tokens=1),
    )

    async def scenario():
        held = ASGIRequest("/api/agent/ask")
        task = asyncio.create_task(held.run())
        await wait_started(controlled)
        rejected = ASGIRequest("/api/agent/ask/stream", "other")
        await rejected.run()
        assert rejected.status == 503
        controlled["gate"].set()
        await asyncio.wait_for(task, 5)

    asyncio.run(scenario())


def test_same_conversation_in_other_season_is_independently_admitted(controlled):
    from dataclasses import replace

    from app.config import get_settings

    app.dependency_overrides[get_settings] = lambda: replace(
        controlled["settings"], agent_max_concurrent_requests=2
    )

    async def scenario():
        archive = ASGIRequest("/api/agent/ask", season="2024-25")
        current = ASGIRequest("/api/agent/ask/stream", season="2025-26")
        tasks = [asyncio.create_task(r.run()) for r in (archive, current)]
        await wait_started(controlled)
        await current.meta_sent.wait()
        controlled["gate"].set()
        await asyncio.wait_for(asyncio.gather(*tasks), 5)
        assert archive.status == current.status == 200
        assert json.loads(archive.wire)["season"] == "2024-25"
        assert '"season": "2025-26"' in current.wire
        for season in ["2024-25", "2025-26"]:
            assert len(controlled["stores"][season].get_turns("capacity-chat", 6)) == 1

    asyncio.run(scenario())


def summaries_from(caplog):
    return [
        json.loads(r.message)
        for r in caplog.records
        if "agent_request_summary" in r.message
    ]


@pytest.mark.parametrize("status", [400, 502, 503])
def test_json_error_keeps_capacity_until_blocked_error_send_finishes(
    status, controlled, monkeypatch
):
    from fastapi import HTTPException

    def reject(*args):
        raise HTTPException(status, "Safe synthetic error")

    monkeypatch.setattr(controlled["main"], "_restore_agent_context", reject)

    async def scenario():
        request = ASGIRequest("/api/agent/ask")
        request.hold_send = asyncio.Event()
        task = asyncio.create_task(request.run())
        await asyncio.wait_for(request.body_sent.wait(), 5)
        assert request.status == status
        assert json.loads(request.wire) == {"detail": "Safe synthetic error"}
        assert len(controlled["main"].admission._active) == 1
        rejected = ASGIRequest("/api/agent/ask/stream", "other")
        await rejected.run()
        assert rejected.status == 503
        request.hold_send.set()
        await asyncio.wait_for(task, 5)
        await wait_workers(controlled)
        assert not controlled["main"].admission._active

    asyncio.run(scenario())


@pytest.mark.parametrize("endpoint", ["/api/agent/ask", "/api/agent/ask/stream"])
def test_nonfinite_terminal_becomes_safe_leased_error(
    endpoint, controlled, monkeypatch
):
    class NonfiniteAnswer:
        def answer(self, *args, **kwargs):
            return {"answer": "bad synthetic response", "value": float("nan")}

    monkeypatch.setattr(
        controlled["main"], "_build_stats_agent", lambda *args: NonfiniteAnswer()
    )

    async def scenario():
        request = ASGIRequest(endpoint)
        await asyncio.wait_for(request.run(), 5)
        assert request.status == (200 if endpoint.endswith("stream") else 502)
        assert "failed while generating" in request.wire
        assert "NaN" not in request.wire
        await wait_workers(controlled)
        assert not controlled["main"].admission._active

    asyncio.run(scenario())
    assert not list(
        Path(controlled["settings"].agent_history_path).parent.glob("*.jsonl")
    )


@pytest.mark.parametrize("endpoint", ["/api/agent/ask", "/api/agent/ask/stream"])
@pytest.mark.parametrize("invalid_context", ["award", "availability"])
def test_actual_restore_validation_failure_preserves_error_contract(
    endpoint, invalid_context, controlled, caplog
):
    previous = {
        "question": "Earlier summary",
        "players": [{"player_id": 7, "player_name": "Synthetic Player"}],
    }
    if invalid_context == "award":
        previous["award_reference"] = {
            "award_key": "unknown-synthetic-award",
            "season": "2024-25",
            "performance_phase": "Regular Season",
        }
    else:
        previous["availability_scope"] = {
            "player_id": 7,
            "teammate_id": 8,
            "season": "2024-25",
            "phase": "Regular Season",
            "metrics": ["pts"],
        }

    async def scenario():
        request = ASGIRequest(endpoint)
        request.payload = {
            "question": "Synthetic follow-up",
            "conversation_id": request.conversation,
            "previous_context": previous,
        }
        await asyncio.wait_for(request.run(), 5)
        assert request.status == (200 if endpoint.endswith("stream") else 400)
        assert b"x-request-id" in request.headers
        assert "could not be validated" in request.wire
        if endpoint.endswith("stream"):
            assert request.wire.count("event: error") == 1
            assert "event: final" not in request.wire
            error = json.loads(request.wire.split("event: error\ndata: ")[1].strip())
            assert error["request_id"] == request.headers[b"x-request-id"].decode()
        await wait_workers(controlled)
        assert not controlled["main"].admission._active

    with caplog.at_level("INFO", logger="app.agent"):
        asyncio.run(scenario())
    (summary,) = summaries_from(caplog)
    assert summary["error_type"] == "HTTP_400"
    assert summary["outcome"] == "error"
    assert summary["model_calls"] == 0
    assert summary["capacity"] == {
        "concurrency_limit": 1,
        "active_at_admission": 1,
        "stream_peak_bytes": 0,
        "stream_peak_events": 0,
        "stream_suppressed_events": 0,
        "stream_overflowed": False,
        "delivery_detached": False,
    }
    assert not controlled["started"].is_set()
    assert not list(
        Path(controlled["settings"].agent_history_path).parent.glob("*.jsonl")
    )
    assert "Synthetic Player" not in caplog.text


@pytest.mark.parametrize("endpoint", ["/api/agent/ask", "/api/agent/ask/stream"])
def test_restored_context_is_retained_but_failed_answer_is_not_saved(
    endpoint, controlled
):
    from app.seasons import settings_for_season

    store = InMemoryConversationStore()
    store.set_pending_clarification(
        "capacity-chat", question="Old clarification", query_plan=None
    )
    controlled["stores"]["2024-25"] = store
    controlled["fail"] = True
    controlled["gate"].set()

    async def scenario():
        request = ASGIRequest(endpoint)
        request.payload = {
            "question": "Synthetic failed follow-up",
            "conversation_id": request.conversation,
            "previous_context": {
                "question": "Previous successful summary",
                "players": [{"player_id": 7, "player_name": "Synthetic Player"}],
                "scope": {"season": "2024-25"},
                "metrics": ["pts"],
            },
        }
        await asyncio.wait_for(request.run(), 5)
        assert "failed while generating" in request.wire
        turns = store.get_turns("capacity-chat", 6)
        assert len(turns) == 1
        assert turns[0].question == "Previous successful summary"
        assert turns[0].answer == ""
        assert turns[0].context["browser_recovered"] is True
        assert store.get_pending_clarification("capacity-chat") is None
        history = Path(
            settings_for_season(controlled["settings"], "2024-25").agent_history_path
        )
        assert not history.exists()

    asyncio.run(scenario())


@pytest.mark.parametrize("scope", ["minute", "day"])
@pytest.mark.parametrize("rejection", ["capacity", "conversation"])
def test_rejected_attempts_intentionally_consume_rate_quota(
    scope, rejection, controlled, monkeypatch, caplog
):
    from dataclasses import replace

    from app import rate_limit
    from app.config import get_settings

    limiter = rate_limit.AgentRateLimiter(rate_limit.InMemoryRateLimitStore())
    monkeypatch.setattr(controlled["main"], "get_agent_rate_limiter", lambda _: limiter)
    monkeypatch.setattr(rate_limit, "time", lambda: 0)
    from datetime import UTC, datetime

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 12, tzinfo=UTC)

    monkeypatch.setattr(rate_limit, "datetime", FixedDateTime)
    app.dependency_overrides[get_settings] = lambda: replace(
        controlled["settings"],
        agent_rate_limit_per_minute=2 if scope == "minute" else 0,
        agent_rate_limit_daily=2 if scope == "day" else 0,
    )
    conv = "capacity-chat" if rejection == "conversation" else "other-active-chat"
    lease = controlled["main"].admission.acquire("2024-25", conv, 1)

    async def scenario():
        for endpoint in ["/api/agent/ask", "/api/agent/ask/stream"]:
            request = ASGIRequest(endpoint)
            await asyncio.wait_for(request.run(), 5)
            assert request.status == (409 if rejection == "conversation" else 503)
        exhausted = ASGIRequest("/api/agent/ask/stream")
        await exhausted.run()
        assert exhausted.status == 429
        assert ("daily" if scope == "day" else "rate limit") in exhausted.wire
        assert controlled["deliveries"] == []
        assert controlled["stores"] == {}

    try:
        with caplog.at_level("INFO", logger="app.agent"):
            asyncio.run(scenario())
    finally:
        lease.release()
    summaries = summaries_from(caplog)
    assert len(summaries) == 3
    assert [s["model_calls"] for s in summaries] == [0, 0, 0]
    assert summaries[-1]["error_type"] == "HTTP_429"


def test_asgi23_disconnect_during_blocked_send_closes_suspended_generator(controlled):
    async def scenario():
        request = ASGIRequest("/api/agent/ask/stream")
        request.hold_send = asyncio.Event()
        task = asyncio.create_task(request.run())
        await asyncio.wait_for(request.meta_sent.wait(), 5)
        await wait_started(controlled)
        assert not request.hold_send.is_set()
        await request.disconnect()
        await asyncio.wait_for(task, 5)
        assert controlled["deliveries"][0].detached
        assert controlled["main"].admission._active
        controlled["gate"].set()
        assert await asyncio.to_thread(controlled["finished"].wait, 5)
        await wait_workers(controlled)
        assert len(controlled["stores"]["2024-25"].get_turns("capacity-chat", 6)) == 1

    asyncio.run(asyncio.wait_for(scenario(), 10))


@pytest.mark.parametrize("fail_stream", [False, True])
def test_bursty_claude_adapter_stream_preserves_terminal_evidence_and_no_replay(
    fail_stream, controlled, monkeypatch, caplog
):
    from dataclasses import replace
    from types import SimpleNamespace as NS

    from app.agent.claude_client import ClaudeResponsesClient
    from app.agent.execution import create_response
    from app.agent.observability import AgentTrace
    from app.config import get_settings
    from app.seasons import settings_for_season

    main = controlled["main"]
    chunks = ["First ", "second ", "x" * 1000, *[" later " for _ in range(20)]]
    budget = sum(
        len(
            main._sse("answer_delta", {"type": "answer_delta", "delta": chunk}).encode()
        )
        for chunk in chunks[:2]
    )
    cfg = replace(
        controlled["settings"],
        agent_stream_buffer_bytes=budget,
        agent_stream_buffer_events=2,
    )
    app.dependency_overrides[get_settings] = lambda: cfg
    emitted = Event()
    original_emit = AgentTrace.emit

    def emit(trace):
        original_emit(trace)
        emitted.set()

    monkeypatch.setattr(AgentTrace, "emit", emit)

    class Stream:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        @property
        def text_stream(self):
            controlled["started"].set()
            assert controlled["gate"].wait(5)
            yield from chunks
            if fail_stream:
                raise ConnectionError("private streaming transport body")

        def get_final_message(self):
            return NS(content=[NS(type="text", text="".join(chunks))], usage=None)

    class SDK:
        calls = 0
        options = []

        def __init__(self):
            self.messages = NS(stream=self.stream)

        def with_options(self, **options):
            self.options.append(options)
            return self

        def stream(self, **kwargs):
            self.calls += 1
            return Stream()

    sdk = SDK()
    client = ClaudeResponsesClient(client=sdk, model="claude-synthetic")
    evidence = {
        "source_kind": "synthetic",
        "player_id": 7,
        "scope": {"season": "2024-25"},
        "value": 5,
    }

    class StreamedAgent:
        settings = cfg

        @governed_request
        def answer(
            self, question, *, conversation_id, trace, progress_callback, **kwargs
        ):
            def delta(text):
                progress_callback({"type": "answer_delta", "delta": text})

            response = create_response(
                client,
                model="claude-synthetic",
                input=[{"role": "user", "content": "Synthetic request"}],
                on_output_text_delta=delta,
            )
            main._season_conversation_store(current_season()).append_turn(
                conversation_id,
                question=question,
                answer=response.output_text,
                max_turns=6,
            )
            trace.outcome = "answered"
            return {
                "answer": response.output_text,
                "answer_streamed": True,
                "status": "ok",
                "conversation_id": conversation_id,
                "semantic_evidence": evidence,
            }

    monkeypatch.setattr(main, "_build_stats_agent", lambda *args: StreamedAgent())

    async def scenario():
        request = ASGIRequest("/api/agent/ask/stream")
        request.payload = {
            "question": "Synthetic Claude stream",
            "conversation_id": request.conversation,
            "provider": "claude",
        }
        request.hold_send = asyncio.Event()
        task = asyncio.create_task(request.run())
        await asyncio.wait_for(request.meta_sent.wait(), 5)
        await wait_started(controlled)
        controlled["gate"].set()
        assert await asyncio.to_thread(emitted.wait, 5)
        assert len(main.admission._active) == 1
        metrics = controlled["deliveries"][0].metrics()
        assert metrics == {
            "stream_peak_bytes": budget,
            "stream_peak_events": 2,
            "stream_suppressed_events": len(chunks) - 2,
            "stream_overflowed": True,
            "delivery_detached": False,
        }
        request.hold_send.set()
        await asyncio.wait_for(task, 5)
        events = [
            json.loads(block.split("\ndata: ")[1])
            for block in request.wire.strip().split("\n\n")
        ]
        deltas = [e["delta"] for e in events if e.get("type") == "answer_delta"]
        assert deltas == chunks[:2]
        assert (
            request.wire.count("event: error") + request.wire.count("event: final") == 1
        )
        turns = controlled["stores"]["2024-25"].get_turns(request.conversation, 6)
        history = Path(settings_for_season(cfg, "2024-25").agent_history_path)
        if fail_stream:
            assert events[-1]["type"] == "error"
            assert turns == [] and not history.exists()
        else:
            assert events[-1]["payload"]["answer"] == "".join(chunks)
            assert events[-1]["payload"]["semantic_evidence"] == evidence
            assert len(turns) == 1 and turns[0].answer == "".join(chunks)
            assert (
                json.loads(history.read_text())["payload"]["semantic_evidence"]
                == evidence
            )
        await wait_workers(controlled)
        assert not main.admission._active

    with caplog.at_level("INFO", logger="app.agent"):
        asyncio.run(scenario())
    (summary,) = summaries_from(caplog)
    assert summary["capacity"]["stream_peak_bytes"] == budget
    assert summary["capacity"]["stream_peak_events"] == 2
    assert summary["capacity"]["stream_suppressed_events"] == len(chunks) - 2
    assert summary["capacity"]["stream_overflowed"] is True
    assert summary["capacity"]["delivery_detached"] is False
    assert summary["capacity"]["concurrency_limit"] == 1
    assert summary["capacity"]["active_at_admission"] == 1
    assert summary["outcome"] == ("provider_failure" if fail_stream else "answered")
    assert summary["model_calls"] == sdk.calls == 1
    assert all(option["max_retries"] == 0 for option in sdk.options)
    assert "private streaming transport body" not in caplog.text
    assert "Synthetic Claude stream" not in caplog.text


@pytest.mark.parametrize("failure", ["start", "capacity"])
def test_logging_thread_exhaustion_cannot_mask_safe_capacity_failure(
    failure, controlled, monkeypatch
):
    main = controlled["main"]
    run = main.run_in_threadpool
    scheduled = []

    async def schedule(fn, *args, **kwargs):
        scheduled.append(fn.__name__)
        if fn is main._emit_agent_trace:
            raise RuntimeError("private logging thread exhaustion")
        return await run(fn, *args, **kwargs)

    monkeypatch.setattr(main, "run_in_threadpool", schedule)
    held = None
    if failure == "start":

        def fail(*args):
            raise RuntimeError("private worker thread exhaustion")

        monkeypatch.setattr(
            main, "Thread", lambda **kwargs: type("BrokenThread", (), {"start": fail})()
        )
    else:
        held = main.admission.acquire("2024-25", "held", 1)

    async def scenario():
        request = ASGIRequest("/api/agent/ask/stream")
        await asyncio.wait_for(request.run(), 5)
        assert request.status == 503
        assert b"x-request-id" in request.headers
        assert "private" not in request.wire
        assert scheduled == ["_prepare_agent_request", "_emit_agent_trace"]
        if failure == "start":
            await wait_workers(controlled)
            assert not main.admission._active
        else:
            assert main.admission._active == {("2024-25", "held")}

    try:
        asyncio.run(scenario())
    finally:
        if held:
            held.release()


@pytest.mark.parametrize("endpoint", ["/api/agent/ask", "/api/agent/ask/stream"])
@pytest.mark.parametrize("failed_answer", [False, True])
@pytest.mark.parametrize("failed_sink", [False, True])
def test_terminal_waits_for_worker_logging_and_immediate_followup_is_admitted(
    endpoint, failed_answer, failed_sink, controlled, monkeypatch
):
    from app.agent.observability import AgentTrace

    log_started, finish_log = Event(), Event()
    emit = AgentTrace.emit
    first = True

    def delayed_emit(trace):
        nonlocal first
        if first:
            first = False
            log_started.set()
            assert finish_log.wait(5), "test logging gate was not released"
            if failed_sink:
                raise RuntimeError("private logging sink failure")
        emit(trace)

    monkeypatch.setattr(AgentTrace, "emit", delayed_emit)
    controlled["fail"] = failed_answer
    controlled["gate"].set()

    async def scenario():
        request = ASGIRequest(endpoint)
        task = asyncio.create_task(request.run())
        try:
            assert await asyncio.to_thread(log_started.wait, 5)
            # Inspect the mailbox while telemetry is held: this deterministically
            # catches early terminal publication, regardless of ASGI scheduling.
            assert not controlled["deliveries"][0]._done
            assert not controlled["worker_done"][0].is_set()
            assert not task.done()
            assert controlled["main"].admission._active
            assert "event: final" not in request.wire
            assert "event: error" not in request.wire

            overlap = ASGIRequest(endpoint)
            await asyncio.wait_for(overlap.run(), 5)
            assert overlap.status == 409
            excess = ASGIRequest(endpoint, "other-conversation")
            await asyncio.wait_for(excess.run(), 5)
            assert excess.status == 503
        finally:
            finish_log.set()
            await asyncio.wait_for(task, 5)

        assert request.status == (
            502 if failed_answer and endpoint.endswith("ask") else 200
        )
        assert controlled["worker_done"][0].is_set()
        assert not controlled["main"].admission._active
        assert "private logging" not in request.wire
        if endpoint.endswith("stream"):
            terminal = "error" if failed_answer else "final"
            assert request.wire.count(f"event: {terminal}") == 1
        else:
            assert bool(json.loads(request.wire).get("detail")) == failed_answer

        # No worker wait, sleep, or retry between the returned result and the
        # next request for the same conversation, including across protocols.
        controlled["fail"] = False
        followup_endpoint = (
            "/api/agent/ask" if endpoint.endswith("stream") else "/api/agent/ask/stream"
        )
        followup = ASGIRequest(followup_endpoint)
        await asyncio.wait_for(followup.run(), 5)
        assert followup.status == 200
        assert "successful" in followup.wire
        assert not controlled["main"].admission._active

    try:
        asyncio.run(scenario())
    finally:
        finish_log.set()
