"""Synthetic authority only: these fixtures never certify retained provider data."""

import copy
import json
from types import SimpleNamespace

import pytest
from app.serving.cutover import CutoverApplication
from app.serving.production_application import ProductionApplication
from app.serving.publication import ordered
from app.serving.source_authority import ProviderAuthorities, SourceAuthority
from app.serving.weekly_manifest import proposal
from fastapi import FastAPI
from fastapi.testclient import TestClient
from scripts.build_synthetic_core_replay import synthetic_bundle
from scripts.validate_publication_contract import (
    PROFILE,
    digest,
    schema_check,
    timestamp,
    validate_binding,
)
from scripts.weekly_profile import weekly_scope

NOW = "2026-11-01T16:00:00Z"
PROJECT, NS = "synthetic-project", "nba_core_synthetic"


def test_production_mode_read_does_not_block_other_requests():
    import asyncio
    import threading

    import httpx
    from scripts.serve_production_candidate import attach_publication

    entered, release = threading.Event(), threading.Event()
    app, core = FastAPI(), FastAPI()

    @app.get("/ping")
    async def ping():
        return {"ok": True}

    @core.get("/test")
    async def test():
        return {"ok": True}

    def mode():
        entered.set()
        release.wait(3)
        return "production"

    attach_publication(app, core, mode)

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            pending = asyncio.create_task(client.get("/publication-core/test"))
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                assert (
                    await asyncio.wait_for(client.get("/ping"), timeout=1)
                ).status_code == 200
            finally:
                release.set()
            assert (await pending).status_code == 200

    asyncio.run(exercise())


def test_publication_authority_does_not_block_other_requests(production):
    import asyncio
    import threading

    import httpx

    pid = production.envelope["manifest"]["publication_id"]
    core = production.app.application(pid)
    original = production.authority.check
    entered, release = threading.Event(), threading.Event()

    def check(*args):
        entered.set()
        release.wait(3)
        return original(*args)

    production.authority.check = check
    app = FastAPI()

    @app.get("/ping")
    async def ping():
        return {"ok": True}

    app.mount("/core", core)

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            pending = asyncio.create_task(client.get("/core/api/players/101"))
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                assert (
                    await asyncio.wait_for(client.get("/ping"), timeout=1)
                ).status_code == 200
            finally:
                release.set()
            assert (await pending).status_code == 200

    asyncio.run(exercise())


