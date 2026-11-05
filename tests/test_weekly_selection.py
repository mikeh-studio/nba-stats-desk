# ruff: noqa: F811
"""The app opens only the selected offline snapshot under its reader policy."""

import json
import shutil

import pytest
from app.serving.contract import ServingError
from app.serving.weekly_selection import open_weekly_selection

from test_current_serving_snapshot import current  # noqa: F401


def test_weekly_selection_uses_existing_reader_policy(tmp_path, current):  # noqa: F811
    root, manifest, live, policy, _, _ = current
    base = tmp_path / "reports/weekly-snapshots"
    destination = base / "snapshots" / manifest["snapshot_id"]
    destination.parent.mkdir(parents=True)
    shutil.copytree(root, destination)
    selected = {
        "schema": "offline-weekly-app-selection/1",
        "snapshot_id": manifest["snapshot_id"],
        "run_id": "a" * 32,
        "runtime_eligible": False,
    }
    (base / "app-selection.json").write_text(json.dumps(selected))
    context = open_weekly_selection(tmp_path, policy)
    assert context.binding["snapshot_id"] == manifest["snapshot_id"]
    selected["runtime_eligible"] = True
    (base / "app-selection.json").write_text(json.dumps(selected))
    with pytest.raises(ServingError, match="weekly_selection_unavailable"):
        open_weekly_selection(tmp_path, policy)


