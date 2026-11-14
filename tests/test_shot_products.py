"""Entirely synthetic optional shot product and public Ask integration."""

import asyncio
import copy
import json

import pytest
from app.serving.core_application import Ask
from app.serving.metric_products import make_core_product
from app.serving.publication_contract import digest
from app.serving.shot_products import (
    make_shot_product,
    read_shot_product,
    validate_shot_product,
)
from fastapi.testclient import TestClient
from starlette.requests import Request

from test_production_publication import production as production_fixture

QUESTION = "What share of Test Player A’s observed attempts were contested from 2026-10-20 to 2026-10-30?"


@pytest.fixture
def production(tmp_path):
    return production_fixture.__wrapped__(tmp_path)


def artifact_for(p, unknown=False, contested=1):
    games = sorted(p.accepted["games"], key=lambda r: r["game_id"])
    coverage = [
        {
            "game_id": g["game_id"],
            "game_date": g["game_date"],
            "status": "unknown" if unknown and i else "complete",
            "expected_events": None if unknown and i else 1 if i == 0 else 9,
        }
        for i, g in enumerate(games)
    ]
    rows = []
    for i, g in enumerate(games):
        if unknown and i:
            continue
        for pid in [101, 202]:
            rows.append(
                {
                    "game_id": g["game_id"],
                    "player_id": pid,
                    "observed_attempts": (1 if i == 0 else 9) if pid == 101 else 0,
                    "contested_attempts": contested if i == 0 and pid == 101 else 0,
                }
            )
    return make_shot_product(
        make_core_product(p.envelope["manifest"], p.envelope["binding"]),
        coverage=coverage,
        rows=rows,
        expires_at=p.envelope["binding"]["expires_at"],
        source_revision=digest(["synthetic events", contested, unknown]),
        inventory_revision=digest(["synthetic inventory", contested, unknown]),
        source_through=max(
            c["game_date"] for c in coverage if c["status"] == "complete"
        ),
    )


def install(p, artifact):
    calls = []
    allowed = [True]
    p.app.shot_product_resolver = lambda envelope, product_id: (
        artifact
        if product_id in (None, artifact["product"]["product_id"])
        else (_ for _ in ()).throw(ValueError("missing shot pin"))
    )

    def authorize(product):
        calls.append(product)
        return allowed[0] and product == artifact["product"]

    p.app.shot_authorize = authorize
    return calls, allowed


def test_real_shot_json_sse_percent_components_and_replay(production):
    p = production
    artifact = artifact_for(p)
    calls, _ = install(p, artifact)
    with TestClient(p.app) as client:
        response = client.post(
            "/api/agent/ask",
            json={"question": QUESTION, "metric_product": "shot_events"},
        )
        assert response.status_code == 200, response.text
        result = response.json()
        row = result["data"]["rows"][0]
        assert (
            row["value"],
            row["display_value"],
            row["numerator"],
            row["denominator"],
        ) == (0.1, 10, 1, 10)
        assert result["data"]["coverage"] == {
            "eligible_games": 2,
            "covered_games": 2,
            "unknown_games": 0,
            "unknown_game_ids": [],
        }
        assert "10%" in result["answer"] and "2 of 2 eligible" in result["answer"]
        replay = {"question": QUESTION, "selection": result["selection"]}
        stream = client.post("/api/agent/ask/stream", json=replay)
        assert stream.status_code == 200 and "event: final" in stream.text
        assert json.loads(stream.text.split("data: ")[1]) == result
        assert len(calls) >= 4
        zero = client.post(
            "/api/agent/ask",
            json={
                "question": "Test Player B ratio contested_attempt_share",
                "metric_product": "shot_events",
            },
        ).json()
        assert zero["data"]["rows"][0]["denominator"] == 0
        assert zero["data"]["rows"][0]["value"] is None
        assert (
            "unavailable" in zero["answer"]
            and zero["data"]["coverage"]["covered_games"] == 2
        )