@pytest.fixture
def production(tmp_path):
    original = synthetic_bundle()
    accepted = original["accepted"]
    accepted.update(
        runtime_eligible=False, authority_scope="offline_model_validation_only"
    )
    # This cutoff differs from the v1 October 20-31 contract; all game data stay October.
    accepted["scope"]["last"] = "2026-10-30"
    policy = dict(
        version="source-use-policy/1",
        provider="espn",
        source_method="reviewed_espn_game_summary",
        fields=PROFILE["assets"]["fct_player_game_stats"]["required_fields"],
        semantics=PROFILE["semantics_version"],
        source_selection="unified_source_selection/1",
        freshness="weekly_8d_10d_v1",
        permitted_uses=["collect", "build", "promote", "serving"],
        scope={**accepted["scope"], "last": "2027-06-30"},
        valid_from="2026-10-01T00:00:00Z",
        valid_until="2027-07-01T00:00:00Z",
    )
    policy_path, registry_path = tmp_path / "policy.json", tmp_path / "registry.json"
    policy_path.write_text(json.dumps(policy))
    m = original["manifest"]
    inputs = copy.deepcopy(m["inputs"])
    for item in inputs:
        item["evidence_kind"] = "retained"  # synthetic stand-in for external controls
        if item["role"] == "source_authority":
            item["digest"] = digest(policy)
    evidence = {
        k: copy.deepcopy(m[k])
        for k in (
            "run_id",
            "versions",
            "selected_games",
            "checks",
            "freshness",
            "validation_as_of",
            "auxiliary",
        )
    }
    evidence.update(
        inputs=inputs,
        historical_routes=[
            r for r in original["binding"]["routes"] if r["mode"] != "release"
        ],
    )
    models = {n: ordered(n, rows) for n, rows in original["models"].items()}
    envelope = proposal(
        accepted=accepted,
        models=models,
        evidence=evidence,
        project=PROJECT,
        namespace=NS,
        materialized_at=NOW,
        generation=1,
    )
    manifest, binding = envelope["manifest"], envelope["binding"]
    pid = manifest["publication_id"]
    registry = dict(
        version="source-revocations/1",
        policy_digest=digest(policy),
        policy_revoked=False,
        checked_at=NOW,
        valid_until="2026-11-02T16:00:00Z",
        revoked_publications=[],
        revoked_dependencies=[],
        validated_releases={
            pid: dict(
                manifest_digest=digest(manifest),
                binding_digest=digest(binding),
                input_fingerprint=manifest["input_fingerprint"],
                code_versions=manifest["versions"],
                check_receipts={
                    c["name"]: c["receipt_digest"] for c in manifest["checks"]
                },
            )
        },
    )
    registry_path.write_text(json.dumps(registry))
    clock = [NOW]
    authority = SourceAuthority(
        policy_path=policy_path,
        policy_digest=digest(policy),
        registry_path=registry_path,
        clock=lambda: clock[0],
    )
    tables = {
        a["relation"]: SimpleNamespace(
            table_type="SNAPSHOT",
            etag=a["digest"],
            num_rows=a["rows"],
            expires=timestamp(binding["expires_at"]),
            rows=models[a["name"]],
        )
        for a in manifest["assets"]
    }

    class Client:
        reads = 0

        def query(self, sql, job_config, **_):
            if ".pointer`" in sql:
                rows = [dict(active_id=pid)]
            else:
                rows = [
                    dict(
                        eligible=True,
                        revoked=False,
                        manifest=json.dumps(envelope),
                        manifest_digest=digest(envelope),
                    )
                ]
            return SimpleNamespace(result=lambda **_: rows)

        def get_table(self, relation, **_):
            return tables[relation]

        def list_rows(self, table, **_):
            self.reads += 1
            return copy.deepcopy(table.rows)

    client = Client()
    app = ProductionApplication(
        client=client, project=PROJECT, namespace=NS, authority=authority, cache_ttl=0
    )
    return SimpleNamespace(
        app=app,
        client=client,
        envelope=envelope,
        accepted=accepted,
        evidence=evidence,
        registry=registry,
        registry_path=registry_path,
        policy=policy,
        policy_path=policy_path,
        authority=authority,
        tables=tables,
        clock=clock,
    )


def test_rolling_production_routes_and_pinned_rollback(production):
    p = production
    legacy = FastAPI()

    @legacy.get("/players/101")
    def old():
        return {"legacy": True}

    mode = ["production"]
    app = CutoverApplication(
        legacy=legacy,
        core=p.app,
        mode=lambda: mode[0],
        legacy_routes=p.evidence["historical_routes"],
    )
    with TestClient(app) as c:
        for path in [
            "/players/101",
            "/compare?player_a_id=101&player_b_id=202",
            "/performance",
            "/ask",
            "/api/players/search?q=Test",
        ]:
            r = c.get(path)
            assert r.status_code == 200, r.text
            assert (
                r.headers["x-publication-id"]
                == p.envelope["manifest"]["publication_id"]
            )
        for season in ["2023-24", "2024-25", "2025-26"]:
            assert c.get("/players/101", params={"season": season}).json() == {
                "legacy": True
            }
        assert c.get("/players/101", params={"phase": "Playoffs"}).json() == {
            "legacy": True
        }
        assert c.get("/api/publication/jobs").json()["production_active"] is True
        pid = p.envelope["manifest"]["publication_id"]
        assert (
            c.get(
                "/players/101", params={"season": "2025-26", "publication_id": pid}
            ).status_code
            == 422
        )
        mode[0] = "legacy"
        assert c.get("/players/101").json() == {"legacy": True}
        assert c.get("/players/101", params={"publication_id": pid}).status_code == 200
        p.registry["revoked_publications"].append(pid)
        p.registry_path.write_text(json.dumps(p.registry))
        assert c.get("/players/101", params={"publication_id": pid}).status_code == 503


