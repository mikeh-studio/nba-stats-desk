"""Synthetic main-route composition; never live authorization or provider proof."""

import asyncio
import json
import socket
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from app import main
from app.config import Settings
from app.rate_limit import InMemoryRateLimitStore
from app.serving.candidate_application import create_candidate_application
from app.serving.candidate_service import CandidateBinding, CandidatePTSService
from app.serving.contract import ServingError, content_digest
from fastapi.testclient import TestClient

from test_current_serving_snapshot import current  # noqa: F401
from test_two_season_composite import prepared  # noqa: F401


@pytest.fixture(autouse=True)
def isolated_candidate_rate_limit(monkeypatch):
    """Do not share request counts with ordinary routes in the pytest process."""
    monkeypatch.setattr("app.rate_limit._memory_store", InMemoryRateLimitStore())


@pytest.fixture
def candidate_client(current, monkeypatch):  # noqa: F811
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.setattr(
        socket.socket, "connect", lambda *a, **k: pytest.fail("network attempted")
    )
    monkeypatch.setattr(main.app, "middleware_stack", None)
    monkeypatch.setattr(main, "get_settings", main.get_settings)
    main.app.dependency_overrides.clear()
    main.app.state._state.pop("offline_candidate_claimed", None)
    settings = Settings(
        project_id="synthetic",
        gold_dataset="synthetic",
        metadata_dataset="synthetic",
        freshness_threshold_hours=1,
        max_search_results=5,
        performance_cache_prewarm_enabled=False,
    )
    binding = CandidateBinding(
        journal_id=content_digest("journal"),
        generation=1,
        record_id=content_digest("record"),
        candidate_id=content_digest("candidate"),
        code_id=content_digest("code"),
        catalog_id=content_digest(((1, "Alex Example"), (2, "Blair Sample"))),
        status_id=content_digest("status"),
        status_revision=1,
        snapshot_ids=(current[-1].binding["snapshot_id"],),
        context_ids=(current[-1].binding["context_id"],),
        selection_id=None,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    active = {"binding": binding}
    service = CandidatePTSService(
        current[-1],
        binding=binding,
        check_binding=lambda: active["binding"],
        players=((1, "Alex Example"), (2, "Blair Sample")),
        evidence_kind="synthetic",
    )
    monkeypatch.setattr(
        main, "_cached_repository", lambda *_: pytest.fail("warehouse constructed")
    )
    application = create_candidate_application(service, settings=settings)
    try:
        with TestClient(
            application, base_url="http://127.0.0.1", client=("127.0.0.1", 43210)
        ) as client:
            yield client, service, active
    finally:
        main.app.dependency_overrides.clear()
        main.app.state._state.pop("offline_candidate_claimed", None)


QUESTION = "Alex Example points per game in the 2026-27 regular season"


def test_actual_ask_json_and_sse_use_shared_pts(candidate_client):
    client, _, _ = candidate_client
    response = client.post("/api/agent/ask", json={"question": QUESTION})
    assert response.status_code == 200
    result = response.json()
    assert result["runtime_eligible"] is False
    assert result["evidence_kind"] == "synthetic"
    assert result["binding_expires_at"]
    assert result["tables"][0]["rows"] == [[1, 5.0, 3, 2, 1]]
    assert (
        result["candidate_evidence"][0]["semantic_evidence"]["aggregates"]["total"][
            "numerator"
        ]
        == 10
    )
    stream = client.post("/api/agent/ask/stream", json={"question": QUESTION})
    assert stream.status_code == 200
    assert "event: final" in stream.text and "event: answer_delta" not in stream.text
    assert "5.0" in stream.text
    page = client.get("/ask")
    assert page.status_code == 200 and "Offline PTS candidate" in page.text


@pytest.mark.parametrize("wording", ["total points", "points total"])
def test_candidate_total_points_uses_sum_and_preserves_scope(candidate_client, wording):
    client, _, _ = candidate_client
    question = f"Alex Example {wording} in the 2026-27 regular season"
    response = client.post("/api/agent/ask", json={"question": question})
    assert response.status_code == 200
    assert response.json()["tables"][0]["rows"] == [[1, 10, 3, 2, 1]]
    unsupported = client.post(
        "/api/agent/ask", json={"question": question + " only fourth quarters"}
    )
    assert unsupported.status_code != 200


def test_actual_player_and_compare_routes_are_scoped_and_truthful(candidate_client):
    client, _, _ = candidate_client
    player = client.get("/api/players/1?season=2026-27")
    assert player.status_code == 200
    assert player.json()["player_profile"]["total_pts"] == 10
    assert player.headers["X-Offline-Evidence-Kind"] == "synthetic"
    assert client.get("/api/players/1").status_code == 400
    assert client.get("/api/players/1?season=2026-27&metric=reb").status_code == 400
    assert client.get("/players/1?season=2026-27&metric=reb").status_code == 400
    page = client.get("/players/1?season=2026-27")
    assert page.status_code == 200 and "Observed appearances" in page.text
    snapshot_id = player.json()["binding"]["snapshot_id"]
    assert snapshot_id in page.text and 'href="/ask"' in page.text
    for item in (
        "synthetic",
        "espn",
        "Source captured",
        "Freshness checked",
        "Status checked",
        "Binding expires",
    ):
        assert item in page.text
    assert "Revalidated" not in page.text
    query = (
        "player_a_id=1&player_b_id=2&season=2026-27&window=regular_season&focus=scoring"
    )
    comparison = client.get("/api/compare?" + query)
    assert comparison.status_code == 200
    assert comparison.json()["tables"][0]["rows"][0][1] == 5.0
    compare_page = client.get("/compare?" + query)
    assert compare_page.status_code == 200
    assert comparison.json()["binding"]["snapshot_id"] == snapshot_id
    assert snapshot_id in compare_page.text and 'href="/ask"' in compare_page.text
    assert "synthetic" in compare_page.text and "espn" in compare_page.text
    assert (
        client.get(
            "/api/compare?player_a_id=1&player_b_id=2&season=2026-27"
        ).status_code
        == 400
    )
    assert client.get("/api/players/999?season=2026-27").status_code == 503


def test_candidate_refuses_unsupported_and_authority_changes(candidate_client):
    client, _, active = candidate_client
    for question in (
        "Alex Example rebounds per game in the 2026-27 regular season",
        "Who leads the league in points in the 2026-27 regular season?",
        "Alex Example points per game in the 2025-26 regular season",
    ):
        assert (
            client.post("/api/agent/ask", json={"question": question}).status_code
            != 200
        )
    assert (
        client.post(
            "/api/agent/ask", json={"question": QUESTION, "provider": "openai"}
        ).status_code
        == 400
    )
    assert client.get("/api/rankings").status_code == 403
    assert (
        client.post(
            "/api/agent/ask?season=2025-26", json={"question": QUESTION}
        ).status_code
        == 400
    )
    active["binding"] = replace(active["binding"], generation=2)
    assert client.get("/api/players/1?season=2026-27").status_code == 503
    assert client.post("/api/agent/ask", json={"question": QUESTION}).status_code == 503


@pytest.mark.parametrize("path", ["/api/agent/ask", "/api/agent/ask/stream"])
def test_generation_change_after_serialization_refuses_terminal(
    candidate_client, monkeypatch, path
):
    client, _, active = candidate_client
    original = main._candidate_answer_seasons

    def rotate(wire):
        seasons = original(wire)
        active["binding"] = replace(active["binding"], generation=2)
        return seasons

    monkeypatch.setattr(main, "_candidate_answer_seasons", rotate)
    response = client.post(path, json={"question": QUESTION})
    if path.endswith("stream"):
        assert "event: error" in response.text
        assert "event: final" not in response.text and "5.0" not in response.text
    else:
        assert response.status_code == 503
        assert response.json()["reason"] == "candidate_authority_changed"
        assert "5.0" not in response.text


def test_generation_change_after_player_calculation_refuses_body(
    candidate_client, monkeypatch
):
    client, service, active = candidate_client
    original = service.player

    def rotate(*args):
        result = original(*args)
        active["binding"] = replace(active["binding"], generation=2)
        return result

    monkeypatch.setattr(service, "player", rotate)
    response = client.get("/api/players/1?season=2026-27")
    assert response.status_code == 503
    assert response.json()["reason"] == "candidate_authority_changed"
    assert "total_pts" not in response.text


def test_generation_change_after_comparison_calculation_refuses_body(
    candidate_client, monkeypatch
):
    client, service, active = candidate_client
    original = service.compare

    def rotate(*args):
        result = original(*args)
        active["binding"] = replace(active["binding"], generation=2)
        return result

    monkeypatch.setattr(service, "compare", rotate)
    query = (
        "player_a_id=1&player_b_id=2&season=2026-27&window=regular_season&focus=scoring"
    )
    response = client.get("/api/compare?" + query)
    assert response.status_code == 503
    assert response.json()["reason"] == "candidate_authority_changed"
    assert "tables" not in response.text


def test_two_season_real_ask_is_side_by_side_and_refuses_missing_member(
    prepared,  # noqa: F811
    monkeypatch,
):
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.setattr(main.app, "middleware_stack", None)
    monkeypatch.setattr(main, "get_settings", main.get_settings)
    main.app.dependency_overrides.clear()
    main.app.state._state.pop("offline_candidate_claimed", None)
    selection = prepared[0]()
    names = ((1, "Alex Example"), (2, "Blair Sample"))
    binding = CandidateBinding(
        journal_id=content_digest("journal-both"),
        generation=1,
        record_id=content_digest("record-both"),
        candidate_id=content_digest("candidate-both"),
        code_id=content_digest("code-both"),
        catalog_id=content_digest(names),
        status_id=content_digest("status-both"),
        status_revision=1,
        snapshot_ids=tuple(c.binding["snapshot_id"] for c in selection._contexts),
        context_ids=tuple(c.binding["context_id"] for c in selection._contexts),
        selection_id=selection.binding["selection_id"],
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    service = CandidatePTSService(
        selection,
        binding=binding,
        check_binding=lambda: binding,
        players=names,
        evidence_kind="synthetic",
    )
    settings = Settings(
        project_id="synthetic",
        gold_dataset="synthetic",
        metadata_dataset="synthetic",
        freshness_threshold_hours=1,
        max_search_results=5,
        performance_cache_prewarm_enabled=False,
    )
    monkeypatch.setattr(
        main, "_cached_repository", lambda *_: pytest.fail("warehouse constructed")
    )
    application = create_candidate_application(service, settings=settings)
    try:
        with TestClient(
            application, base_url="http://127.0.0.1", client=("127.0.0.1", 43210)
        ) as client:
            question = "Compare Alex Example's points per game in the 2026-27 regular season vs the 2025-26 regular season"
            answer = client.post("/api/agent/ask", json={"question": question})
            assert answer.status_code == 200
            members = answer.json()["candidate_evidence"]
            assert [m["binding"]["scope"]["season"] for m in members] == [
                "2025-26",
                "2026-27",
            ]
            assert all(m["tables"][0]["rows"][0][1] == 5.0 for m in members)
            prepared[1]["h"] = replace(
                prepared[1]["h"], allowed_snapshot_ids=frozenset()
            )
            denied = client.post("/api/agent/ask", json={"question": question})
            assert denied.status_code == 503 and "5.0" not in denied.text
            current = client.get("/api/players/1?season=2026-27")
            assert current.status_code == 200
    finally:
        main.app.dependency_overrides.clear()
        main.app.state._state.pop("offline_candidate_claimed", None)


@pytest.mark.parametrize("path", ["/api/agent/ask", "/api/agent/ask/stream"])
def test_source_status_change_after_ask_serialization_refuses_result(
    candidate_client,
    current,  # noqa: F811
    monkeypatch,
    path,
):
    client, _, _ = candidate_client
    live = current[2]
    original = main._candidate_answer_seasons

    def rotate(wire):
        seasons = original(wire)
        live["status"] = replace(live["status"], status_id=content_digest("new-status"))
        return seasons

    monkeypatch.setattr(main, "_candidate_answer_seasons", rotate)
    result = client.post(path, json={"question": QUESTION})
    if path.endswith("stream"):
        assert "event: error" in result.text
        assert "event: final" not in result.text
    else:
        assert result.status_code == 503
    assert "5.0" not in result.text


@pytest.mark.parametrize(
    ("method", "path", "query"),
    [
        ("GET", "/api/players/1", "season=2026-27"),
        ("GET", "/players/1", "season=2026-27"),
        ("POST", "/api/agent/ask", ""),
        (
            "GET",
            "/api/compare",
            "player_a_id=1&player_b_id=2&season=2026-27&window=regular_season&focus=scoring",
        ),
        (
            "GET",
            "/compare",
            "player_a_id=1&player_b_id=2&season=2026-27&window=regular_season&focus=scoring",
        ),
    ],
)
def test_authority_rotation_after_response_start_aborts_body(
    candidate_client, method, path, query
):
    client, _, active = candidate_client
    messages = []
    body = json.dumps({"question": QUESTION}).encode() if method == "POST" else b""
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "root_path": "",
        "headers": [(b"host", b"127.0.0.1"), (b"content-type", b"application/json")],
        "client": ("127.0.0.1", 43210),
        "server": ("127.0.0.1", 80),
    }
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.Event().wait()

    async def send(message):
        messages.append(message)
        if message["type"] == "http.response.start":
            active["binding"] = replace(active["binding"], generation=2)
            await asyncio.sleep(0)

    async def invoke():
        with pytest.raises(Exception, match="candidate_authority_changed"):
            await client.app(scope, receive, send)

    asyncio.run(invoke())
    assert [message["type"] for message in messages] == ["http.response.start"]
    assert messages[0]["status"] == 200


def test_candidate_post_body_is_bounded_and_closed(candidate_client):
    client, _, _ = candidate_client
    assert (
        client.post(
            "/api/agent/ask", content=b'{"question":"x","question":"y"}'
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/agent/ask", json={"question": QUESTION, "provider": None}
        ).status_code
        == 400
    )
    assert client.post("/api/agent/ask", content=b"x" * 32769).status_code == 413


@pytest.mark.parametrize("alteration", ["identity", "players"])
def test_two_season_binding_refuses_other_identity_or_expanded_players(
    prepared,  # noqa: F811
    alteration,
):
    original = prepared[0]()
    authority = prepared[4]
    if alteration == "identity":
        altered_authority = replace(
            authority, identity_record_id=content_digest("other identity")
        )
    else:
        altered_authority = replace(
            authority, approved_player_ids=authority.approved_player_ids | {12345}
        )
    swapped = prepared[0](altered_authority)
    assert tuple(c.binding for c in original._contexts) == tuple(
        c.binding for c in swapped._contexts
    )
    assert original.binding["selection_id"] != swapped.binding["selection_id"]
    names = ((1, "Alex Example"), (2, "Blair Sample"))
    binding = CandidateBinding(
        journal_id=content_digest("journal"),
        generation=1,
        record_id=content_digest("record"),
        candidate_id=content_digest("candidate"),
        code_id=content_digest("code"),
        catalog_id=content_digest(names),
        status_id=content_digest("status"),
        status_revision=1,
        snapshot_ids=tuple(c.binding["snapshot_id"] for c in original._contexts),
        context_ids=tuple(c.binding["context_id"] for c in original._contexts),
        selection_id=original.binding["selection_id"],
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    kwargs = dict(
        binding=binding,
        check_binding=lambda: binding,
        players=names,
        evidence_kind="synthetic",
    )
    with pytest.raises(ServingError, match="snapshot_binding_mismatch"):
        CandidatePTSService(swapped, **kwargs)
    service = CandidatePTSService(original, **kwargs)
    service._selection = swapped
    with pytest.raises(ServingError, match="candidate_authority_changed"):
        service.check()


def test_candidate_exposes_jobs_only_when_explicitly_mounted():
    from app.serving.candidate_application import _CandidateBoundary
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    inner = FastAPI()

    @inner.get("/jobs-monitor/api/jobs")
    def jobs():
        return {"status": "ok"}

    for enabled, expected in [(False, 403), (True, 200)]:
        app = _CandidateBoundary(inner, jobs_enabled=enabled)
        with TestClient(
            app, base_url="http://127.0.0.1", client=("127.0.0.1", 1234)
        ) as client:
            assert client.get("/jobs-monitor/api/jobs").status_code == expected
            assert client.post("/jobs-monitor/api/jobs").status_code == 403
            assert (
                client.get(
                    "/jobs-monitor/api/jobs", headers={"X-Forwarded-For": "127.0.0.1"}
                ).status_code
                == 403
            )
