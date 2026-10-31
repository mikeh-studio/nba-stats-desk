"""Explicit local request ledger, separate from content-free service telemetry."""

from __future__ import annotations

import fcntl
import json
import os
import stat
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from time import monotonic
from uuid import uuid4

from scripts.ask_eval.costs import estimate_request
from starlette.requests import Request

from app.agent.private_traces import SECRET
from app.security import is_direct_local


def now():
    return datetime.now(timezone.utc).isoformat()


class RequestJournal:
    """One locked JSONL ledger; request content never enters ordinary loggers.

    Started/completed events share a journal ID. A start without a completion is
    visibly unfinished after process loss. Response bodies/prompts are not saved.
    """

    def __init__(self, path, *, model, prices=None):
        path = Path(path).absolute()
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError("Request journal refuses symlinks")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = path.parent.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError("Request journal directory must be private")
        self.fd = os.open(
            path, os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW, 0o600
        )
        try:
            info = os.fstat(self.fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
            ):
                raise ValueError("Request journal file must be private")
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(self.fd)
            raise
        self.model = model
        self.prices = dict(prices or {})
        self.lock = RLock()
        self.current = ContextVar("local_request_journal", default=None)
        self.write_failures = 0

    def failure(self):
        self.write_failures += 1
        try:
            os.write(2, b"request_journal_write_failed\n")
        except OSError:
            pass

    def best_effort(self, callback, *args):
        try:
            callback(*args)
        except Exception:
            self.failure()

    def append(self, event):
        try:
            raw = (
                json.dumps(
                    {"schema": "ask_request_journal/1", **event}, allow_nan=False
                )
                + "\n"
            ).encode()
            with self.lock:
                if os.write(self.fd, raw) != len(raw):
                    raise OSError("short write")
                os.fsync(self.fd)
        except Exception:
            self.failure()

    def observe(self, event, trace):
        entry = self.current.get()
        if entry is None:
            return
        with entry.lock:
            if event == "started":
                entry.expects_trace = True
                entry.request_id = trace.request_id
            elif event == "completed":
                entry.expects_trace = True
                entry.request_id = trace.request_id
                entry.summary = trace.to_log_dict()
                entry.complete_if_ready()

    def close(self):
        os.close(self.fd)