def test_full_app_publication_overlay_preserves_legacy_and_rechecks_authority(
    production, monkeypatch, tmp_path
):
    from app import main
    from app.config import Settings
    from scripts.serve_production_candidate import attach_publication

    settings = Settings(
        project_id="synthetic",
        gold_dataset="synthetic",
        metadata_dataset="synthetic",
        freshness_threshold_hours=1,
        max_search_results=5,
        performance_cache_prewarm_enabled=False,
        agent_history_enabled=False,
        agent_history_path=str(tmp_path / "history.jsonl"),
    )
    main.app.dependency_overrides[main.get_settings] = lambda: settings
    monkeypatch.setattr(main, "_configure_weekly_snapshot", lambda *_: None)
    monkeypatch.setattr(main, "_start_cache_prewarm", lambda: None)
    state = ["production"]
    original_routes = list(main.app.router.routes)
    attach_publication(main.app, production.app, lambda: state[0])
    try:
        with TestClient(
            main.app, base_url="http://127.0.0.1", client=("127.0.0.1", 42)
        ) as c:
            ask = c.get("/ask")
            assert ask.status_code == 200 and "Ask NBA Stats" in ask.text
            assert "data-publication-routing" in ask.text
            assert "production_publication.js" in c.get("/players/101").text
            r = c.get("/publication-core/api/players/101")
            assert r.status_code == 200, r.text
            pid = r.json()["publication_id"]
            assert pid == production.envelope["manifest"]["publication_id"]
            answer = c.post(
                "/publication-core/api/agent/ask",
                json={"question": "Test Player A total points"},
            )
            assert answer.status_code == 200, answer.text
            assert answer.json()["publication_id"] == pid
            for question in (
                "How many points did Test Player A score?",
                "Test Player A average points",
            ):
                natural = c.post(
                    "/publication-core/api/agent/ask", json={"question": question}
                )
                assert natural.status_code == 200, natural.text
                assert natural.json()["publication_id"] == pid
            assert (
                c.post(
                    "/publication-core/api/agent/ask",
                    json={
                        "question": "How many points did Test Player A score last week?"
                    },
                ).status_code
                == 422
            )
            comparison = c.get(
                "/publication-core/api/compare",
                params={"player_a_id": 101, "player_b_id": 202, "window": "last_5"},
            )
            assert comparison.status_code == 200, comparison.text
            assert comparison.json()["publication_id"] == pid
            dates = c.get("/publication-core/api/performance/dates")
            assert dates.status_code == 200
            day = dates.json()["data"][0]
            games = c.get(
                "/publication-core/api/performance/games",
                params={"game_date": day},
                headers={"X-Publication-ID": pid},
            )
            assert games.status_code == 200
            players = c.get(
                "/publication-core/api/performance/players",
                params={"game_id": games.json()["data"][0]["game_id"]},
                headers={"X-Publication-ID": pid},
            )
            assert players.status_code == 200 and players.json()["data"]
            assert "data-publication-performance" in c.get("/performance").text
            r = c.post(
                "/publication-core/api/agent/ask", json={"question": "not supported"}
            )
            assert r.status_code == 422
            state[0] = "legacy"
            assert c.get("/publication-core/api/players/101").status_code == 409
            assert c.get("/ask").status_code == 200
            state[0] = "production"
            production.registry["revoked_publications"].append(pid)
            production.registry_path.write_text(json.dumps(production.registry))
            assert c.get("/publication-core/api/players/101").status_code == 503
    finally:
        main.app.router.routes[:] = original_routes
        del main.app.state.production_publication_enabled
        main.app.dependency_overrides.clear()


def test_nba_rolling_scope_does_not_grant_source_authority(production):
    scope = {**production.accepted["scope"], "provider": "nba_api"}
    assert weekly_scope(scope) == scope
    manifest = copy.deepcopy(production.envelope["manifest"])
    manifest["scope"] = scope
    schema_check("manifest", manifest)
    with pytest.raises(ValueError, match="outside operator policy"):
        production.authority.authorize_scope("build", scope)


