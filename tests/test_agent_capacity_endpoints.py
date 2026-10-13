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
from tests.test_agent_service import _settings
from tests.test_api import build_client


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

    async def run(self):
        body = json.dumps(
            {
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

    monkeypatch.setattr(main, "Delivery", delivery)
    monkeypatch.setattr(main, "admission", Admission())
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
    }
    yield state
    gate.set()
    app.dependency_overrides.clear()


async def wait_started(state):
    assert await asyncio.to_thread(state["started"].wait, 5)


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
        assert "successful" in active.wire
        retry = ASGIRequest(rejected, "other-chat")
        await asyncio.wait_for(retry.run(), 5)
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
        # Completion notification need not reach the disconnected event loop.
        for _ in range(100):
            await asyncio.sleep(0)
            if not controlled["main"].admission._active:
                break
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
    controlled, monkeypatch
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
        assert not controlled["main"].admission._active

    asyncio.run(scenario())


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
