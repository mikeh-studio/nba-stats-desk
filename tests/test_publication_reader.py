import copy
import json
from types import SimpleNamespace

import pytest
from app.serving.core_dbt import generate, isolated_dataset, staging_schema
from app.serving.publication import Resolver, ordered, validate_receipt
from app.serving.publication_application import PublicationApplication
from app.serving.publication_contract import PROFILE, digest, timestamp
from fastapi.testclient import TestClient
from scripts.build_synthetic_core_replay import synthetic_bundle

PROJECT, NS = "synthetic-project", "nba_phase4_synthetic_test"
NOW = "2026-11-01T16:00:00Z"


@pytest.fixture
def warehouse():
    bundle = synthetic_bundle()
    models = {n: ordered(n, r) for n, r in bundle["models"].items()}
    receipts, tables = {}, {}
    for i in [1, 2]:
        pid = f"pub_{i:016x}"
        assets = {}
        for n, rows in models.items():
            relation = f"{PROJECT}.{NS}_release_{pid[4:]}.{n}"
            assets[n] = dict(
                relation=relation,
                rows=len(rows),
                digest=digest(rows),
                etag=f"{pid}-{n}",
            )
            tables[relation] = SimpleNamespace(
                table_type="SNAPSHOT",
                etag=assets[n]["etag"],
                num_rows=len(rows),
                expires=timestamp("2026-11-03T00:00:00Z"),
                rows=rows,
            )
        receipt = dict(
            version="core-warehouse-rehearsal/1",
            purpose="isolated_warehouse_rehearsal",
            runtime_eligible=False,
            publication_id=pid,
            profile_digest=digest(PROFILE),
            semantics=PROFILE["semantics_version"],
            scope=PROFILE["scope"],
            validation_as_of=NOW,
            expires_at="2026-11-02T00:00:00Z",
            source_valid_until="2026-11-09T00:00:00Z",
            input_digest=digest(bundle["accepted"]),
            assets=assets,
        )
        receipts[pid] = dict(
            publication_id=pid,
            eligible=True,
            revoked=False,
            manifest=json.dumps(receipt),
            manifest_digest=digest(receipt),
        )

    class Client:
        active = "pub_0000000000000001"
        reads = 0

        def query(self, sql, job_config, **_):
            assert job_config.maximum_bytes_billed == 268435456
            if ".pointer`" in sql:
                result = [dict(active_id=self.active)]
            else:
                result = (
                    [receipts[job_config.query_parameters[0].value]]
                    if job_config.query_parameters[0].value in receipts
                    else []
                )
            return SimpleNamespace(result=lambda **_: result)

        def get_table(self, relation, **_):
            return tables[relation]

        def list_rows(self, table, **_):
            self.reads += 1
            return copy.deepcopy(table.rows)

    c = Client()
    clock = [NOW]
    return (
        Resolver(c, project=PROJECT, namespace=NS, clock=lambda: clock[0]),
        receipts,
        tables,
        clock,
    )


def test_pointer_swap_pins_old_flow_cache_and_explicit_token(warehouse):
    resolver, _, _, _ = warehouse
    a = resolver.resolve()
    rows = a.asset("fct_player_game_stats")
    resolver.client.active = "pub_0000000000000002"
    b = resolver.resolve()
    assert a.publication_id != b.publication_id and a.cache_key != b.cache_key
    assert a.asset("fct_player_game_stats") == rows
    assert resolver.client.reads == 1
    assert resolver.resolve(a.publication_id).cache_key == a.cache_key


@pytest.mark.parametrize(
    "failure", ["revoked", "expired", "changed", "missing", "digest", "table_type"]
)
def test_cached_release_fails_closed(warehouse, failure):
    r, registry, tables, clock = warehouse
    a = r.resolve()
    a.asset("fct_player_game_stats")
    entry = registry[a.publication_id]
    table = tables[a.receipt["assets"]["fct_player_game_stats"]["relation"]]
    if failure == "revoked":
        entry["revoked"] = True
    elif failure == "expired":
        clock[0] = "2026-11-04T00:00:00Z"
    elif failure == "changed":
        table.etag = "replacement"
    elif failure == "missing":
        table.num_rows = 0
    elif failure == "digest":
        entry["manifest_digest"] = "0" * 64
    elif failure == "table_type":
        table.table_type = "TABLE"
    with pytest.raises(ValueError):
        a.asset("fct_player_game_stats")