def test_policy_provider_must_match_its_scope(production):
    production.policy["scope"]["provider"] = "nba_api"
    production.policy_path.write_text(json.dumps(production.policy))
    new_digest = digest(production.policy)
    production.registry["policy_digest"] = new_digest
    production.registry_path.write_text(json.dumps(production.registry))
    production.authority.policy_digest = new_digest
    with pytest.raises(ValueError, match="source authority unavailable"):
        production.authority.authorize_scope("collect", production.policy["scope"])


def test_nba_policy_requires_specific_method_and_selection(production):
    p = production
    p.policy.update(
        provider="nba_api",
        source_method="reviewed_nba_traditional_boxscore_v3",
        source_selection="nba_manual_selection/1",
    )
    p.policy["scope"]["provider"] = "nba_api"

    def repin():
        p.policy_path.write_text(json.dumps(p.policy))
        new_digest = digest(p.policy)
        p.registry["policy_digest"] = new_digest
        p.registry_path.write_text(json.dumps(p.registry))
        p.authority.policy_digest = new_digest

    repin()
    assert (
        p.authority.authorize_scope("build", p.policy["scope"])[0]["provider"]
        == "nba_api"
    )
    p.policy["source_method"] = "reviewed_espn_game_summary"
    repin()
    with pytest.raises(ValueError, match="source authority unavailable"):
        p.authority.policy("build")


def test_reader_routes_each_release_to_its_own_source_authority(production):
    class UnavailableNBA:
        def policy(self, _use):
            raise ValueError("NBA unavailable")

        def check(self, *_):
            raise ValueError("NBA release unavailable")

        def authorize_scope(self, *_):
            raise ValueError("NBA scope unavailable")

    p = production
    multi = ProviderAuthorities(
        {"nba_api": UnavailableNBA(), "espn": p.authority}, clock=lambda: NOW
    )
    assert multi.policy("serving")[0]["provider"] == "espn"
    assert multi.authorize_scope("build", p.accepted["scope"])[0]["provider"] == "espn"
    with pytest.raises(ValueError, match="NBA scope unavailable"):
        multi.authorize_scope("collect", {**p.accepted["scope"], "provider": "nba_api"})
    with pytest.raises(ValueError, match="production source authority unavailable"):
        ProviderAuthorities(
            {"nba_api": UnavailableNBA()}, clock=lambda: NOW
        ).authorize_scope("build", p.accepted["scope"])
    assert multi.check(p.envelope["manifest"], p.envelope["binding"]) is True
    nba_manifest = copy.deepcopy(p.envelope["manifest"])
    nba_manifest["scope"]["provider"] = "nba_api"
    with pytest.raises(ValueError, match="NBA release unavailable"):
        multi.check(nba_manifest, p.envelope["binding"])


def test_generation_pinned_cloud_mode_allows_rollback(tmp_path):
    from scripts.serve_production_candidate import attach_publication

    class Blob:
        generation = 1
        raw = b'{"mode":"production"}'

        @property
        def size(self):
            return len(self.raw)

        def reload(self, **_):
            pass

        def download_as_bytes(self, **kwargs):
            assert kwargs["if_generation_match"] == self.generation
            return self.raw

    blob = Blob()
    client = SimpleNamespace(bucket=lambda _: SimpleNamespace(blob=lambda _: blob))
    reader = SourceAuthority(
        policy_path="gs://control-bucket/app/mode.json",
        policy_digest="",
        registry_path=tmp_path / "unused",
        clock=lambda: NOW,
        storage_client=client,
    )
    legacy, core = FastAPI(), FastAPI()

    @core.get("/status")
    def status():
        return {"source": "publication"}

    attach_publication(
        legacy, core, lambda: reader.read_document(reader.policy_path)["mode"]
    )
    with TestClient(legacy) as c:
        assert c.get("/publication-core/status").status_code == 200
        blob.generation += 1
        blob.raw = b'{"mode":"legacy"}'
        assert c.get("/publication-core/status").status_code == 409
        blob.raw = b'{"mode":"unknown"}'
        assert c.get("/publication-core/status").status_code == 503