def test_refresh_swaps_whole_service_and_keeps_valid_prior_on_failure(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from app.serving import candidate_application, candidate_service, weekly_selection
    from fastapi import HTTPException

    base = tmp_path / "reports/weekly-snapshots"
    base.mkdir(parents=True)

    def select(sid):
        (base / "app-selection.json").write_text(
            json.dumps(
                {
                    "schema": "offline-weekly-app-selection/1",
                    "snapshot_id": sid,
                    "run_id": "a" * 32,
                    "runtime_eligible": False,
                }
            )
        )

    active = {"binding": "binding-a", "bad": False, "valid": True}
    select("a" * 64)
    monkeypatch.setattr(
        weekly_selection,
        "open_weekly_selection",
        lambda root, policy: SimpleNamespace(
            binding={"snapshot_id": weekly_selection._selected_snapshot_id(root)}
        ),
    )

    class Service:
        def __init__(self, selection, *, binding, **kwargs):
            if active["bad"]:
                raise ServingError("invalid_artifact")
            self.sid = selection.binding["snapshot_id"]
            self.binding = binding

        def check(self):
            if not active["valid"]:
                raise ServingError("candidate_authority_changed")

    monkeypatch.setattr(candidate_service, "CandidatePTSService", Service)
    monkeypatch.setattr(
        candidate_application,
        "create_candidate_application",
        lambda service, **kwargs: (service, kwargs["service_provider"]),
    )
    initial, provider = weekly_selection.create_weekly_candidate_application(
        tmp_path,
        policy=object(),
        binding="binding-a",
        check_binding=lambda: None,
        players=((1, "A"),),
        settings=object(),
        binding_loader=lambda: active["binding"],
    )
    assert provider() is initial
    select("b" * 64)
    active["binding"] = "binding-b"
    active["bad"] = True
    assert provider() is initial
    active["bad"] = False
    newer = provider()
    assert newer.sid == "b" * 64 and newer is not initial
    assert initial.sid == "a" * 64  # An in-flight request retains its service.
    active["binding"] = "renewed-b"
    renewed = provider()
    assert renewed is not newer and renewed.binding == "renewed-b"
    active["valid"] = False
    with pytest.raises(HTTPException) as exc:
        provider()
    assert exc.value.status_code == 503
    assert exc.value.headers == {"Cache-Control": "no-store"}


def test_real_reader_refreshes_when_identity_policy_changes(
    tmp_path, current, monkeypatch
):  # noqa: F811
    from dataclasses import replace
    from datetime import datetime, timedelta, timezone

    from app import main
    from app.config import Settings
    from app.serving import FactReadStatus
    from app.serving.candidate_service import CandidateBinding
    from app.serving.contract import canonical_json, content_digest
    from app.serving.current import CurrentReadPolicy
    from app.serving.weekly_selection import create_weekly_candidate_application
    from fastapi.testclient import TestClient

    root, manifest, live, policy_a, _, context_a = current
    base = tmp_path / "reports/weekly-snapshots"
    snap = base / "snapshots"
    snap.mkdir(parents=True)
    shutil.copytree(root, snap / manifest["snapshot_id"])
    manifest_b = json.loads(json.dumps(manifest))
    manifest_b.pop("snapshot_id")
    old_identity = manifest_b["lineage"]["source_identity_authority_id"]
    new_identity = content_digest("updated-identity")
    manifest_b["lineage"]["source_identity_authority_id"] = new_identity
    manifest_b["dependency_ids"] = sorted(
        (set(manifest_b["dependency_ids"]) - {old_identity}) | {new_identity}
    )
    manifest_b["snapshot_id"] = content_digest(manifest_b)
    shutil.copytree(root, snap / manifest_b["snapshot_id"])
    (snap / manifest_b["snapshot_id"] / "manifest.json").write_bytes(
        canonical_json(manifest_b)
    )
    live["status"] = FactReadStatus(
        live["status"].status_id,
        live["now"],
        live["now"] + timedelta(hours=1),
        frozenset({manifest["snapshot_id"], manifest_b["snapshot_id"]}),
        {
            k: "active"
            for k in set(manifest["dependency_ids"]) | set(manifest_b["dependency_ids"])
        },
    )
    policy_b = CurrentReadPolicy(
        lambda: live["status"],
        lambda: live["now"],
        freshness_policy_fingerprint=policy_a.freshness_policy_fingerprint,
        source_identity_authority_id=new_identity,
    )

    def select(sid):
        (base / "app-selection.json").write_text(
            json.dumps(
                {
                    "schema": "offline-weekly-app-selection/1",
                    "snapshot_id": sid,
                    "run_id": "a" * 32,
                    "runtime_eligible": False,
                }
            )
        )

    select(manifest["snapshot_id"])
    context_b = None
    binding_a = CandidateBinding(
        journal_id=content_digest("journal"),
        generation=1,
        record_id=content_digest("record"),
        candidate_id=content_digest("candidate"),
        code_id=content_digest("code"),
        catalog_id=content_digest(((1, "Alex Example"), (2, "Blair Sample"))),
        status_id=content_digest("status"),
        status_revision=1,
        snapshot_ids=(context_a.binding["snapshot_id"],),
        context_ids=(context_a.binding["context_id"],),
        selection_id=None,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.setattr(main.app, "middleware_stack", None)
    monkeypatch.setattr(main, "get_settings", main.get_settings)
    main.app.dependency_overrides.clear()
    main.app.state._state.pop("offline_candidate_claimed", None)
    active = {"binding": binding_a, "policy": policy_a}
    settings = Settings(
        project_id="synthetic",
        gold_dataset="synthetic",
        metadata_dataset="synthetic",
        freshness_threshold_hours=1,
        max_search_results=5,
        performance_cache_prewarm_enabled=False,
    )
    app = create_weekly_candidate_application(
        tmp_path,
        policy=policy_a,
        binding=binding_a,
        check_binding=lambda: active["binding"],
        players=((1, "Alex Example"), (2, "Blair Sample")),
        settings=settings,
        binding_loader=lambda: active["binding"],
        refresh_context_loader=lambda: {"policy": active["policy"]},
    )
    try:
        with TestClient(
            app, base_url="http://127.0.0.1", client=("127.0.0.1", 43210)
        ) as client:
            before = client.get("/api/players/1?season=2026-27")
            assert before.status_code == 200
            assert before.json()["binding"]["snapshot_id"] == manifest["snapshot_id"]
            select(manifest_b["snapshot_id"])
            context_b = open_weekly_selection(tmp_path, policy_b)
            active["binding"] = replace(
                binding_a,
                snapshot_ids=(manifest_b["snapshot_id"],),
                context_ids=(context_b.binding["context_id"],),
            )
            active["policy"] = policy_b
            after = client.get("/api/players/1?season=2026-27")
            assert after.status_code == 200
            assert after.json()["binding"]["snapshot_id"] == manifest_b["snapshot_id"]
    finally:
        main.app.dependency_overrides.clear()
        main.app.state._state.pop("offline_candidate_claimed", None)
