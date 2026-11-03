import json
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest
from app.agent.semantics import Query, SemanticError, run_query
from app.agent.serving_bigquery import (
    BigQueryServingReader,
    DevelopmentBigQueryPublisher,
    ServingReference,
    encode_publication,
)
from app.agent.serving_publication import digest
from app.agent.serving_warehouse import ServingWarehouse
from tests.test_serving_expansion import make_prepared
from tests.test_serving_runtime import NOW


@pytest.fixture
def prepared():
    return make_prepared()


TABLE = "ask_serving_" + "a" * 32


class Client:
    def __init__(self, records):
        self.records = deepcopy(records)
        self.calls = []

    def query(self, sql, **kwargs):
        self.calls.append((sql, kwargs))
        detail = "WHERE kind = 'detail'" in sql
        rows = [dict(r) for r in self.records if (r["kind"] == "detail") == detail]
        player = next(
            (
                p.value
                for p in kwargs["job_config"].query_parameters
                if p.name == "player_id"
            ),
            None,
        )
        if player is not None:
            rows = [r for r in rows if r.get("player_id") == player]
        for r in rows:
            r["record_count"] = len(rows)
        return SimpleNamespace(
            total_bytes_processed=100,
            total_bytes_billed=100,
            cache_hit=False,
            result=lambda **kw: rows,
        )

    def create_table(self, table):
        self.table = table

    def load_table_from_json(self, records, table, **kwargs):
        assert kwargs["job_config"].write_disposition == "WRITE_EMPTY"
        self.records = deepcopy(records)
        return SimpleNamespace(output_rows=len(records), result=lambda **kw: None)


def wire(pub):
    header, records = encode_publication(pub, now=NOW, relation_id=TABLE)
    ref = ServingReference(
        "test-project",
        "nba_agent_dev",
        TABLE,
        digest(header),
        pub.approval.source_snapshot_id,
        tuple(sorted(pub.approval.scopes)),
    )
    return ref, records


def test_read_only_core_does_not_load_detail_and_bounded(prepared):
    evidence, pub = prepared
    ref, records = wire(pub)
    client = Client(records)
    reader = BigQueryServingReader(client, ref)
    loaded = reader.load(now=NOW)
    assert loaded.detail_json is None
    q = Query(metric="ast", season="2024-25", aggregation="average", player_id=1)
    assert loaded.run_expanded(q, now=NOW) == run_query(evidence, q)
    reader.load(now=NOW)
    assert len(client.calls) == 1
    complete = reader.load(now=NOW, include_detail=True)
    assert complete.detail_evidence(now=NOW) == evidence
    assert len(client.calls) == 2
    assert all(
        call[1]["job_config"].maximum_bytes_billed == 50_000_000
        for call in client.calls
    )
    assert all("@row_limit" in call[0] for call in client.calls)


@pytest.mark.parametrize(
    "change", ["summary", "metadata", "missing", "duplicate", "hash", "relation"]
)
def test_bad_wire_fails_closed(prepared, change):
    _, pub = prepared
    ref, records = wire(pub)
    if change in ("summary", "metadata"):
        row = next(r for r in records if r["kind"] == change)
        row["record_json"] = "{}"
    elif change == "missing":
        records.pop(2)
    elif change == "duplicate":
        records.append(dict(records[2]))
    elif change == "hash":
        ref = replace(ref, manifest_digest="0" * 64)
    else:
        ref = replace(ref, table="other_table")
    with pytest.raises(SemanticError):
        BigQueryServingReader(Client(records), ref).load(now=NOW)


def test_atomic_development_publisher_roundtrip(prepared):
    _, pub = prepared
    client = Client([])
    publisher = DevelopmentBigQueryPublisher(
        client, project="test-project", dataset="nba_agent_dev"
    )
    ref = publisher.publish(pub, now=NOW)
    assert client.table.expires is not None
    assert (
        BigQueryServingReader(client, ref)
        .load(now=NOW, include_detail=True)
        .detail_json
        == pub.detail_json
    )
    with pytest.raises(ValueError):
        DevelopmentBigQueryPublisher(
            client, project="test-project", dataset="nba_agent"
        )


def test_prepared_scalar_never_loads_facts(prepared):
    evidence, pub = prepared
    ref, records = wire(pub)

    class Facts:
        def load(self, *a):
            raise AssertionError("Fact preload is forbidden in serving summary")

    warehouse = ServingWarehouse(
        Facts(), BigQueryServingReader(Client(records), ref), lambda: NOW, mode="serve"
    )
    plan = dict(
        status="query",
        queries=[
            dict(
                metric="pts",
                aggregation="average",
                season="2024-25",
                player_name="Regular Player",
            )
        ],
    )
    result = warehouse.prepare(lambda players, teams: plan)
    assert result.result["status"] == "ok"
    assert result.result["evidence"] == run_query(
        evidence,
        Query(metric="pts", aggregation="average", season="2024-25", player_id=1),
    )