def test_cloud_launcher_requires_explicit_gcs_authority(monkeypatch):
    from scripts.serve_cloud_run_publication import config_from_environment

    config = {
        "project": "synthetic-project",
        "namespace": "nba_core_synthetic",
        "mode_path": "gs://synthetic-bucket/mode.json",
        "source_authorities": {
            "espn": {
                "policy_path": "gs://synthetic-bucket/policy.json",
                "registry_path": "gs://synthetic-bucket/registry.json",
                "policy_digest": "a" * 64,
            }
        },
    }
    monkeypatch.setenv("NBA_PUBLICATION_CONFIG", json.dumps(config))
    assert config_from_environment() == config
    config["mode_path"] = "/tmp/mode.json"
    monkeypatch.setenv("NBA_PUBLICATION_CONFIG", json.dumps(config))
    with pytest.raises(ValueError, match="mode control"):
        config_from_environment()


def test_production_snapshot_metadata_reads_are_bounded_parallel_and_fail_closed():
    import threading

    manifest = {
        "freshness": {"expires_at": "2026-11-09T00:00:00Z"},
        "assets": [
            {"name": f"asset_{i}", "relation": f"snapshot_{i}", "rows": 1}
            for i in range(4)
        ],
    }
    barrier = threading.Barrier(4)

    class Client:
        def get_table(self, relation, **_):
            barrier.wait(timeout=5)
            return SimpleNamespace(
                table_type="SNAPSHOT",
                num_rows=2 if relation == "snapshot_3" else 1,
                expires=timestamp("2026-11-10T00:00:00Z"),
            )

    reader = object.__new__(ProductionApplication)
    reader.client = Client()
    from app.serving.control_cache import ControlCache

    reader.controls = ControlCache(0)
    with pytest.raises(ValueError, match="snapshot unavailable"):
        reader.tables(manifest)


def test_source_authority_reads_generation_pinned_gcs_registry(production):
    p = production
    raw = json.dumps(p.registry).encode()

    class Blob:
        size = len(raw)
        generation = 7
        changed = False

        def reload(self, **kwargs):
            pass

        def download_as_bytes(self, **kwargs):
            assert kwargs["if_generation_match"] == 7
            if self.changed:
                raise RuntimeError("generation changed")
            return raw

    blob = Blob()
    client = SimpleNamespace(bucket=lambda name: SimpleNamespace(blob=lambda key: blob))
    authority = SourceAuthority(
        policy_path=p.policy_path,
        policy_digest=digest(p.policy),
        registry_path="gs://private-bucket/release/registry.json",
        clock=lambda: NOW,
        storage_client=client,
    )
    assert authority.check(p.envelope["manifest"], p.envelope["binding"]) is True
    blob.changed = True
    with pytest.raises(ValueError, match="source authority unavailable"):
        authority.policy("serving")


def test_independent_candidate_content_verification_detects_changed_table(production):
    from app.serving.candidate_verification import verify_candidate

    p = production
    receipt = verify_candidate(
        p.client, p.envelope, p.accepted, project=PROJECT, namespace=NS, now=NOW
    )
    assert (
        receipt["status"] == "verified_contents_only"
        and receipt["runtime_eligible"] is False
    )
    relation = p.envelope["manifest"]["assets"][0]["relation"]
    p.tables[relation].rows[0]["home_score"] += 1
    with pytest.raises(ValueError, match="contents differ"):
        verify_candidate(
            p.client, p.envelope, p.accepted, project=PROJECT, namespace=NS, now=NOW
        )


def test_cold_asset_reads_run_in_parallel_and_cache_only_verified_data(production):
    import threading

    p = production
    barrier = threading.Barrier(4)
    guard = threading.Lock()
    started = 0
    original = p.client.list_rows

    def concurrent_read(table, **kwargs):
        nonlocal started
        with guard:
            started += 1
            first = started <= 4
        if first:
            barrier.wait(timeout=5)
        return original(table, **kwargs)

    p.client.list_rows = concurrent_read
    p.app.application(None)
    assert started == len(p.envelope["manifest"]["assets"])
    assert len(p.app.apps) == 1


