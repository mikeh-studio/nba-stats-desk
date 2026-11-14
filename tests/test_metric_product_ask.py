"""Synthetic reader/selection fixtures; no source or production certification."""

import asyncio
import copy
import json

import pytest
from app.serving.core_application import Ask, create_core_application
from app.serving.metric_products import (
    make_core_product,
)
from app.serving.publication_contract import digest
from fastapi.testclient import TestClient
from scripts.build_synthetic_core_replay import synthetic_bundle

from test_production_publication import production as production_fixture

QUESTION = "Test Player A ratio FG% from 2026-10-20 to 2026-10-30"


@pytest.fixture
def production(tmp_path):
    return production_fixture.__wrapped__(tmp_path)


def result_row(payload):
    return payload["data"]["rows"][0]


def configure(production):
    p = production
    product = make_core_product(p.envelope["manifest"], p.envelope["binding"])
    p.app.metric_product_resolver = lambda envelope: product
    return p, product


def test_actual_json_stream_capture_and_replay(production):
    p, product = configure(production)
    with TestClient(p.app) as client:
        initial = client.post(
            "/api/agent/ask",
            json={"question": QUESTION, "metric_product": "core_box_score"},
        )
        assert initial.status_code == 200, initial.text
        answer = initial.json()
        assert answer["selection"]["product"] == product
        assert answer["selection"]["query"]["player_id"] == 101
        assert answer["selection"]["query"]["first"] == "2026-10-20"
        assert result_row(answer)["value"] == 0.2
        assert (result_row(answer)["numerator"], result_row(answer)["denominator"]) == (
            4,
            20,
        )
        replay = {"question": QUESTION, "selection": answer["selection"]}
        assert client.post("/api/agent/ask", json=replay).json() == answer
        stream = client.post("/api/agent/ask/stream", json=replay)
        assert stream.status_code == 200 and "event: final" in stream.text
        terminal = json.loads(stream.text.split("data: ")[1])
        assert terminal == answer
        # Existing callers retain exactly the old payload shape.
        old = client.post(
            "/api/agent/ask", json={"question": "Test Player A ratio FG%"}
        )
        assert old.status_code == 200 and "selection" not in old.json()


@pytest.mark.parametrize(
    "change",
    ["player", "first", "last", "question", "semantic", "identity", "binding", "extra"],
)
def test_tampered_pin_never_falls_back(production, change):
    p, _ = configure(production)
    with TestClient(p.app) as client:
        answer = client.post(
            "/api/agent/ask",
            json={"question": QUESTION, "metric_product": "core_box_score"},
        ).json()
        body = {"question": QUESTION, "selection": copy.deepcopy(answer["selection"])}
        selection = body["selection"]
        if change == "player":
            selection["query"]["player_id"] = 202
        if change == "first":
            selection["query"]["first"] = "2026-10-21"
        if change == "last":
            selection["query"]["last"] = "2026-10-29"
        if change == "question":
            body["question"] = "Test Player B ratio FG%"
        if change == "semantic":
            selection["product"]["semantic_revision"] = "0" * 64
        if change == "identity":
            selection["product"]["identity_digest"] = "0" * 64
        if change == "binding":
            selection["product"]["binding_digest"] = "0" * 64
        if change == "extra":
            selection["product"]["path"] = "synthetic-private-path"
        for route in ("/api/agent/ask", "/api/agent/ask/stream"):
            response = client.post(route, json=body)
            assert response.status_code == 409, response.text
            assert "selection" not in response.json()


def test_disabled_missing_expired_and_conflicting_pins(production):
    p, product = configure(production)
    with TestClient(p.app) as client:
        answer = client.post(
            "/api/agent/ask",
            json={"question": QUESTION, "metric_product": "core_box_score"},
        ).json()
        replay = {"question": QUESTION, "selection": answer["selection"]}
        assert (
            client.post(
                "/api/agent/ask?publication_id=pub_0000000000000000", json=replay
            ).status_code
            == 409
        )
        p.app.metric_product_resolver = None
        assert client.post("/api/agent/ask", json=replay).status_code == 409
        p.app.metric_product_resolver = lambda envelope: product
        p.clock[0] = "2026-12-01T00:00:00Z"
        assert client.post("/api/agent/ask", json=replay).status_code == 503


def test_stream_rechecks_authority_after_result_capture():
    bundle = synthetic_bundle()
    state = {"allowed": True}
    app = create_core_application(
        bundle,
        expected_digest=digest(bundle),
        clock=lambda: "2026-11-01T16:00:00Z",
        authorize=lambda *_: state["allowed"],
        metric_product=make_core_product(bundle["manifest"], bundle["binding"]),
    )
    endpoint = next(r.endpoint for r in app.routes if r.path == "/api/agent/ask/stream")
    response = endpoint(Ask(question=QUESTION, metric_product="core_box_score"))
    state["allowed"] = False

    async def collect():
        return "".join([chunk async for chunk in response.body_iterator])

    assert "event: error" in asyncio.run(collect())


