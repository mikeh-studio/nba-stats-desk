"""Private, bounded claim-rollout observations: never payloads or identifiers."""

import asyncio
import json
import logging
from threading import Lock
from time import monotonic
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

from app.agent.route_labels import LEGACY_LABELS, ROUTE_LABELS

VERSION = "ask_claim_rollout/1"

ROUTES = frozenset((*ROUTE_LABELS, *LEGACY_LABELS, "unknown"))
ENUMS = {
    "version": {VERSION},
    "origin": {"backend", "browser"},
    "kind": {"assessment", "first_invalid"},
    "route": ROUTES,
    "surface": {
        "response",
        "overview",
        "comparison",
        "comparison-chart",
        "drawer",
        "unknown",
    },
    "status": {"complete", "incomplete", "invalid"},
    "reason": {
        "none",
        "binding_mismatch",
        "serialization",
        "source_changed",
        "destination_mismatch",
        "binding_structure",
        "other",
    },
    "contract": {"ask_claim_bindings/2", "ask_ui_claim_bindings/1", "unknown"},
    "mode": {"log", "enforce"},
}
ORIGIN_ENUMS = {
    "backend": {
        "kind": {"assessment"},
        "surface": {"response"},
        "contract": {"ask_claim_bindings/2"},
        "reason": {"none", "binding_mismatch", "serialization"},
    },
    "browser": {
        "kind": {"assessment", "first_invalid"},
        "surface": {"overview", "comparison", "comparison-chart", "drawer", "unknown"},
        "contract": {"ask_ui_claim_bindings/1"},
        "reason": {
            "none",
            "source_changed",
            "destination_mismatch",
            "binding_structure",
            "other",
        },
    },
}
COUNTS = {"assessed", "invalid"}
MAX_BODY = 8192
MAX_BATCH = 16
LIMIT_PER_MINUTE = 120
_lock = Lock()
_window = 0.0
_admitted = 0
_readers = 0
MAX_READERS = 4
READ_TIMEOUT = 2.0
logger = logging.getLogger("app.claim_rollout")
logger.setLevel(logging.INFO)
logger.propagate = False
if not logger.handlers:
    logger.addHandler(logging.StreamHandler())


def project_event(event):
    """Strict allowlist: arbitrary reasons/keys never reach private logs."""
    if not isinstance(event, dict) or set(event) != set(ENUMS) | COUNTS:
        raise ValueError("Invalid claim event")
    out = {}
    for key, allowed in ENUMS.items():
        value = event[key]
        if not isinstance(value, str) or value not in allowed:
            raise ValueError("Invalid claim event enum")
        out[key] = value
    for key, allowed in ORIGIN_ENUMS[out["origin"]].items():
        if out[key] not in allowed:
            raise ValueError("Invalid origin-specific claim enum")
    for key in COUNTS:
        value = event[key]
        if type(value) is not int or not 0 <= value <= 1000000:
            raise ValueError("Invalid claim event count")
        out[key] = value
    if out["assessed"] != (out["kind"] == "assessment") or out["invalid"] not in {0, 1}:
        raise ValueError("Invalid claim event denominator")
    invalid = out["status"] == "invalid"
    if out["invalid"] != int(invalid) or (out["reason"] == "none") == invalid:
        raise ValueError("Contradictory claim event status")
    if out["kind"] == "first_invalid" and (
        out["status"] != "invalid" or out["invalid"] != 1
    ):
        raise ValueError("Invalid claim transition")
    return out


def emit_event(event):
    try:
        logger.info(
            "ask_claim_observation %s", json.dumps(project_event(event), sort_keys=True)
        )
    except Exception:
        pass  # Logging must never change delivery or conversation effects.


def backend_event(payload, report, mode, serialization_error):
    metadata = payload.get("ask_route")
    route = (
        metadata.get("route", "unknown") if isinstance(metadata, dict) else "unknown"
    )
    if not isinstance(route, str) or route not in ROUTES:
        route = "unknown"
    return dict(
        version=VERSION,
        origin="backend",
        kind="assessment",
        route=route,
        surface="response",
        status=report["status"],
        reason="serialization"
        if serialization_error
        else "binding_mismatch"
        if report["status"] == "invalid"
        else "none",
        contract="ask_claim_bindings/2",
        mode=mode,
        assessed=1,
        invalid=int(report["status"] == "invalid"),
    )


def admit():
    """Fixed process-wide budget; no client/session/address map."""
    global _window, _admitted
    with _lock:
        now = monotonic()
        if now - _window >= 60:
            _window, _admitted = now, 0
        if _admitted >= LIMIT_PER_MINUTE:
            return False
        _admitted += 1
        return True


async def ingest(request: Request, mode: str):
    # Fetch Metadata is required for browser reports; absent headers fail closed.
    if request.headers.get("sec-fetch-site") != "same-origin":
        raise HTTPException(403, "Same-origin browser report required")
    origins = request.headers.getlist("origin")
    hosts = request.headers.getlist("host")
    if len(origins) > 1 or len(hosts) != 1:
        raise HTTPException(403, "Ambiguous origin authority")
    if origins:
        try:
            origin = urlsplit(origins[0])
            authority = urlsplit("//" + hosts[0])
            if (
                origin.scheme not in {"http", "https"}
                or not origin.hostname
                or origin.username is not None
                or origin.password is not None
                or origin.path
                or origin.query
                or origin.fragment
                or authority.path
                or authority.query
                or authority.fragment
                or origin.netloc.casefold() != authority.netloc.casefold()
                or any(c.isspace() for c in origins[0] + hosts[0])
            ):
                raise ValueError("Invalid origin authority")
            origin.port  # Reject malformed ports without trusting forwarding headers.
        except ValueError:
            raise HTTPException(403, "Origin mismatch") from None
    if (
        request.headers.get("content-type", "").split(";", 1)[0].strip()
        != "application/json"
    ):
        raise HTTPException(415, "JSON required")
    if not admit():
        raise HTTPException(429, "Observation admission limit")
    global _readers
    with _lock:
        if _readers >= MAX_READERS:
            raise HTTPException(429, "Observation readers busy")
        _readers += 1
    body = bytearray()
    try:
        async with asyncio.timeout(READ_TIMEOUT):
            async for chunk in request.stream():
                if len(body) + len(chunk) > MAX_BODY:
                    raise HTTPException(413, "Observation body too large")
                body.extend(chunk)
    except TimeoutError:
        raise HTTPException(408, "Observation read timeout") from None
    finally:
        with _lock:
            _readers -= 1
    try:
        batch = json.loads(body)
        if not isinstance(batch, list) or not 1 <= len(batch) <= MAX_BATCH:
            raise ValueError("Invalid batch")
        events = [project_event(e) for e in batch]
        if any(e["origin"] != "browser" for e in events):
            raise ValueError("Invalid origin")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise HTTPException(400, "Invalid observation batch") from None
    if any(event["mode"] != mode for event in events):
        raise HTTPException(409, "Stale page mode; observation dropped")
    for event in events:
        emit_event(event)
    return {"accepted": len(events)}