def test_cold_asset_digest_mismatch_never_populates_cache(production):
    p = production
    relation = next(
        a["relation"]
        for a in p.envelope["manifest"]["assets"]
        if a["name"] == "accepted_game_results"
    )
    p.tables[relation].rows[0]["home_score"] += 1
    with pytest.raises(ValueError, match="production content differs"):
        p.app.application(None)
    assert not p.app.apps


@pytest.mark.parametrize(
    "failure",
    [
        "missing_policy",
        "changed_policy",
        "registry_missing",
        "registry_malformed",
        "policy_revoked",
        "dependency_revoked",
        "validation_missing",
        "expired",
        "table_replaced",
        "wrong_scope",
    ],
)
def test_authority_loss_closes_cached_release(production, failure):
    p = production
    with TestClient(p.app) as c:
        assert c.get("/players/101").status_code == 200
        if failure == "missing_policy":
            p.policy_path.unlink()
        elif failure == "changed_policy":
            p.policy_path.write_text("{}")
        elif failure == "registry_missing":
            p.registry_path.unlink()
        elif failure == "registry_malformed":
            p.registry_path.write_text("{}")
        elif failure == "policy_revoked":
            p.registry["policy_revoked"] = True
        elif failure == "dependency_revoked":
            p.registry["revoked_dependencies"].append(
                p.envelope["manifest"]["inputs"][0]["digest"]
            )
        elif failure == "validation_missing":
            p.registry["validated_releases"] = {}
        elif failure == "expired":
            p.clock[0] = "2026-11-12T00:00:00Z"
        elif failure == "table_replaced":
            next(iter(p.tables.values())).etag = "replaced"
        elif failure == "wrong_scope":
            p.envelope["manifest"]["scope"]["last"] = "2026-11-01"
        if failure in {"policy_revoked", "dependency_revoked", "validation_missing"}:
            p.registry_path.write_text(json.dumps(p.registry))
        assert c.get("/players/101").status_code == 503


def test_binding_versions_must_match(production):
    p = production
    binding = copy.deepcopy(p.envelope["binding"])
    binding.update(version="publication-binding/1", profile="core_stats_v1")
    with pytest.raises(ValueError, match="profile/version"):
        validate_binding(binding, p.envelope["manifest"])


def test_missing_authority_is_lazy_and_does_not_read_warehouse(production):
    p = production
    p.policy_path.unlink()
    reader = ProductionApplication(
        client=p.client, project=PROJECT, namespace=NS, authority=p.authority
    )
    with TestClient(reader) as c:
        assert c.get("/players/101").status_code == 503
    assert p.client.reads == 0


def test_expired_authority_does_not_prevent_launcher_or_legacy_startup(
    production, monkeypatch, tmp_path
):
    from app import main
    from google.cloud import bigquery
    from scripts.serve_production_candidate import create_config

    legacy = FastAPI()

    @legacy.get("/ping")
    def ping():
        return {"legacy": True}

    monkeypatch.setattr(main, "app", legacy)
    monkeypatch.setattr(bigquery, "Client", lambda **_: production.client)
    mode_path = tmp_path / "mode.json"
    mode_path.write_text('{"mode":"production"}')
    production.registry["valid_until"] = "2026-01-01T00:00:00Z"
    production.registry_path.write_text(json.dumps(production.registry))
    app = create_config(
        dict(
            project=PROJECT,
            namespace=NS,
            policy_path=str(production.policy_path),
            policy_digest=digest(production.policy),
            registry_path=str(production.registry_path),
            mode_path=str(mode_path),
        )
    )
    with TestClient(app) as client:
        assert client.get("/ping").json() == {"legacy": True}
        assert client.get("/publication-core/api/players/101").status_code == 503
    assert production.client.reads == 0