def test_partial_unknown_is_disclosed_and_no_unobserved_zero(production):
    p = production
    install(p, artifact_for(p, unknown=True))
    with TestClient(p.app) as client:
        result = client.post(
            "/api/agent/ask",
            json={"question": QUESTION, "metric_product": "shot_events"},
        ).json()
        row = result["data"]["rows"][0]
        assert (
            row["value"],
            row["observed_games"],
            row["valid_games"],
            row["missing_component_games"],
        ) == (1, 2, 1, 1)
        assert row["status"] == "partial" and "1 of 2 eligible" in result["answer"]
        day = next(
            c["game_date"]
            for c in artifact_for(p, unknown=True)["coverage"]
            if c["status"] == "unknown"
        )
        unknown = client.post(
            "/api/agent/ask",
            json={
                "question": f"Test Player A ratio contested_attempt_share from {day} to {day}",
                "metric_product": "shot_events",
            },
        ).json()
        assert unknown["data"]["rows"][0]["value"] is None
        assert unknown["data"]["rows"][0]["denominator"] is None
        assert unknown["data"]["coverage"]["covered_games"] == 0


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown_rows",
        "bad_date",
        "bad_identity",
        "missing_pair",
        "bad_count",
        "future_semantics",
        "tampered_bytes",
        "wrong_core",
    ],
)
def test_shot_validation_fails_closed(production, mutation):
    p = production
    artifact = artifact_for(p)
    if mutation == "unknown_rows":
        artifact["coverage"][0]["status"] = "unknown"
        artifact["coverage"][0]["expected_events"] = None
    if mutation == "bad_date":
        artifact["coverage"][0]["game_date"] = "2026-10-31"
    if mutation == "bad_identity":
        artifact["rows"][0]["player_id"] = 999
    if mutation == "missing_pair":
        artifact["rows"].pop()
    if mutation == "bad_count":
        artifact["rows"][0]["contested_attempts"] = 2
    if mutation == "future_semantics":
        artifact["product"]["semantic_revision"] = "0" * 64
    if mutation == "tampered_bytes":
        artifact["rows"][0]["contested_attempts"] = 0
    if mutation == "wrong_core":
        artifact["product"]["core_reference"]["identity_digest"] = "0" * 64
    install(p, artifact)
    with TestClient(p.app) as client:
        response = client.post(
            "/api/agent/ask",
            json={"question": QUESTION, "metric_product": "shot_events"},
        )
        assert response.status_code == 503 and "numerator" not in response.text


def test_shot_request_and_terminal_recheck_both_authorities(production):
    p = production
    artifact = artifact_for(p)
    _, allowed = install(p, artifact)
    core = p.app.application(p.envelope["manifest"]["publication_id"], "shot_events")
    endpoint = next(
        r.endpoint for r in core.routes if r.path == "/api/agent/ask/stream"
    ).__wrapped__
    response = endpoint(
        Ask(question=QUESTION, metric_product="shot_events"),
        Request({"type": "http", "state": {}}),
    )
    allowed[0] = False

    async def collect():
        return "".join([chunk async for chunk in response.body_iterator])

    assert (
        asyncio.run(collect())
        == 'event: error\ndata: {"detail":"authority unavailable"}\n\n'
    )
    allowed[0] = True
    with TestClient(p.app) as client:
        p.registry["revoked_publications"].append(
            p.envelope["manifest"]["publication_id"]
        )
        p.registry_path.write_text(json.dumps(p.registry))
        response = client.post(
            "/api/agent/ask",
            json={"question": QUESTION, "metric_product": "shot_events"},
        )
        assert response.status_code == 503 and "numerator" not in response.text


def test_core_queries_never_load_optional_shots(production):
    p = production

    def forbidden(*_):
        raise AssertionError("core-only request read shots")

    p.app.shot_product_resolver = forbidden
    p.app.shot_authorize = forbidden
    p.app.metric_product_resolver = lambda envelope: make_core_product(
        envelope["manifest"], envelope["binding"]
    )
    with TestClient(p.app) as client:
        assert client.get("/api/players/101").status_code == 200
        for product in (None, "core_box_score"):
            payload = {"question": "Test Player A ratio FG%"}
            if product:
                payload["metric_product"] = product
            assert client.post("/api/agent/ask", json=payload).status_code == 200


