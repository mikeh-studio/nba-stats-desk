import asyncio
import json
from types import SimpleNamespace

import pytest
from app.agent.observability import AgentTrace
from app.agent.request_journal import RequestJournal, RequestJournalMiddleware


def records(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def scope(path="/api/agent/ask", peer="127.0.0.1"):
    return {
        "type": "http",
        "method": "POST",
        "path": path,
        "client": (peer, 1),
        "headers": [(b"host", b"localhost")],
    }


async def run(
    app,
    body=b'{"question":"Compare Avery Able and Blake Baker"}',
    request_scope=None,
    disconnect=False,
):
    sent = []

    async def receive():
        return {"type": "http.request", "body": body}

    async def send(message):
        if disconnect and message["type"] == "http.response.body":
            raise ConnectionError("synthetic client disconnect")
        sent.append(message)

    await app(request_scope or scope(), receive, send)
    return sent


@pytest.mark.parametrize("streaming", [False, True])
def test_private_ledger_records_actual_usage_cost_and_route(tmp_path, streaming):
    path = tmp_path / "requests.jsonl"
    journal = RequestJournal(
        path,
        model="fixture",
        prices={
            "model": "fixture",
            "input_per_million": 1,
            "cached_input_per_million": 0.1,
            "output_per_million": 2,
            "max_input_tokens": 272000,
            "source": "fixture",
            "as_of": "2026-10-29",
        },
    )

    async def app(scope, receive, send):
        await receive()
        trace = AgentTrace("server-id", "private query", "fixture")
        journal.observe("started", trace)
        trace.model_calls = 1
        trace.models_used = {"fixture": 1}
        trace.add_usage(
            SimpleNamespace(
                input_tokens=100,
                output_tokens=20,
                input_tokens_details={"cached_tokens": 80},
            )
        )
        trace.route, trace.outcome = "player_comparison", "answered"
        journal.observe("completed", trace)
        await send({"type": "http.response.start", "status": 200})
        await send({"type": "http.response.body", "body": b"secret response body"})

    asyncio.run(
        run(
            RequestJournalMiddleware(app, journal),
            request_scope=scope(
                "/api/agent/ask/stream" if streaming else "/api/agent/ask"
            ),
        )
    )
    journal.close()
    start, end = records(path)
    assert start["event"] == "request_started" and end["event"] == "request_completed"
    assert start["journal_id"] == end["journal_id"]
    assert end["query"] == "Compare Avery Able and Blake Baker"
    assert (
        end["request_id"] == "server-id" and end["question_type"] == "player_comparison"
    )
    assert end["duration_ms"] >= end["processing_ms"] >= 0
    assert end["tokens"]["total"] == 120
    assert end["estimated_model_cost_usd"] == pytest.approx(68 / 1_000_000)
    assert end["transport"] == ("sse" if streaming else "json")
    assert "secret response body" not in path.read_text()
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "body",
    [
        json.dumps({"question": "test " + "sk-" + "synthetic123456789"}).encode(),
        b"{",
        b"x" * 40000,
    ],
)
def test_early_rejections_and_redaction(tmp_path, body):
    path = tmp_path / "requests.jsonl"
    journal = RequestJournal(path, model="fixture")

    async def reject(scope, receive, send):
        await receive()
        await send({"type": "http.response.start", "status": 403})
        await send({"type": "http.response.body", "body": b""})

    asyncio.run(run(RequestJournalMiddleware(reject, journal), body))
    journal.close()
    end = records(path)[-1]
    assert end["http_status"] == 403 and end["model_calls"] == 0
    assert end["estimated_model_cost_usd"] == 0 and end["outcome"] == "rejected"
    assert "synthetic123456789" not in path.read_text()


@pytest.mark.parametrize("disconnect", [False, True])
def test_late_worker_usage_survives_transport_failure_and_missing_usage_is_unknown(
    tmp_path,
    disconnect,
):
    path = tmp_path / "requests.jsonl"
    journal = RequestJournal(path, model="fixture")
    tasks = []

    async def app(scope, receive, send):
        await receive()
        trace = AgentTrace("server-id", "query", "fixture")
        journal.observe("started", trace)
        trace.model_calls = 1
        trace.models_used = {"fixture": 1}
        trace.outcome = "provider_timeout"

        async def worker():
            await asyncio.sleep(0)
            journal.observe("completed", trace)

        task = asyncio.create_task(worker())
        tasks.append(task)
        await send({"type": "http.response.start", "status": 502})
        await send({"type": "http.response.body", "body": b""})
        return task

    async def exercise():
        try:
            await run(RequestJournalMiddleware(app, journal), disconnect=disconnect)
        except ConnectionError:
            assert disconnect
        assert len(records(path)) == 1
        await tasks[0]

    asyncio.run(exercise())
    journal.close()
    end = records(path)[-1]
    assert len(records(path)) == 2
    assert end["model_calls"] == 1 and end["tokens"] is None
    assert end["estimated_model_cost_usd"] is None
    assert end["usage_complete"] is False
    assert end["client_disconnected"] is disconnect


def test_remote_requests_are_not_captured_and_existing_private_file_is_not_replaced(
    tmp_path,
):
    path = tmp_path / "requests.jsonl"
    journal = RequestJournal(path, model="fixture")

    async def app(scope, receive, send):
        await receive()
        await send({"type": "http.response.start", "status": 403})

    asyncio.run(
        run(
            RequestJournalMiddleware(app, journal),
            request_scope=scope(peer="192.0.2.1"),
        )
    )
    assert path.read_text() == ""
    with pytest.raises(BlockingIOError):
        RequestJournal(path, model="fixture")
    journal.close()
    path.chmod(0o644)
    with pytest.raises(ValueError):
        RequestJournal(path, model="fixture")


def test_sink_failure_does_not_change_http_response(tmp_path, monkeypatch):
    journal = RequestJournal(tmp_path / "requests.jsonl", model="fixture")

    def fail(*args):
        raise OSError("private storage details")

    monkeypatch.setattr(journal, "append", fail)

    async def app(scope, receive, send):
        await receive()
        await send({"type": "http.response.start", "status": 200})
        await send({"type": "http.response.body", "body": b"ok"})

    result = asyncio.run(run(RequestJournalMiddleware(app, journal)))
    journal.close()
    assert result[0]["status"] == 200 and result[-1]["body"] == b"ok"
    assert journal.write_failures > 0