def test_cloud_serving_caches_bound_remote_work_and_never_extend_authority(production):
    from app.serving.control_cache import ControlCache

    p = production
    ticks = [0]
    calls = dict(queries=0, tables=0, downloads=0)
    originals = p.client.query, p.client.get_table

    def query(*args, **kwargs):
        calls["queries"] += 1
        return originals[0](*args, **kwargs)

    def table(*args, **kwargs):
        calls["tables"] += 1
        return originals[1](*args, **kwargs)

    class Blob:
        generation = 1

        def __init__(self, document):
            self.raw = json.dumps(document).encode()

        @property
        def size(self):
            return len(self.raw)

        def reload(self, **_):
            pass

        def download_as_bytes(self, **kwargs):
            assert kwargs["if_generation_match"] == self.generation
            calls["downloads"] += 1
            return self.raw

    blobs = {"policy": Blob(p.policy), "registry": Blob(p.registry)}
    storage = SimpleNamespace(bucket=lambda _: SimpleNamespace(blob=blobs.__getitem__))
    authority = SourceAuthority(
        policy_path="gs://control-bucket/policy",
        policy_digest=digest(p.policy),
        registry_path="gs://control-bucket/registry",
        clock=lambda: p.clock[0],
        storage_client=storage,
        cache_ttl=30,
    )
    authority.documents.timer = lambda: ticks[0]
    p.client.query, p.client.get_table = query, table
    reader = ProductionApplication(
        client=p.client, project=PROJECT, namespace=NS, authority=authority
    )
    reader.controls = ControlCache(5, timer=lambda: ticks[0])
    with TestClient(reader) as client:
        assert client.get("/api/players/101").status_code == 200
        cold = dict(calls)
        assert cold == dict(queries=2, tables=len(p.tables), downloads=2)
        for _ in range(3):
            assert client.get("/api/players/101").status_code == 200
        assert calls == cold
        ticks[0] = 6
        assert client.get("/api/players/101").status_code == 200
        assert calls == dict(queries=4, tables=2 * len(p.tables), downloads=2)
        # An authority clock deadline is enforced even inside the cache lease.
        p.clock[0] = p.registry["valid_until"]
        assert client.get("/api/players/101").status_code == 503
        p.clock[0] = NOW
        p.registry["policy_revoked"] = True
        blobs["registry"].generation += 1
        blobs["registry"].raw = json.dumps(p.registry).encode()
        ticks[0] = 31
        assert client.get("/api/players/101").status_code == 503


def test_failed_control_refresh_never_serves_expired_cached_value():
    from app.serving.control_cache import ControlCache

    ticks = [0]
    cache = ControlCache(5, limit=2, timer=lambda: ticks[0])
    assert cache.get("control", lambda: {"ok": True}) == {"ok": True}
    ticks[0] = 6

    def unavailable():
        raise ValueError("remote unavailable")

    with pytest.raises(ValueError, match="remote unavailable"):
        cache.get("control", unavailable)
    assert "control" not in cache.entries
    for key in range(3):
        cache.get(key, lambda: {"ok": True})
    assert list(cache.entries) == [1, 2]


def test_pointer_cache_is_bounded_and_pinned_flows_do_not_follow_new_pointer(
    production,
):
    from app.serving.control_cache import ControlCache

    p = production
    ticks, pointer = [0], [p.envelope["manifest"]["publication_id"]]
    original = p.client.query

    def query(sql, **kwargs):
        if ".pointer`" in sql:
            return SimpleNamespace(result=lambda **_: [dict(active_id=pointer[0])])
        if (
            kwargs["job_config"].query_parameters[0].value
            != p.envelope["manifest"]["publication_id"]
        ):
            return SimpleNamespace(result=lambda **_: [])
        return original(sql, **kwargs)

    p.client.query = query
    p.app.controls = ControlCache(5, timer=lambda: ticks[0])
    with TestClient(p.app) as client:
        first = client.get("/api/players/101")
        assert first.status_code == 200
        pid = first.headers["X-Publication-ID"]
        pointer[0] = "pub_ffffffffffffffff"
        # The brief pointer lease deliberately retains one complete valid release.
        assert client.get("/api/players/101").headers["X-Publication-ID"] == pid
        ticks[0] = 6
        assert client.get("/api/players/101").status_code == 503
        pinned = client.get("/api/players/101", headers={"X-Publication-ID": pid})
        assert pinned.status_code == 200
        assert pinned.headers["X-Publication-ID"] == pid