def test_actual_warehouse_composition_routes_and_old_token(warehouse):
    resolver, registry, _, _ = warehouse
    with TestClient(
        PublicationApplication(resolver),
        base_url="http://127.0.0.1",
        client=("127.0.0.1", 12345),
    ) as c:
        for path in [
            "/players/101",
            "/compare?player_a_id=101&player_b_id=202",
            "/performance",
            "/ask",
            "/api/players/search?q=Test",
        ]:
            response = c.get(path)
            assert response.status_code == 200, response.text
            assert response.headers["x-publication-id"] == "pub_0000000000000001"
        resolver.client.active = "pub_0000000000000002"
        assert (
            c.get("/api/health").headers["x-publication-id"] == resolver.client.active
        )
        response = c.get(
            "/api/health", headers={"x-publication-id": "pub_0000000000000001"}
        )
        assert (
            response.status_code == 200
            and response.headers["x-publication-id"] == "pub_0000000000000001"
        )
        registry["pub_0000000000000001"]["revoked"] = True
        assert (
            c.get(
                "/api/health", headers={"x-publication-id": "pub_0000000000000001"}
            ).status_code
            == 503
        )
        assert c.get("/api/health?season=2025-26").status_code == 422


@pytest.mark.parametrize(
    "name",
    [
        "nba_gold",
        "release_0000000000000000",
        "nba_phase4_x`;DROP TABLE x",
        "nba_phase4_x",
    ],
)
def test_production_or_untrusted_target_rejected(name):
    with pytest.raises(ValueError):
        isolated_dataset(name)


def test_generated_dbt_project_has_no_legacy_defaults(tmp_path):
    root = tmp_path / "project"
    sql = generate(
        root,
        project=PROJECT,
        staging=NS + "_input",
        candidate=NS + "_build",
        materialized_at=NOW,
    )
    assert len(sql) == 13
    profile = json.loads((root / "profiles.yml").read_text())["core_rehearsal"][
        "outputs"
    ]["isolated"]
    assert profile["dataset"] == NS + "_build"
    assert profile["maximum_bytes_billed"] == 268435456
    assert not list(root.glob("macros/*"))
    assert staging_schema("games")["game_date"] == "DATE"
    assert staging_schema("appearances")["source_captured_at"] == "TIMESTAMP"
    assert staging_schema("appearances")["pts"] == "INT64"
    with pytest.raises(ValueError):
        generate(
            root,
            project=PROJECT,
            staging=NS + "_input",
            candidate=NS + "_build",
            materialized_at=NOW,
        )


def test_receipt_cannot_select_other_project_or_production(warehouse):
    r, _, _, _ = warehouse
    receipt = r.resolve().receipt
    receipt["assets"]["dim_team"]["relation"] = PROJECT + ".nba_gold.dim_team"
    with pytest.raises(ValueError):
        validate_receipt(receipt, project=PROJECT, namespace=NS, now=NOW)


@pytest.mark.parametrize("warm", [False, True])
@pytest.mark.parametrize("failure", ["deleted", "replacement", "registry"])
def test_actual_cloud_errors_and_warm_app_fail_unavailable(warehouse, warm, failure):
    from google.api_core.exceptions import NotFound, ServiceUnavailable

    resolver, _, tables, _ = warehouse
    with TestClient(
        PublicationApplication(resolver),
        base_url="http://127.0.0.1",
        client=("127.0.0.1", 12345),
    ) as c:
        if warm:
            assert c.get("/api/health").status_code == 200

        def unavailable(*args, **kwargs):
            raise (
                NotFound("removed")
                if failure == "deleted"
                else ServiceUnavailable("reader outage")
            )

        if failure == "registry":
            resolver.client.query = unavailable
        elif failure == "deleted":
            resolver.client.get_table = unavailable
        else:
            for table in tables.values():
                table.etag = "replacement"
        assert c.get("/api/health").status_code == 503


def test_preload_during_activation_keeps_request_binding(warehouse):
    resolver, _, _, _ = warehouse
    original = resolver.client.list_rows

    def swap_during_preload(*args, **kwargs):
        resolver.client.active = "pub_0000000000000002"
        return original(*args, **kwargs)

    resolver.client.list_rows = swap_during_preload
    with TestClient(
        PublicationApplication(resolver),
        base_url="http://127.0.0.1",
        client=("127.0.0.1", 12345),
    ) as c:
        assert (
            c.get("/api/health").headers["x-publication-id"] == "pub_0000000000000001"
        )
        assert (
            c.get("/api/health").headers["x-publication-id"] == "pub_0000000000000002"
        )