@pytest.mark.parametrize("suffix", ["", "/stream"])
@pytest.mark.parametrize(
    "question",
    [
        "Regular Player points per game in 2024-25 regular season",
        "Top 3 players by assists per game in 2024-25 regular season with at least 1 games",
    ],
)
def test_serving_actual_handlers_without_fact_preload(prepared, suffix, question):
    from app.main import app, get_agent_client, get_repository, get_settings
    from app.repository import BigQueryWarehouseRepository
    from fastapi.testclient import TestClient
    from tests.support.semantic import settings

    evidence, pub = prepared
    ref, records = wire(pub)
    client = Client(records)

    class Facts:
        def load(self, *args):
            raise AssertionError("Serving scalar must not preload facts")

    repo = BigQueryWarehouseRepository(settings(), client=SimpleNamespace())
    repo._governed_warehouse = ServingWarehouse(
        Facts(),
        BigQueryServingReader(client, ref),
        lambda: NOW,
        mode="serve",
        families=("points", "rankings"),
    )
    config = replace(
        settings(),
        ask_deterministic_planning_enabled=True,
        performance_cache_prewarm_enabled=False,
    )
    old = dict(app.dependency_overrides)
    app.dependency_overrides.update(
        {
            get_settings: lambda: config,
            get_repository: lambda: repo,
            get_agent_client: lambda: object(),
        }
    )
    try:
        with TestClient(app) as http:
            response = http.post(
                "/api/agent/ask" + suffix + "?season=2024-25",
                json={"question": question},
            )
        response.raise_for_status()
        if suffix:
            events = [
                json.loads(line[6:])
                for line in response.text.splitlines()
                if line.startswith("data: ")
            ]
            payload = next(e["payload"] for e in events if e.get("type") == "final")
        else:
            payload = response.json()
        assert payload["status"] == "ok"
        assert payload["semantic_plan"]["model_calls"] == 0
        assert (
            payload["semantic_evidence"]["provenance"]["snapshot_id"]
            == evidence.snapshot_id
        )
        assert len(client.calls) == 1
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(old)


@pytest.mark.parametrize("player", [1, 2, 999])
@pytest.mark.parametrize("phase", ["Regular Season", "Both"])
def test_player_bound_game_log_matches_complete_fact_scope(prepared, player, phase):
    evidence, pub = prepared
    ref, records = wire(pub)
    client = Client(records)
    reader = BigQueryServingReader(client, ref)
    query = Query(
        metric="pts",
        season="2024-25",
        season_type=phase,
        operation="game_log",
        aggregation="total",
        player_id=player,
        window="last_n_games",
        n=2,
    )
    assert reader.run_game_log(query, now=NOW) == run_query(evidence, query)
    if len(client.calls) == 2:
        assert "player_id = @player_id" in client.calls[1][0]
        assert client.calls[1][1]["job_config"].query_parameters[0].value == 1001


def test_reader_cache_single_flight_and_rechecks_freshness(prepared):
    from concurrent.futures import ThreadPoolExecutor
    from datetime import timedelta

    _, pub = prepared
    ref, records = wire(pub)
    client = Client(records)
    reader = BigQueryServingReader(client, ref)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: reader.load(now=NOW), range(8)))
    assert len(client.calls) == 1 and all(p is results[0] for p in results)
    with pytest.raises(SemanticError):
        reader.load(now=NOW + timedelta(seconds=301))
    assert len(client.calls) == 1


def test_permission_failure_does_not_fall_back_to_facts(prepared):
    from google.api_core.exceptions import Forbidden

    _, pub = prepared
    ref, records = wire(pub)

    class Denied(Client):
        def query(self, *args, **kwargs):
            raise Forbidden("private test body")

    class Facts:
        def load(self, *args):
            raise AssertionError("Denied serving reads must stay visible")

    warehouse = ServingWarehouse(
        Facts(), BigQueryServingReader(Denied(records), ref), lambda: NOW, mode="serve"
    )
    with pytest.raises(SemanticError) as error:
        warehouse.prepare(lambda *_: {})
    assert error.value.code == "access_denied"


def test_player_game_cache_reads_once_and_checks_manifest(prepared):
    _, pub = prepared
    ref, records = wire(pub)
    client = Client(records)
    reader = BigQueryServingReader(client, ref)
    query = Query(
        metric="pts",
        season="2024-25",
        aggregation="total",
        operation="game_log",
        player_id=1,
    )
    assert reader.run_game_log(query, now=NOW) == reader.run_game_log(query, now=NOW)
    assert len(client.calls) == 2
    reader._header["player_details"]["1"]["digest"] = "0" * 64
    with pytest.raises(SemanticError):
        reader.run_game_log(query, now=NOW)


def test_atomic_reference_rotation_without_restart(prepared, tmp_path):
    from dataclasses import asdict

    from app.agent.serving_warehouse import RefreshingServingWarehouse

    _, pub = prepared
    first, records_a = wire(pub)
    table_b = "ask_serving_" + "b" * 32
    header_b, records_b = encode_publication(pub, now=NOW, relation_id=table_b)
    second = replace(first, table=table_b, manifest_digest=digest(header_b))
    path = tmp_path / "reference.json"
    path.write_text(json.dumps(asdict(first)))

    class Versions(Client):
        def query(self, sql, **kwargs):
            self.records = records_b if table_b in sql else records_a
            return super().query(sql, **kwargs)

    client = Versions(records_a)
    warehouse = RefreshingServingWarehouse(
        object(), client, path, lambda: NOW, project="test-project", mode="serve"
    )
    plan = dict(
        status="query",
        queries=[
            dict(
                metric="pts",
                aggregation="average",
                season="2024-25",
                player_name="Regular Player",
            )
        ],
    )
    assert warehouse.prepare(lambda *_: plan).result["status"] == "ok"
    assert warehouse.prepare(lambda *_: plan).result["status"] == "ok"
    assert len(client.calls) == 1
    temporary = tmp_path / "new.json"
    temporary.write_text(json.dumps(asdict(second)))
    temporary.replace(path)
    assert warehouse.prepare(lambda *_: plan).result["status"] == "ok"
    assert len(client.calls) == 2 and table_b in client.calls[-1][0]
    path.write_text("{}")
    with pytest.raises(SemanticError):
        warehouse.prepare(lambda *_: plan)
    assert len(client.calls) == 2  # never mask the bad pointer with an old cache
