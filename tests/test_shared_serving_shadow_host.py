"""Synthetic transport acceptance; no production provider or source acceptance."""

import json
import subprocess
from dataclasses import replace
from datetime import timedelta

import pytest
from app.serving.shadow_host import _PAGE, create_shadow_app
from fastapi.testclient import TestClient

from shared_serving_fixtures import NOW
from test_two_season_composite import prepared  # noqa: F401


@pytest.fixture
def client(prepared):  # noqa: F811
    with TestClient(
        create_shadow_app(prepared[0](), evidence_kind="synthetic"),
        base_url="http://127.0.0.1",
    ) as client:
        yield client


@pytest.mark.parametrize("season", ["2025-26", "2026-27", "both"])
def test_populated_ask_sse_player_and_comparison(client, season):
    for mode, expected in (("total", 10), ("average", 5.0)):
        request = {"season": season, "player_ids": [1], "aggregation": mode}
        response = client.post("/api/ask", json=request)
        assert response.status_code == 200
        payload = response.json()
        members = payload.get("members", [payload])
        assert all(
            m["semantic_evidence"]["aggregates"][mode]["value"] == expected
            for m in members
        )
        stream = client.post("/api/ask/stream", json=request)
        assert stream.status_code == 200
        assert stream.headers["content-type"].startswith("text/event-stream")
        assert json.loads(stream.text.split("data: ")[1]) == payload
        player = client.post("/api/player", json=request)
        assert player.status_code == 200
        profiles = player.json().get("members", [player.json()])
        assert all(p["player_profile"]["observed_games"] == 3 for p in profiles)
        request["player_ids"] = [1, 2]
        comparison = client.post("/api/comparison", json=request)
        assert comparison.status_code == 200
        pairs = comparison.json().get("members", [comparison.json()])
        assert all(m["semantic_evidence"]["player_ids"] == [1, 2] for m in pairs)
        assert response.headers["cache-control"] == "no-store"


def test_unavailable_no_partial_stream_and_single_season_independence(client, prepared):  # noqa: F811
    request = {"season": "both", "player_ids": [1], "aggregation": "average"}
    prepared[1]["c"] = replace(prepared[1]["c"], allowed_snapshot_ids=frozenset())
    stream = client.post("/api/ask/stream", json=request)
    assert stream.status_code == 503 and "event: final" not in stream.text
    assert stream.json()["reason"] == "snapshot_not_allowed"
    request["season"] = "2025-26"
    assert client.post("/api/ask", json=request).status_code == 200
    prepared[1]["h"] = replace(prepared[1]["h"], valid_until=NOW - timedelta(seconds=1))
    assert client.post("/api/player", json=request).status_code == 503


def test_missing_denied_and_invalid_requests(client):
    body = {"season": "both", "player_ids": [999], "aggregation": "total"}
    assert client.post("/api/ask", json=body).json()["reason"] == "player_not_observed"
    body["player_ids"] = [3]
    assert (
        client.post("/api/ask", json=body).json()["reason"]
        == "identity_compatibility_denied"
    )
    for players in ([True], [1.0], [[1]], [1, 1], [], ["1"]):
        assert (
            client.post("/api/ask", json={**body, "player_ids": players}).status_code
            == 400
        )
    assert client.post("/api/ask", json={**body, "sql": "select 1"}).status_code == 400
    assert (
        client.post(
            "/api/ask",
            content='{"season":"both","season":"both"}',
            headers={"content-type": "application/json"},
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/ask", content=" " * 2049, headers={"content-type": "application/json"}
        ).status_code
        == 413
    )
    assert (
        client.post(
            "/api/ask", json=body, headers={"origin": "https://foreign.example"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/ask", json=body, headers={"host": "foreign.example"}
        ).status_code
        == 400
    )


def test_dedicated_page_clearly_labels_evidence_and_no_runtime_routes(client):
    page = client.get("/ask")
    assert page.status_code == 200
    assert "synthetic evidence" in page.text and "Runtime not enabled" in page.text
    assert client.get("/jobs").status_code == 404
    assert client.get("/docs").status_code == 404


def test_factory_rejects_implicit_source_or_certification_labels(prepared):  # noqa: F811
    with pytest.raises(ValueError, match="explicit_selection_required"):
        create_shadow_app(None, evidence_kind="synthetic")
    with pytest.raises(ValueError, match="evidence_label_required"):
        create_shadow_app(prepared[0](), evidence_kind="approved")


def test_rendered_members_keep_scope_clocks_and_limitations_as_text(client):
    # Execute the actual page renderer with a minimal DOM that forbids innerHTML.
    # Real HTTP payloads cover each adapter shape; independently check rendered
    # qualifiers so tables cannot silently lose their scope/readiness caveats.
    members = []
    for route, ids in (("ask", [1]), ("player", [1]), ("comparison", [1, 2])):
        response = client.post(
            "/api/" + route,
            json={"season": "both", "player_ids": ids, "aggregation": "average"},
        )
        assert response.status_code == 200
        members.extend(response.json()["members"])
    attack = '<img src=x onerror="globalThis.injected=true">'
    members[0]["limitations"].append(attack)
    page_script = _PAGE.split("<script>", 1)[1].split("</script>", 1)[0]
    harness = """
const vm=require('node:vm'), fs=require('node:fs');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
class Element {constructor(){this.children=[];this.textContent='';}append(...values){this.children.push(...values);}set innerHTML(_){throw Error('HTML injection');}}
const elements={},context={document:{createElement:()=>new Element(),getElementById:id=>elements[id]||(elements[id]=new Element())}};
vm.createContext(context);vm.runInContext(input.script,context);
function text(element){return [element.textContent,...element.children.flatMap(text)];}
const rendered=input.members.map(member=>{context.member=member;elements.results=new Element();vm.runInContext('renderMember(member)',context);return text(elements.results);});
process.stdout.write(JSON.stringify(rendered));
"""
    result = subprocess.run(
        ["node", "-e", harness],
        input=json.dumps({"script": page_script, "members": members}),
        text=True,
        capture_output=True,
        check=True,
        timeout=10,
    )
    rendered = json.loads(result.stdout)
    for member, texts in zip(members, rendered):
        scope = member["binding"]["scope"]
        evidence = member["semantic_evidence"]
        clocks = (
            evidence.get("evidence_clocks") or evidence["sides"][0]["evidence_clocks"]
        )
        for value in (
            *scope.values(),
            *clocks.values(),
            *member["authority"].values(),
            *member["limitations"],
        ):
            # The opaque status ID is provenance, not a human-facing clock.
            if value is not None and value != member["authority"]["status_id"]:
                assert value in texts
        assert "Freshness checked" in texts and "Evidence cutoff" in texts
    assert attack in rendered[0]