class Entry:
    def __init__(self, journal, path):
        self.journal, self.path = journal, path
        self.id, self.at, self.tick = uuid4().hex, now(), monotonic()
        self.lock = RLock()
        self.query = None
        self.query_state = "unavailable"
        self.provider, self.model = "openai", journal.model
        self.started = self.done = self.http_done = self.expects_trace = False
        self.request_id = self.summary = self.status = None
        self.transport = "sse" if path.endswith("/stream") else "json"
        self.disconnected = False

    def finish_transport(self, body):
        with self.lock:
            self.start(body)
            self.http_done = True
            self.complete_if_ready()

    def start(self, body):
        if self.started:
            return
        self.started = True
        try:
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise ValueError("object required")
            query = payload.get("question")
            if isinstance(query, str):
                self.query = SECRET.sub("[REDACTED]", query[:2000])
                self.query_state = (
                    "truncated"
                    if len(query) > 2000
                    else "recorded"
                    if self.query == query
                    else "redacted"
                )
            for field in ("provider", "model"):
                value = payload.get(field)
                if isinstance(value, str):
                    setattr(self, field, SECRET.sub("[REDACTED]", value[:80]))
        except (ValueError, TypeError, UnicodeError):
            self.query_state = "invalid_or_oversized_body"
        self.journal.append(
            {
                "event": "request_started",
                "journal_id": self.id,
                "timestamp": self.at,
                "query": self.query,
                "query_state": self.query_state,
                "provider": self.provider,
                "selected_model": self.model,
                "transport": self.transport,
            }
        )

    def complete_if_ready(self):
        if (
            self.done
            or not self.http_done
            or (self.expects_trace and self.summary is None)
        ):
            return
        self.done = True
        summary = self.summary or {}
        calls = summary.get("model_calls")
        usage_calls = summary.get("usage_reported_calls", 0)
        usage_complete = calls is not None and usage_calls == calls
        tokens = summary.get("tokens") if usage_complete else None
        prices = dict(self.journal.prices)
        # This single-model runtime never requests long-context or cache-write
        # pricing. Refuse estimates outside the explicitly supplied rate scope.
        if summary.get("tokens", {}).get("prompt", 0) > prices.get(
            "max_input_tokens", 0
        ):
            prices = {}
        costs = estimate_request(summary, prices)
        # Without trace construction, an HTTP rejection cannot have sent a model.
        if not self.expects_trace:
            calls, tokens = (
                0,
                {
                    "prompt": 0,
                    "completion": 0,
                    "total": 0,
                    "cached_input": 0,
                    "reasoning_output": 0,
                },
            )
            costs["model_estimate"] = 0.0
            usage_complete = True
        outcome = summary.get("outcome") or (
            "disconnected"
            if self.disconnected
            else "rejected"
            if (self.status or 500) >= 400
            else "unknown"
        )
        question_type = summary.get("route")
        if question_type is None and self.query:
            from app.agent.routes import semantic_shape

            question_type = semantic_shape(self.query)
        self.journal.append(
            {
                "event": "request_completed",
                "journal_id": self.id,
                "request_id": self.request_id,
                "timestamp": now(),
                "started_at": self.at,
                "query": self.query,
                "query_state": self.query_state,
                "provider": self.provider,
                "selected_model": self.model,
                "models_used": summary.get("models_used", {}),
                "model_calls": calls,
                "transport": self.transport,
                "http_status": self.status,
                "question_type": question_type or "unclassified",
                "question_type_source": "executed_route"
                if summary.get("route")
                else "inferred_from_query",
                "ask_route": summary.get("ask_route"),
                "outcome": outcome,
                "error_type": summary.get("error_type")
                or (
                    f"HTTP_{self.status}"
                    if self.status and self.status >= 400
                    else None
                ),
                "client_disconnected": self.disconnected,
                "duration_ms": round((monotonic() - self.tick) * 1000, 3),
                "processing_ms": summary.get("latency_ms"),
                "usage_complete": usage_complete,
                "tokens": tokens,
                "reported_tokens": summary.get("tokens"),
                "usage_reported_calls": usage_calls,
                "estimated_model_cost_usd": costs["model_estimate"],
                "cost_status": "estimated"
                if costs["model_estimate"] is not None
                else "unavailable_usage_or_price",
                "price_source": prices.get("source"),
                "price_as_of": prices.get("as_of"),
                "price_basis": prices,
                "cost_excludes": [
                    "warehouse",
                    "ingestion",
                    "storage",
                    "hosting",
                    "taxes",
                    "account_discounts",
                ],
            }
        )


class RequestJournalMiddleware:
    def __init__(self, app, journal):
        self.app, self.journal = app, journal

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] != "http"
            or scope["method"] != "POST"
            or scope["path"] not in {"/api/agent/ask", "/api/agent/ask/stream"}
        ):
            return await self.app(scope, receive, send)
        if not is_direct_local(Request(scope)):
            return await self.app(scope, receive, send)
        entry = Entry(self.journal, scope["path"])
        token = self.journal.current.set(entry)
        body = bytearray()
        oversized = False

        async def read():
            nonlocal oversized
            message = await receive()
            if message["type"] == "http.request":
                chunk = message.get("body", b"")
                if len(body) + len(chunk) > 32768:
                    oversized = True
                    body.clear()
                if not oversized:
                    body.extend(chunk)
                if not message.get("more_body", False):
                    self.journal.best_effort(
                        entry.start, bytes(body) if not oversized else b""
                    )
            elif message["type"] == "http.disconnect":
                entry.disconnected = True
            return message

        async def write(message):
            if message["type"] == "http.response.start":
                entry.status = message["status"]
            await send(message)

        try:
            await self.app(scope, read, write)
        except BaseException:
            entry.disconnected = True
            raise
        finally:
            self.journal.best_effort(
                entry.finish_transport, bytes(body) if not oversized else b""
            )
            self.journal.current.reset(token)
