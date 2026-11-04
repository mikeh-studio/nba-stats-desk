"""The normal app reads only an actual successful local weekly build."""

import json
import shutil
from datetime import datetime, timedelta

import pytest
from app import main
from app.serving.contract import ServingError, content_digest
from app.serving.local_weekly import LocalWeeklyPTS
from fastapi import FastAPI
from fastapi.testclient import TestClient

from test_current_serving_snapshot import current  # noqa: F401


@pytest.fixture
def weekly_root(tmp_path, current):  # noqa: F811
    root, manifest, live, _, _, _ = current
    base = tmp_path.resolve() / "reports/weekly-snapshots"
    target = base / "snapshots" / manifest["snapshot_id"]
    target.parent.mkdir(parents=True)
    shutil.copytree(root, target)
    (base / "runs").mkdir()
    selection = {
        "schema": "offline-weekly-app-selection/1",
        "snapshot_id": manifest["snapshot_id"],
        "run_id": "a" * 32,
        "runtime_eligible": False,
    }
    receipt = {
        "schema": "private-current-pts-build/1",
        "snapshot_id": manifest["snapshot_id"],
        "runtime_eligible": False,
        "source_identity_kind": "reviewed_source_identity",
        "policy_fingerprint": manifest["current_evidence"]["policy_fingerprint"],
    }
    receipt["receipt_id"] = content_digest(receipt)
    record = {
        "schema": "weekly-snapshot-result/1",
        "status": "succeeded",
        "snapshot_id": manifest["snapshot_id"],
        "run_id": "a" * 32,
        "runtime_eligible": False,
        "producer_receipt": receipt,
    }
    (base / "app-selection.json").write_text(json.dumps(selection))
    (base / "runs" / ("a" * 32 + ".result.json")).write_text(json.dumps(record))
    return tmp_path.resolve(), manifest, live


def test_local_reader_uses_successful_build_and_rejects_tampering(weekly_root):
    root, manifest, live = weekly_root
    service = LocalWeeklyPTS(root, clock=lambda: live["now"])
    assert service.player(1)["player_profile"]["total_pts"] == 10
    assert service.ask(1)["binding"]["snapshot_id"] == manifest["snapshot_id"]
    assert service.compare((1, 2))["tables"][0]["rows"][0][1] == 5
    path = root / "reports/weekly-snapshots/runs" / ("a" * 32 + ".result.json")
    record = json.loads(path.read_text())
    record["producer_receipt"]["source_identity_kind"] = "synthetic"
    path.write_text(json.dumps(record))
    with pytest.raises(ServingError):
        service.player(1)


def test_normal_app_startup_attaches_configured_snapshot(weekly_root, monkeypatch):
    root, _, _ = weekly_root
    application = FastAPI()
    monkeypatch.setenv("NBA_WEEKLY_SNAPSHOT_ROOT", str(root))
    main._configure_weekly_snapshot(application)
    assert isinstance(application.state.weekly_pts, LocalWeeklyPTS)
    assert application.state.weekly_pts.root == root
    disabled = FastAPI()
    monkeypatch.setenv("NBA_WEEKLY_SNAPSHOT_ROOT", "")
    main._configure_weekly_snapshot(disabled)
    assert not hasattr(disabled.state, "weekly_pts")


def test_normal_app_uses_snapshot_without_replacing_other_routes(
    weekly_root, monkeypatch
):
    root, manifest, live = weekly_root
    service = LocalWeeklyPTS(root, clock=lambda: live["now"])

    class Repo:
        def get_player_detail(self, player_id):
            return {"player": {"player_id": player_id}}

        def get_compare(self, *args, **kwargs):
            return {"warehouse_result": True}

    monkeypatch.setattr(main.app.state, "weekly_pts", service, raising=False)
    monkeypatch.setitem(
        main.app.dependency_overrides, main.get_repository, lambda: Repo()
    )
    client = TestClient(
        main.app, base_url="http://127.0.0.1", client=("127.0.0.1", 4000)
    )
    player = client.get("/api/players/1")
    assert player.status_code == 200
    assert player.json()["item"]["player"]["player_id"] == 1
    assert (
        player.json()["weekly_pts"]["binding"]["snapshot_id"] == manifest["snapshot_id"]
    )
    historical = client.get("/api/players/1?season=2025-26")
    assert historical.status_code == 200 and "weekly_pts" not in historical.json()
    ask = client.post(
        "/api/weekly-pts/ask", json={"player_id": 1, "aggregation": "total"}
    )
    assert ask.status_code == 200 and ask.json()["tables"][0]["rows"][0][1] == 10
    assert ask.json()["tables"][0]["rows"][0][2:] == [3, 2, 1]
    compare = client.get(
        "/api/compare?player_a_id=1&player_b_id=2&window=regular_season&focus=scoring"
    )
    assert compare.status_code == 200 and compare.json()["warehouse_result"] is True
    assert (
        compare.json()["weekly_pts"]["binding"]["snapshot_id"]
        == manifest["snapshot_id"]
    )
    (root / "reports/weekly-snapshots/app-selection.json").unlink()
    assert client.post("/api/weekly-pts/ask", json={"player_id": 1}).status_code == 503
    assert client.get("/api/players/1").json()["item"]["player"]["player_id"] == 1


def test_selection_change_during_response_fails_closed(weekly_root, monkeypatch):
    root, _, live = weekly_root
    service = LocalWeeklyPTS(root, clock=lambda: live["now"])
    original = service.ask

    def remove_after_answer(*args):
        result = original(*args)
        (root / "reports/weekly-snapshots/app-selection.json").unlink()
        return result

    monkeypatch.setattr(service, "ask", remove_after_answer)
    monkeypatch.setattr(main.app.state, "weekly_pts", service, raising=False)
    client = TestClient(
        main.app, base_url="http://127.0.0.1", client=("127.0.0.1", 4000)
    )
    response = client.post("/api/weekly-pts/ask", json={"player_id": 1})
    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"


def test_expiry_during_response_fails_closed(weekly_root, monkeypatch):
    root, manifest, live = weekly_root
    service = LocalWeeklyPTS(root, clock=lambda: live["now"])
    original = service.ask

    def expire_after_answer(*args):
        result = original(*args)
        live["now"] = datetime.fromisoformat(
            manifest["clocks"]["hard_expiry"]
        ) + timedelta(microseconds=1)
        return result

    monkeypatch.setattr(service, "ask", expire_after_answer)
    monkeypatch.setattr(main.app.state, "weekly_pts", service, raising=False)
    client = TestClient(
        main.app, base_url="http://127.0.0.1", client=("127.0.0.1", 4000)
    )
    assert client.post("/api/weekly-pts/ask", json={"player_id": 1}).status_code == 503
