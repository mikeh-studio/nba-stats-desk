"""Synthetic rollout/privacy controls, independent of private evaluations."""

import asyncio
import io
import json
import logging
from dataclasses import replace

import pytest
from app.agent import claim_rollout as rollout
from app.agent.claims import finalize
from fastapi import HTTPException
from starlette.requests import Request
from tests.support.semantic import settings
from tests.test_claim_bindings import literal_breakdown


def event():
    return dict(
        version=rollout.VERSION,
        origin="browser",
        kind="assessment",
        route="governed_metrics",
        surface="overview",
        status="incomplete",
        reason="none",
        contract="ask_ui_claim_bindings/1",
        mode="log",
        assessed=1,
        invalid=0,
    )


@pytest.mark.parametrize("value", ["", "ENFORCE", "false", "off", "unknown"])
def test_invalid_setting_rejects(value):
    with pytest.raises(ValueError):
        replace(settings(), ask_claim_enforcement=value)


@pytest.mark.parametrize("mode", ["log", "enforce"])
def test_serialization_stays_safe_and_projection_cannot_break_delivery(
    monkeypatch, mode
):
    payload = literal_breakdown()
    payload["research"]["players"][0]["metrics"][0]["result"]["value"] = float("nan")
    result = finalize(payload, mode=mode)
    assert result["status"] == "error"
    json.dumps(result, allow_nan=False)
    monkeypatch.setattr(
        rollout,
        "backend_event",
        lambda *a: (_ for _ in ()).throw(ValueError("PRIVATE")),
    )
    result = finalize({"answer": "legacy", "ask_route": ["PRIVATE"]}, mode=mode)
    assert result["answer"] == "legacy"


def test_real_logger_projects_only_allowlisted_fields():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    rollout.logger.addHandler(handler)
    try:
        rollout.emit_event(event())
        rollout.emit_event(dict(event(), question="SECRET_PLAYER"))
        rollout.emit_event(dict(event(), reason="/private/SECRET_PATH"))
    finally:
        rollout.logger.removeHandler(handler)
    text = stream.getvalue()
    assert text.count("ask_claim_observation") == 1
    assert "SECRET" not in text
    assert rollout.logger.level == logging.INFO and not rollout.logger.propagate


def request(chunks, headers=None):
    items = iter(chunks)

    async def receive():
        chunk = next(items, None)
        return dict(type="http.request", body=chunk or b"", more_body=chunk is not None)

    base = {
        "content-type": "application/json",
        "sec-fetch-site": "same-origin",
        "host": "testserver",
    }
    base.update(headers or {})
    return Request(
        dict(
            type="http",
            method="POST",
            scheme="http",
            server=("testserver", 80),
            path="/api/agent/claim-observations",
            headers=[(k.encode(), v.encode()) for k, v in base.items()],
        ),
        receive,
    )


@pytest.fixture(autouse=True)
def fresh_budget(monkeypatch):
    monkeypatch.setattr(rollout, "_admitted", 0)
    monkeypatch.setattr(rollout, "_window", rollout.monotonic())
    monkeypatch.setattr(rollout, "_readers", 0)


def test_streamed_size_origin_batch_and_count_guards(monkeypatch):
    async def check(req, code):
        with pytest.raises(HTTPException) as caught:
            await rollout.ingest(req, "log")
        assert caught.value.status_code == code

    asyncio.run(check(request([b"a" * 4096, b"b" * 4097]), 413))
    asyncio.run(check(request([b"[]"], {"sec-fetch-site": "cross-site"}), 403))
    asyncio.run(check(request([b"[]"], {"sec-fetch-site": ""}), 403))
    asyncio.run(check(request([b"[]"], {"origin": "https://evil.invalid"}), 403))
    for payload in [
        [],
        [event()] * 17,
        [dict(event(), assessed=True)],
        [dict(event(), player_id=3)],
        [dict(event(), reason="secret")],
    ]:
        asyncio.run(check(request([json.dumps(payload).encode()]), 400))
    asyncio.run(
        check(request([json.dumps([dict(event(), mode="enforce")]).encode()]), 409)
    )
    monkeypatch.setattr(rollout, "_admitted", rollout.LIMIT_PER_MINUTE)
    asyncio.run(check(request([b"[]"]), 429))


