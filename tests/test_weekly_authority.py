"""An independently supplied authority drives the real weekly candidate."""

import json
import os
import shutil
from datetime import datetime, timedelta, timezone

import pytest
from app import main
from app.config import Settings
from app.serving.candidate_service import CandidateBinding
from app.serving.contract import ServingError, canonical_json, content_digest
from app.serving.weekly_authority import WeeklyAuthority
from app.serving.weekly_selection import open_weekly_selection
from fastapi.testclient import TestClient

from test_current_serving_snapshot import current  # noqa: F401
from test_two_season_composite import prepared  # noqa: F401


def _document(current_fixture):
    _, manifest, live, policy, _, context = current_fixture
    status = live["status"]
    players = ((1, "Alex Example"), (2, "Blair Sample"))
    binding = CandidateBinding(
        journal_id=content_digest("journal"),
        generation=1,
        record_id=content_digest("record"),
        candidate_id=content_digest("candidate"),
        code_id=content_digest("code"),
        catalog_id=content_digest(players),
        status_id=content_digest("status"),
        status_revision=1,
        snapshot_ids=(manifest["snapshot_id"],),
        context_ids=(context.binding["context_id"],),
        selection_id=None,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    b = {
        **vars(binding),
        "snapshot_ids": list(binding.snapshot_ids),
        "context_ids": list(binding.context_ids),
        "expires_at": binding.expires_at.isoformat(),
    }
    return {
        "schema": "weekly-app-authority/1",
        "binding": b,
        "current_status": {
            "status_id": status.status_id,
            "checked_at": status.checked_at.isoformat(),
            "valid_until": status.valid_until.isoformat(),
            "allowed_snapshot_ids": list(status.allowed_snapshot_ids),
            "dependency_states": dict(status.dependency_states),
        },
        "current_policy": {
            "freshness_policy_fingerprint": policy.freshness_policy_fingerprint,
            "source_identity_authority_id": policy.source_identity_authority_id,
            "max_status_age_seconds": 86400,
        },
        "players": [list(player) for player in players],
        "historical": None,
    }


def test_authority_file_drives_candidate_and_revocation(tmp_path, current, monkeypatch):  # noqa: F811
    root, manifest, live, _, _, _ = current
    base = tmp_path / "reports/weekly-snapshots"
    (base / "snapshots").mkdir(parents=True)
    shutil.copytree(root, base / "snapshots" / manifest["snapshot_id"])
    (base / "app-selection.json").write_text(
        json.dumps(
            {
                "schema": "offline-weekly-app-selection/1",
                "snapshot_id": manifest["snapshot_id"],
                "run_id": "a" * 32,
                "runtime_eligible": False,
            }
        )
    )
    path = tmp_path / "authority.json"
    document = _document(current)
    path.write_text(json.dumps(document))
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.setattr(main.app, "middleware_stack", None)
    monkeypatch.setattr(main, "get_settings", main.get_settings)
    main.app.dependency_overrides.clear()
    main.app.state._state.pop("offline_candidate_claimed", None)
    settings = Settings(
        project_id="offline",
        gold_dataset="unused",
        metadata_dataset="unused",
        freshness_threshold_hours=1,
        max_search_results=5,
        performance_cache_prewarm_enabled=False,
    )
    authority = WeeklyAuthority(path, clock=lambda: live["now"])
    app = authority.application(tmp_path, settings=settings)
    try:
        with TestClient(
            app, base_url="http://127.0.0.1", client=("127.0.0.1", 43210)
        ) as client:
            response = client.get("/api/players/1?season=2026-27")
            assert response.status_code == 200
            assert response.json()["binding"]["snapshot_id"] == manifest["snapshot_id"]
            updated = json.loads(json.dumps(manifest))
            updated.pop("snapshot_id")
            original_identity = updated["lineage"]["source_identity_authority_id"]
            new_identity = content_digest("new-source-identity")
            updated["lineage"]["source_identity_authority_id"] = new_identity
            updated["dependency_ids"] = sorted(
                (set(updated["dependency_ids"]) - {original_identity}) | {new_identity}
            )
            updated["snapshot_id"] = content_digest(updated)
            shutil.copytree(root, base / "snapshots" / updated["snapshot_id"])
            (base / "snapshots" / updated["snapshot_id"] / "manifest.json").write_bytes(
                canonical_json(updated)
            )
            document["current_status"]["allowed_snapshot_ids"].append(
                updated["snapshot_id"]
            )
            document["current_status"]["dependency_states"][new_identity] = "active"
            document["current_policy"]["source_identity_authority_id"] = new_identity
            path.write_text(json.dumps(document))
            (base / "app-selection.json").write_text(
                json.dumps(
                    {
                        "schema": "offline-weekly-app-selection/1",
                        "snapshot_id": updated["snapshot_id"],
                        "run_id": "b" * 32,
                        "runtime_eligible": False,
                    }
                )
            )
            context = open_weekly_selection(tmp_path, authority.context()["policy"])
            document["binding"]["snapshot_ids"] = [updated["snapshot_id"]]
            document["binding"]["context_ids"] = [context.binding["context_id"]]
            document["binding"]["generation"] += 1
            path.write_text(json.dumps(document))
            response = client.get("/api/players/1?season=2026-27")
            assert response.status_code == 200
            assert response.json()["binding"]["snapshot_id"] == updated["snapshot_id"]
            document["current_status"]["allowed_snapshot_ids"] = []
            path.write_text(json.dumps(document))
            response = client.get("/api/players/1?season=2026-27")
            assert response.status_code == 503
    finally:
        main.app.dependency_overrides.clear()
        main.app.state._state.pop("offline_candidate_claimed", None)


def test_authority_rejects_symlink_and_writable_file(tmp_path, current):  # noqa: F811
    path = tmp_path / "authority.json"
    path.write_text(json.dumps(_document(current)))
    path.chmod(0o666)
    with pytest.raises(ServingError, match="weekly_authority_unavailable"):
        WeeklyAuthority(path).read()
    path.chmod(0o600)
    link = tmp_path / "link.json"
    os.symlink(path, link)
    with pytest.raises(ServingError, match="weekly_authority_unavailable"):
        WeeklyAuthority(link).read()
    path.write_text('{"schema":"x","schema":"y"}')
    with pytest.raises(ServingError, match="weekly_authority_unavailable"):
        WeeklyAuthority(path).read()


def test_policy_change_requires_a_changed_binding(tmp_path, current):  # noqa: F811
    path = tmp_path / "authority.json"
    document = _document(current)
    path.write_text(json.dumps(document))
    authority = WeeklyAuthority(path)
    authority.binding()
    document["current_policy"]["source_identity_authority_id"] = content_digest("other")
    path.write_text(json.dumps(document))
    with pytest.raises(ServingError, match="weekly_authority_unavailable"):
        authority.binding()
    document["binding"]["generation"] += 1
    path.write_text(json.dumps(document))
    assert authority.binding().generation == 2


def test_captured_context_cannot_use_new_binding_or_status(tmp_path, current):  # noqa: F811
    path = tmp_path / "authority.json"
    document = _document(current)
    path.write_text(json.dumps(document))
    authority = WeeklyAuthority(path)
    old_context = authority.context()
    document["current_policy"]["source_identity_authority_id"] = content_digest("new")
    document["binding"]["generation"] += 1
    path.write_text(json.dumps(document))
    with pytest.raises(ServingError, match="weekly_authority_unavailable"):
        old_context["check_binding"]()
    with pytest.raises(ServingError, match="weekly_authority_unavailable"):
        old_context["policy"].authority_provider()
    assert authority.context()["check_binding"]().generation == 2


def test_local_registry_revocation_is_checked_on_every_read(tmp_path, current):  # noqa: F811
    import hashlib

    path = tmp_path / "authority.json"
    registry_path = tmp_path / "registry.json"
    policy_path = tmp_path / "policy.json"
    policy_path.write_text('{"approved":true}')
    document = _document(current)
    policy_id = hashlib.sha256(policy_path.read_bytes()).hexdigest()
    document["registry"] = {
        "path": str(registry_path),
        "policy_path": str(policy_path),
        "policy_sha256": policy_id,
    }
    registry = {
        "schema": "weekly-local-revocations/1",
        "policy_sha256": policy_id,
        "enabled": True,
        "revision": 1,
        "revoked_snapshot_ids": [],
        "revoked_dependency_ids": [],
        "revoked_code_ids": [],
    }
    path.write_text(json.dumps(document))
    registry_path.write_text(json.dumps(registry))
    authority = WeeklyAuthority(path)
    checker = authority.context()["check_binding"]
    assert checker().snapshot_ids == tuple(document["binding"]["snapshot_ids"])
    policy_path.write_text('{"approved":false}')
    with pytest.raises(ServingError, match="weekly_authority_unavailable"):
        checker()
    policy_path.write_text('{"approved":true}')
    registry["revoked_snapshot_ids"] = document["binding"]["snapshot_ids"]
    registry_path.write_text(json.dumps(registry))
    with pytest.raises(ServingError, match="weekly_authority_unavailable"):
        checker()
    registry["revoked_snapshot_ids"] = []
    document["historical"] = {"status": {"dependency_states": {"f" * 64: "active"}}}
    registry["revoked_dependency_ids"] = ["f" * 64]
    path.write_text(json.dumps(document))
    registry_path.write_text(json.dumps(registry))
    with pytest.raises(ServingError, match="weekly_authority_unavailable"):
        authority.read()
    registry["revoked_dependency_ids"] = []
    registry["revision"] = 0
    registry_path.write_text(json.dumps(registry))
    with pytest.raises(ServingError, match="weekly_authority_unavailable"):
        authority.read()
    registry_path.unlink()
    with pytest.raises(ServingError, match="weekly_authority_unavailable"):
        checker()


def test_authority_loads_two_season_candidate(tmp_path, prepared, monkeypatch):  # noqa: F811
    from shared_serving_fixtures import NOW

    make, live, hm, cm, composition = prepared
    selection = make()
    players = ((1, "Alex Example"), (2, "Blair Sample"))
    binding = CandidateBinding(
        journal_id=content_digest("journal"),
        generation=1,
        record_id=content_digest("record"),
        candidate_id=content_digest("candidate"),
        code_id=content_digest("code"),
        catalog_id=content_digest(players),
        status_id=content_digest("status"),
        status_revision=1,
        snapshot_ids=tuple(c.binding["snapshot_id"] for c in selection._contexts),
        context_ids=tuple(c.binding["context_id"] for c in selection._contexts),
        selection_id=selection.binding["selection_id"],
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )

    def status(value):
        return {
            "status_id": value.status_id,
            "checked_at": value.checked_at.isoformat(),
            "valid_until": value.valid_until.isoformat(),
            "allowed_snapshot_ids": list(value.allowed_snapshot_ids),
            "dependency_states": dict(value.dependency_states),
        }

    wire = {
        **vars(binding),
        "snapshot_ids": list(binding.snapshot_ids),
        "context_ids": list(binding.context_ids),
        "expires_at": binding.expires_at.isoformat(),
    }
    document = {
        "schema": "weekly-app-authority/1",
        "binding": wire,
        "current_status": status(live["c"]),
        "current_policy": {
            "freshness_policy_fingerprint": cm["current_evidence"][
                "policy_fingerprint"
            ],
            "source_identity_authority_id": cm["lineage"][
                "source_identity_authority_id"
            ],
            "max_status_age_seconds": 86400,
        },
        "players": [list(player) for player in players],
        "historical": {
            "root": str(tmp_path / "historical"),
            "snapshot_id": hm["snapshot_id"],
            "status": status(live["h"]),
            "composition": {
                "policy_id": composition.policy_id,
                "identity_record_id": composition.identity_record_id,
                "historical_identity_id": composition.historical_identity_id,
                "current_identity_id": composition.current_identity_id,
                "approved_player_ids": list(composition.approved_player_ids),
            },
        },
    }
    base = tmp_path / "reports/weekly-snapshots"
    (base / "snapshots").mkdir(parents=True)
    shutil.copytree(tmp_path / "current", base / "snapshots" / cm["snapshot_id"])
    (base / "app-selection.json").write_text(
        json.dumps(
            {
                "schema": "offline-weekly-app-selection/1",
                "snapshot_id": cm["snapshot_id"],
                "run_id": "a" * 32,
                "runtime_eligible": False,
            }
        )
    )
    path = tmp_path / "authority.json"
    path.write_text(json.dumps(document))
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.setattr(main.app, "middleware_stack", None)
    monkeypatch.setattr(main, "get_settings", main.get_settings)
    main.app.dependency_overrides.clear()
    main.app.state._state.pop("offline_candidate_claimed", None)
    settings = Settings(
        project_id="offline",
        gold_dataset="unused",
        metadata_dataset="unused",
        freshness_threshold_hours=1,
        max_search_results=5,
        performance_cache_prewarm_enabled=False,
    )
    app = WeeklyAuthority(path, clock=lambda: NOW).application(
        tmp_path, settings=settings
    )
    try:
        with TestClient(
            app, base_url="http://127.0.0.1", client=("127.0.0.1", 43210)
        ) as client:
            response = client.post(
                "/api/agent/ask",
                json={
                    "question": "Compare Alex Example's points per game in the 2026-27 regular season vs the 2025-26 regular season"
                },
            )
            assert response.status_code == 200
            assert [
                m["binding"]["snapshot_id"]
                for m in response.json()["candidate_evidence"]
            ] == [hm["snapshot_id"], cm["snapshot_id"]]
    finally:
        main.app.dependency_overrides.clear()
        main.app.state._state.pop("offline_candidate_claimed", None)