def test_real_file_registry_promotion_failure_replay_and_rollback(production, tmp_path):
    p = production
    one, two = artifact_for(p), artifact_for(p, contested=0)
    path = tmp_path / "shots.json"
    registry = {
        "version": "shot-product-registry/1",
        "active_product_id": one["product"]["product_id"],
        "products": [one],
    }
    path.write_text(json.dumps(registry))
    p.app.shot_product_resolver = lambda envelope, product_id: read_shot_product(
        json.loads(path.read_text()), envelope, product_id
    )
    p.app.shot_authorize = lambda product: product in [one["product"], two["product"]]
    request = {"question": QUESTION, "metric_product": "shot_events"}
    with TestClient(p.app) as client:
        a = client.post("/api/agent/ask", json=request).json()
        # An invalid refresh is rejected before the registry's active pointer changes.
        bad = copy.deepcopy(two)
        bad["rows"][0]["contested_attempts"] = 99
        with pytest.raises(ValueError):
            validate_shot_product(
                bad,
                p.envelope["manifest"],
                p.envelope["binding"],
                {
                    a["name"]: p.tables[a["relation"]].rows
                    for a in p.envelope["manifest"]["assets"]
                },
            )
        assert client.post("/api/agent/ask", json=request).json() == a
        core = p.app.application(
            p.envelope["manifest"]["publication_id"],
            "shot_events",
            one["product"]["product_id"],
        )
        endpoint = next(
            r.endpoint for r in core.routes if r.path == "/api/agent/ask/stream"
        ).__wrapped__
        captured = endpoint(Ask(**request), Request({"type": "http", "state": {}}))
        registry["products"].append(two)
        registry["active_product_id"] = two["product"]["product_id"]
        path.write_text(json.dumps(registry))

        async def collect_captured():
            return "".join([chunk async for chunk in captured.body_iterator])

        assert json.loads(asyncio.run(collect_captured()).split("data: ")[1]) == a
        b = client.post("/api/agent/ask", json=request).json()
        assert (
            b["data"]["rows"][0]["value"] == 0 and a["data"]["rows"][0]["value"] == 0.1
        )
        assert (
            client.post(
                "/api/agent/ask",
                json={"question": QUESTION, "selection": a["selection"]},
            ).json()
            == a
        )
        registry["active_product_id"] = one["product"]["product_id"]
        path.write_text(json.dumps(registry))
        assert client.post("/api/agent/ask", json=request).json() == a
        assert (
            client.post(
                "/api/agent/ask/stream",
                json={"question": QUESTION, "selection": b["selection"]},
            ).status_code
            == 200
        )
        registry["products"] = [one]
        path.write_text(json.dumps(registry))
        assert (
            client.post(
                "/api/agent/ask",
                json={"question": QUESTION, "selection": b["selection"]},
            ).status_code
            == 503
        )


def test_active_core_advances_incompatible_shots_unavailable_retained_pair_works(
    production,
):
    from app.serving.weekly_manifest import proposal

    p = production
    one = artifact_for(p)
    registry = {
        "version": "shot-product-registry/1",
        "active_product_id": one["product"]["product_id"],
        "products": [one],
    }
    p.app.shot_product_resolver = lambda envelope, pid: read_shot_product(
        registry, envelope, pid
    )
    p.app.shot_authorize = lambda product: product == one["product"]
    a = p.envelope
    evidence = copy.deepcopy(p.evidence)
    for item in evidence["inputs"]:
        if item["role"] == "selection":
            item["digest"] = digest("synthetic new core selection")
    models = {
        asset["name"]: p.tables[asset["relation"]].rows
        for asset in a["manifest"]["assets"]
    }
    b = proposal(
        accepted=p.accepted,
        models=models,
        evidence=evidence,
        project=p.app.project,
        namespace=p.app.namespace,
        materialized_at=a["manifest"]["materialized_at"],
        generation=2,
    )
    aid, bid = a["manifest"]["publication_id"], b["manifest"]["publication_id"]
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
    active = [aid]
    p.app.release = lambda token: {aid: a, bid: b}[token or active[0]]
    tables = {
        asset["name"]: p.tables[asset["relation"]] for asset in a["manifest"]["assets"]
    }
    p.app.tables = lambda _: tables
    with TestClient(p.app) as client:
        request = {"question": QUESTION, "metric_product": "shot_events"}
        old = client.post("/api/agent/ask", json=request).json()
        active[0] = bid
        assert client.get("/api/players/101").json()["publication_id"] == bid
        for route in ["/api/agent/ask", "/api/agent/ask/stream"]:
            invalid = client.post(route, json=request)
            assert (
                invalid.status_code == 503
                and "coverage" not in invalid.text
                and "numerator" not in invalid.text
            )
            retained = client.post(
                route, json={"question": QUESTION, "selection": old["selection"]}
            )
            assert retained.status_code == 200
            assert (
                retained.json()
                if route.endswith("ask")
                else json.loads(retained.text.split("data: ")[1])
            ) == old


def test_shot_expiry_at_final_emits_no_numeric_result(production):
    p = production
    artifact = artifact_for(p)
    descriptor = artifact["product"]
    artifact = make_shot_product(
        descriptor["core_reference"],
        coverage=artifact["coverage"],
        rows=artifact["rows"],
        expires_at="2026-11-01T17:00:00Z",
        **{
            k: descriptor[k]
            for k in [
                "source_revision",
                "inventory_revision",
                "source_through",
                "source_version",
                "inventory_version",
            ]
        },
    )
    install(p, artifact)
    core = p.app.application(p.envelope["manifest"]["publication_id"], "shot_events")
    endpoint = next(
        r.endpoint for r in core.routes if r.path == "/api/agent/ask/stream"
    ).__wrapped__
    response = endpoint(
        Ask(question=QUESTION, metric_product="shot_events"),
        Request({"type": "http", "state": {}}),
    )
    p.clock[0] = artifact["product"]["expires_at"]

    async def collect():
        return "".join([chunk async for chunk in response.body_iterator])

    assert (
        asyncio.run(collect())
        == 'event: error\ndata: {"detail":"authority unavailable"}\n\n'
    )
