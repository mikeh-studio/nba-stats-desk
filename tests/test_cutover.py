import json

import pytest
from app.serving.core_application import create_core_application
from app.serving.cutover import CutoverApplication, public_job
from fastapi import FastAPI
from fastapi.testclient import TestClient
from scripts.build_synthetic_core_replay import synthetic_bundle
from scripts.validate_publication_contract import digest


def test_switch_rollback_keeps_pinned_core_flow_and_old_app():
    bundle = synthetic_bundle()
    core = create_core_application(
        bundle,
        expected_digest=digest(bundle),
        clock=lambda: "2026-11-01T16:00:00Z",
        authorize=lambda *_: True,
    )
    old = FastAPI()

    @old.get("/api/health")
    def health():
        return {"mode": "legacy"}

    state = ["legacy"]
    app = CutoverApplication(legacy=old, core=core, mode=lambda: state[0])
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 123)) as c:
        assert c.get("/api/health").json() == {"mode": "legacy"}
        state[0] = "core_rehearsal"
        r = c.get("/api/health")
        assert r.status_code == 200
        token = r.headers["x-publication-id"]
        page = c.get("/compare?player_a_id=101&player_b_id=202")
        assert page.status_code == 200
        assert 'name="publication_id"' in page.text
        state[0] = "legacy"
        assert c.get("/api/health").json() == {"mode": "legacy"}
        assert (
            c.get("/api/health?publication_id=" + token).headers["x-publication-id"]
            == token
        )
        assert (
            c.get(
                "/api/health?publication_id=" + token,
                headers={"x-publication-id": "pub_ffffffffffffffff"},
            ).status_code
            == 503
        )
        assert c.get("/api/health?publication_id=bad").status_code == 503
        state[0] = "production"
        assert c.get("/api/health").status_code == 503


def test_jobs_never_treats_collector_success_as_publication_or_exposes_private_fields():
    record = {
        "bundle": "secret/path",
        "error": "private details",
        "stages": [
            {"name": "collection", "status": "retained", "private": "secret"},
            {"name": "models", "status": "succeeded"},
            {"name": "production_publication", "status": "held"},
        ],
    }
    projected = public_job(record)
    assert projected["stages"] == dict(
        collector="retained", candidate="succeeded", publication="held"
    )
    assert "secret" not in json.dumps(projected) and not projected["production_active"]
    with pytest.raises(ValueError):
        public_job({"stages": [{"name": "collection", "status": "unknown"}]})


def test_real_existing_app_history_and_historical_routes_survive_switch(
    monkeypatch, tmp_path
):
    import socket

    from app import main
    from app.config import Settings

    monkeypatch.setattr(
        socket.socket, "connect", lambda *_: pytest.fail("network attempted")
    )
    settings = Settings(
        project_id="synthetic",
        gold_dataset="synthetic",
        metadata_dataset="synthetic",
        freshness_threshold_hours=1,
        max_search_results=5,
        performance_cache_prewarm_enabled=False,
        agent_history_enabled=True,
        agent_history_path=str(tmp_path / "history.jsonl"),
    )
    main.app.dependency_overrides[main.get_settings] = lambda: settings
    monkeypatch.setattr(main, "_configure_weekly_snapshot", lambda *_: None)
    monkeypatch.setattr(main, "_start_cache_prewarm", lambda: None)
    monkeypatch.setattr(
        main,
        "read_history",
        lambda path, **kw: {
            "conversations": [
                {"id": kw["conversation_id"], "title": "Retained session"}
            ]
        },
    )
    b = synthetic_bundle()
    core = create_core_application(
        b,
        expected_digest=digest(b),
        clock=lambda: "2026-11-01T16:00:00Z",
        authorize=lambda *_: True,
    )
    mode = ["legacy"]
    app = CutoverApplication(legacy=main.app, core=core, mode=lambda: mode[0])
    try:
        with TestClient(
            app, base_url="http://127.0.0.1", client=("127.0.0.1", 321)
        ) as c:
            before = c.get("/api/agent/history?conversation_id=retained-session").json()
            legacy = c.get("/ask?season=2025-26", follow_redirects=False)
            assert legacy.status_code == 307 and legacy.headers["location"] == "/ask"
            mode[0] = "core_rehearsal"
            current = c.get("/ask")
            assert current.status_code == 200 and "Core stats" in current.text
            token = current.headers["x-publication-id"]
            assert (
                c.get("/api/agent/history?conversation_id=retained-session").json()
                == before
            )
            historical = c.get(
                "/api/agent/history?conversation_id=retained-session&season=2025-26"
            )
            assert historical.status_code == 200 and historical.json() == before
            mode[0] = "legacy"
            assert (
                c.get("/api/agent/history?conversation_id=retained-session").json()
                == before
            )
            assert c.get("/ask").status_code == 200
            assert (
                c.get("/ask?publication_id=" + token).headers["x-publication-id"]
                == token
            )
    finally:
        main.app.dependency_overrides.clear()


def test_missing_job_status_is_unavailable_not_pending():
    result = public_job(None)
    assert result["stages"] == {
        "collector": "unavailable",
        "candidate": "unavailable",
        "publication": "unavailable",
    }
    assert result["production_active"] is False