def test_ingress_logging_failure_and_reader_limits(monkeypatch):
    monkeypatch.setattr(
        rollout.logger,
        "info",
        lambda *a: (_ for _ in ()).throw(RuntimeError("PRIVATE")),
    )
    assert asyncio.run(
        rollout.ingest(request([json.dumps([event()]).encode()]), "log")
    ) == {"accepted": 1}
    monkeypatch.setattr(rollout, "_readers", rollout.MAX_READERS)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(rollout.ingest(request([b"[]"]), "log"))
    assert caught.value.status_code == 429
    monkeypatch.setattr(rollout, "_readers", 0)
    monkeypatch.setattr(rollout, "READ_TIMEOUT", 0.001)
    req = request([])

    async def slow():
        await asyncio.sleep(1)
        return dict(type="http.request", body=b"[]", more_body=False)

    req._receive = slow
    with pytest.raises(HTTPException) as caught:
        asyncio.run(rollout.ingest(req, "log"))
    assert caught.value.status_code == 408 and rollout._readers == 0


@pytest.mark.parametrize(
    "mutation",
    [
        {"status": "invalid", "invalid": 0, "reason": "binding_mismatch"},
        {"status": "incomplete", "invalid": 1},
        {"status": "complete", "reason": "serialization"},
        {"status": "invalid", "invalid": 1, "reason": "none"},
    ],
)
def test_contradictory_observation_counts_reject(mutation):
    with pytest.raises(ValueError):
        rollout.project_event({**event(), **mutation})


def test_tls_termination_origin_uses_public_authority_not_internal_scheme():
    req = request(
        [json.dumps([event()]).encode()],
        {"origin": "https://public.example", "host": "public.example"},
    )
    assert asyncio.run(rollout.ingest(req, "log")) == {"accepted": 1}
    for origin in [
        "https://other.example",
        "null",
        "https://public.example/path",
        "https://user@public.example",
        "https://public.example:bad",
        "https://public.example#x",
    ]:
        with pytest.raises(HTTPException) as caught:
            asyncio.run(
                rollout.ingest(
                    request([b"[]"], {"origin": origin, "host": "public.example"}),
                    "log",
                )
            )
        assert caught.value.status_code == 403
    req = request(
        [b"[]"], {"origin": "https://public.example", "host": "public.example"}
    )
    req.scope["headers"].append((b"origin", b"https://public.example"))
    with pytest.raises(HTTPException) as caught:
        asyncio.run(rollout.ingest(req, "log"))
    assert caught.value.status_code == 403


@pytest.mark.parametrize("mode", ["log", "enforce"])
@pytest.mark.parametrize("kind", ["bytes", "set", "object", "recursive"])
def test_unsafe_json_has_bounded_report_and_context(mode, kind):
    payload = literal_breakdown()
    value = {"bytes": b"private", "set": {1}, "object": object(), "recursive": payload}[
        kind
    ]
    payload["unsafe"] = value
    payload["query_plan"] = {"unsafe": value}
    payload["conversation_id"] = "safe-context"
    payload["_claim_binding_error"] = "internal"
    result = finalize(payload, mode=mode)
    assert result["status"] == "error"
    assert result["conversation_id"] == "safe-context"
    assert "query_plan" not in result and "_claim_binding_error" not in result
    assert result["claim_validation"]["inventory_available"] is False
    assert len(json.dumps(result, allow_nan=False)) < 2000


@pytest.mark.parametrize(
    "mutation",
    [
        {"surface": "response"},
        {"contract": "ask_claim_bindings/2"},
        {"status": "invalid", "invalid": 1, "reason": "serialization"},
        {"status": "invalid", "invalid": 1, "reason": "binding_mismatch"},
        {"route": "overview"},
        {"bound": 1},
    ],
)
def test_browser_origin_rejects_backend_vocabulary(mutation):
    with pytest.raises(ValueError):
        rollout.project_event({**event(), **mutation})


def test_backend_rejects_browser_transition():
    e = rollout.backend_event({}, {"status": "invalid"}, "log", False)
    with pytest.raises(ValueError):
        rollout.project_event({**e, "kind": "first_invalid", "assessed": 0})


def test_internal_marker_not_exposed_in_log_mode():
    result = finalize({"answer": "legacy", "_claim_binding_error": "internal"})
    assert "_claim_binding_error" not in result


def test_browser_route_vocabulary_matches_canonical_backend():
    import re
    from pathlib import Path

    source = Path("app/static/ask_claim_rollout.js").read_text()
    literal = re.search(r"const routes = new Set\((\[.*?\])\)", source, re.S)[1]
    routes = set(re.findall(r'"([^"\n]+)"', literal))
    assert routes == rollout.ROUTES - {"unknown"}


def test_browser_surface_and_reason_vocabulary_matches_ingress():
    import re
    from pathlib import Path

    source = Path("app/static/ask_claim_rollout.js").read_text()
    for js_name, field in [("surfaces", "surface"), ("reasons", "reason")]:
        literal = re.search(
            r"const " + js_name + r" = new Set\((\[.*?\])\)", source, re.S
        )[1]
        values = set(re.findall(r'"([^"\n]+)"', literal))
        assert values == rollout.ORIGIN_ENUMS["browser"][field]