def test_unsupported_query_is_rejected_before_handler_runs(production):
    p = production
    core = p.app.application(None)
    calls = []

    @core.get("/expensive")
    def expensive():
        calls.append(True)
        return {"ok": True}

    with TestClient(core) as client:
        assert client.get("/expensive?unknown=1").status_code == 422
    assert not calls


def test_legacy_history_and_routes_survive_core_authority_loss(production):
    p = production
    legacy = FastAPI()

    @legacy.get("/api/agent/history")
    @legacy.get("/players/101")
    def prior():
        return {"legacy": True}

    app = CutoverApplication(
        legacy=legacy,
        core=p.app,
        mode=lambda: "production",
        legacy_routes=p.evidence["historical_routes"],
    )
    p.policy_path.unlink()
    with TestClient(app) as c:
        assert c.get("/api/agent/history").json() == {"legacy": True}
        assert c.get("/players/101?season=2024-25").json() == {"legacy": True}
        assert c.get("/players/101").status_code == 503


def test_route_resolution_is_pinned_before_dispatch(production, monkeypatch):
    p = production
    original = p.client.query
    pointers = []

    def query(sql, *args, **kwargs):
        if ".pointer`" in sql:
            pointers.append(sql)
            assert len(pointers) == 1, (
                "routing and reading must use one pointer resolution"
            )
        return original(sql, *args, **kwargs)

    monkeypatch.setattr(p.client, "query", query)
    with TestClient(
        CutoverApplication(legacy=FastAPI(), core=p.app, mode=lambda: "production")
    ) as c:
        assert c.get("/players/101").status_code == 200
    assert len(pointers) == 1


def test_operational_routes_remain_available_when_core_authority_fails(production):
    p = production
    legacy = FastAPI()

    @legacy.get("/jobs")
    @legacy.get("/api/jobs")
    @legacy.get("/api/jobs/runs/1/log")
    @legacy.get("/static/jobs.js")
    def operational():
        return {"served_by": "legacy"}

    app = CutoverApplication(legacy=legacy, core=p.app, mode=lambda: "production")
    p.policy_path.unlink()
    with TestClient(app) as client:
        for path in ("/jobs", "/api/jobs", "/api/jobs/runs/1/log", "/static/jobs.js"):
            assert client.get(path).json() == {"served_by": "legacy"}
        assert client.get("/players/101").status_code == 503


def test_mounted_jobs_monitor_is_preserved_during_cutover(production, tmp_path):
    from scripts.serve_production_candidate import compose_legacy

    legacy_app = FastAPI()
    legacy = compose_legacy(legacy_app, {"jobs_state_dir": str(tmp_path)})
    assert legacy_app.state.jobs_monitor_enabled is True
    app = CutoverApplication(
        legacy=legacy, core=production.app, mode=lambda: "production"
    )
    production.policy_path.unlink()
    with TestClient(
        app, base_url="http://127.0.0.1", client=("127.0.0.1", 123)
    ) as client:
        assert client.get("/jobs-monitor/jobs").status_code == 200
        assert client.get("/jobs-monitor/assets/jobs.js").status_code == 200


def test_composed_legacy_lifespan_runs(tmp_path):
    from contextlib import asynccontextmanager

    from scripts.serve_production_candidate import compose_legacy

    events = []

    @asynccontextmanager
    async def lifespan(_app):
        events.append("start")
        yield
        events.append("stop")

    legacy = FastAPI(lifespan=lifespan)
    with TestClient(compose_legacy(legacy, {"jobs_state_dir": str(tmp_path)})):
        assert events == ["start"]
    assert events == ["start", "stop"]
