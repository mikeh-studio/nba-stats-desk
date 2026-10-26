"""Exercise the real browser producer against the public Python consumer."""

import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from app.agent import claim_rollout as rollout
from fastapi import HTTPException
from tests.test_claim_rollout import request

ROOT = Path(__file__).resolve().parents[1]
SENTINEL = "SYNTHETIC_PRIVATE_OBSERVATION_TEXT"
PRODUCER = """
import fs from 'node:fs';
const {mode, cases} = JSON.parse(fs.readFileSync(0, 'utf8'));
globalThis.document = {querySelector: () => ({content: mode})};
const events = [];
globalThis.fetch = async (_url, options) => {
  events.push(...JSON.parse(options.body));
  return {ok: true};
};
const {observeClaims, flushClaimObservations, claimObservationDrops} =
  await import('./app/static/ask_claim_rollout.js');
for (const item of cases) {
  const root = {}, payload = {ask_route: {route: item.route}, answer: item.secret};
  const session = {component: item.surface};
  observeClaims(root, payload, session, {status: item.status}, item.reason);
  flushClaimObservations();
  await new Promise(setImmediate);
  if (item.transition) {
    observeClaims(root, payload, session, {status: 'invalid'}, 'source_changed');
    flushClaimObservations();
    await new Promise(setImmediate);
  }
}
if (claimObservationDrops()) throw new Error('Unexpected dropped observations');
process.stdout.write(JSON.stringify(events));
"""


@pytest.mark.parametrize("mode", ["log", "enforce"])
def test_browser_categories_and_transitions_reach_real_consumer(monkeypatch, mode):
    def case(**overrides):
        return dict(
            dict(
                route="governed_metrics",
                surface="overview",
                status="complete",
                reason="none",
                secret=SENTINEL,
            ),
            **overrides,
        )

    # The public consumer's admitted categories must have an actual producer.
    cases = [case(route=value) for value in sorted(rollout.ROUTES)]
    browser = rollout.ORIGIN_ENUMS["browser"]
    cases += [case(surface=value) for value in sorted(browser["surface"])]
    cases += [
        case(status="invalid", reason=value)
        for value in sorted(browser["reason"] - {"none"})
    ]
    cases += [case(status="incomplete", transition=True)]
    expected = [
        {key: item[key] for key in ("route", "surface", "status", "reason")}
        for item in cases
    ] + [
        dict(
            route="governed_metrics",
            surface="overview",
            status="invalid",
            reason="source_changed",
        )
    ]
    # Raw content and backend-only categories are normalized before transport.
    for reason in (SENTINEL, "binding_mismatch", "serialization", "none"):
        cases.append(
            case(route=SENTINEL, surface="response", status="invalid", reason=reason)
        )
        expected.append(
            dict(route="unknown", surface="unknown", status="invalid", reason="other")
        )
    node = shutil.which("node")
    assert node, (
        "Full pytest needs Node.js, matching the public CI JavaScript toolchain"
    )
    completed = subprocess.run(
        [node, "--input-type=module", "-e", PRODUCER],
        input=json.dumps(dict(mode=mode, cases=cases)),
        text=True,
        capture_output=True,
        check=False,
        cwd=ROOT,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stderr
    events = json.loads(completed.stdout)
    assert len(events) == len(expected)
    assert SENTINEL not in completed.stdout
    for event, wanted in zip(events, expected, strict=True):
        assert rollout.project_event(event) == event
        assert {key: event[key] for key in wanted} == wanted
        assert event["mode"] == mode
    assert {e["route"] for e in events} == rollout.ROUTES
    for key, values in browser.items():
        assert {e[key] for e in events} == values
    assert {e["status"] for e in events} == rollout.ENUMS["status"]

    monkeypatch.setattr(rollout, "_admitted", 0)
    monkeypatch.setattr(rollout, "_window", rollout.monotonic())
    received = []
    monkeypatch.setattr(rollout, "emit_event", received.append)
    for offset in range(0, len(events), rollout.MAX_BATCH):
        batch = events[offset : offset + rollout.MAX_BATCH]
        result = asyncio.run(
            rollout.ingest(request([json.dumps(batch).encode()]), mode)
        )
        assert result == {"accepted": len(batch)}
    assert received == events
    for update in (
        {"answer": SENTINEL},
        {"reason": SENTINEL},
        {"surface": "response"},
        {"contract": "ask_claim_bindings/2"},
        {"reason": "binding_mismatch"},
    ):
        invalid = {**events[0], **update}
        with pytest.raises(HTTPException) as caught:
            asyncio.run(rollout.ingest(request([json.dumps([invalid]).encode()]), mode))
        assert caught.value.status_code == 400
    mixed = [events[0], {**events[1], "reason": SENTINEL}]
    with pytest.raises(HTTPException) as caught:
        asyncio.run(rollout.ingest(request([json.dumps(mixed).encode()]), mode))
    assert caught.value.status_code == 400
    backend = rollout.backend_event(
        {"ask_route": {"route": "governed_metrics"}},
        {"status": "complete"},
        mode,
        False,
    )
    assert rollout.project_event(backend) == backend
    stale = {**events[0], "mode": "enforce" if mode == "log" else "log"}
    for invalid, status in ((backend, 400), (stale, 409)):
        with pytest.raises(HTTPException) as caught:
            asyncio.run(rollout.ingest(request([json.dumps([invalid]).encode()]), mode))
        assert caught.value.status_code == status
    assert received == events  # Even a valid prefix of a rejected batch is not emitted.