def test_unselected_core_never_reads_installed_product(production):
    p = production
    calls = []

    def unavailable(envelope):
        calls.append(envelope)
        raise ValueError("synthetic unavailable product")

    p.app.metric_product_resolver = unavailable
    with TestClient(p.app) as client:
        assert client.get("/api/players/101").status_code == 200
        old = client.post(
            "/api/agent/ask", json={"question": "Test Player A ratio FG%"}
        )
        assert old.status_code == 200 and "selection" not in old.json()
        assert calls == []
        assert (
            client.post(
                "/api/agent/ask",
                json={"question": QUESTION, "metric_product": "core_box_score"},
            ).status_code
            == 503
        )
        assert len(calls) == 1


def test_same_question_promotion_pinned_stream_and_rollback(production):
    from types import SimpleNamespace

    from app.serving.core_models import build_models
    from app.serving.publication import ordered
    from app.serving.publication_contract import timestamp
    from app.serving.weekly_manifest import proposal

    p = production
    a = p.envelope
    accepted = copy.deepcopy(p.accepted)
    evidence = copy.deepcopy(p.evidence)
    accepted["appearances"][0]["fga"] = 10
    for item in evidence["inputs"]:
        if item["role"] == "selection":
            item["digest"] = digest(["synthetic revision B", accepted])
    bmodels = build_models(
        accepted,
        materialized_at=a["manifest"]["materialized_at"],
        release_scope=accepted["scope"],
    )
    b = proposal(
        accepted=accepted,
        models=bmodels,
        evidence=evidence,
        project=p.app.project,
        namespace=p.app.namespace,
        materialized_at=a["manifest"]["materialized_at"],
        generation=2,
    )
    aid, bid = a["manifest"]["publication_id"], b["manifest"]["publication_id"]
    assert aid != bid
    p.registry["validated_releases"][bid] = {
        "manifest_digest": digest(b["manifest"]),
        "binding_digest": digest(b["binding"]),
        "input_fingerprint": b["manifest"]["input_fingerprint"],
        "code_versions": b["manifest"]["versions"],
        "check_receipts": {
            c["name"]: c["receipt_digest"] for c in b["manifest"]["checks"]
        },
    }
    p.registry_path.write_text(json.dumps(p.registry))
    b_tables = {
        asset["name"]: SimpleNamespace(
            etag=asset["digest"],
            rows=ordered(asset["name"], bmodels[asset["name"]]),
            expires=timestamp(b["binding"]["expires_at"]),
        )
        for asset in b["manifest"]["assets"]
    }
    a_tables = {
        asset["name"]: p.tables[asset["relation"]] for asset in a["manifest"]["assets"]
    }
    envelopes, tables = {aid: a, bid: b}, {aid: a_tables, bid: b_tables}
    active = [aid]
    p.app.release = lambda token: envelopes[token or active[0]]
    p.app.tables = lambda manifest: tables[manifest["publication_id"]]
    p.app.metric_product_resolver = lambda envelope: make_core_product(
        envelope["manifest"], envelope["binding"]
    )
    request = {"question": QUESTION, "metric_product": "core_box_score"}
    with TestClient(p.app) as client:
        answer_a = client.post("/api/agent/ask", json=request).json()
        assert result_row(answer_a)["value"] == 0.2
        # Capture the result on A, then promote before SSE emits its final event.
        core_a = p.app.application(aid, True)
        endpoint = next(
            r.endpoint for r in core_a.routes if r.path == "/api/agent/ask/stream"
        )
        # Production endpoints are wrapped async functions with a __wrapped__ sync original.
        endpoint = getattr(endpoint, "__wrapped__", endpoint)
        response = endpoint(Ask(**request))
        active[0] = bid

        async def collect():
            return "".join([chunk async for chunk in response.body_iterator])

        terminal = json.loads(asyncio.run(collect()).split("data: ")[1])
        assert terminal == answer_a
        answer_b = client.post("/api/agent/ask", json=request).json()
        assert result_row(answer_b)["value"] == 0.16
        assert answer_b["selection"]["product"]["publication_id"] == bid
        assert (
            client.post(
                "/api/agent/ask",
                json={"question": QUESTION, "selection": answer_a["selection"]},
            ).json()
            == answer_a
        )
        active[0] = aid
        assert client.post("/api/agent/ask", json=request).json() == answer_a
        assert (
            client.post(
                "/api/agent/ask",
                json={"question": QUESTION, "selection": answer_b["selection"]},
            ).json()
            == answer_b
        )
        p.registry["revoked_publications"].append(aid)
        p.registry_path.write_text(json.dumps(p.registry))
        revoked = client.post(
            "/api/agent/ask",
            json={"question": QUESTION, "selection": answer_a["selection"]},
        )
        assert revoked.status_code == 503
