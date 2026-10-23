from dataclasses import replace
from datetime import datetime, timezone

import pytest
from app.agent.semantic_source import (
    BigQuerySemanticSource,
    snapshot_digest,
    snapshot_evidence,
)
from app.agent.semantics import Query, SemanticError, run_query
from app.agent.serving_runtime import PilotPublication, ServingShadow
from tests.test_semantic_source import Client

NOW = datetime(2026, 10, 22, tzinfo=timezone.utc)


@pytest.fixture
def bundle():
    snapshot = BigQuerySemanticSource(Client(), project="test-project").capture(
        ["2024-25"]
    )
    snapshot["source_timestamp"] = NOW.isoformat()
    snapshot["sha256"] = snapshot_digest(snapshot)
    publication = PilotPublication.build(
        snapshot,
        expected_snapshot_id=snapshot["sha256"],
        publication_id="pilot",
        relation_version="fixture/1",
        now=NOW,
    )
    return snapshot_evidence(snapshot), publication


@pytest.mark.parametrize("phase", ["Regular Season", "Playoffs", "Both"])
@pytest.mark.parametrize("aggregation", ["average", "total"])
@pytest.mark.parametrize("player", [1, 2, 3, 999])
def test_full_result_parity(bundle, phase, aggregation, player):
    evidence, pub = bundle
    query = Query(
        metric="pts",
        season="2024-25",
        aggregation=aggregation,
        season_type=phase,
        player_id=player,
    )
    assert pub.run(query, now=NOW) == run_query(evidence, query)


@pytest.mark.parametrize(
    "changes",
    [
        dict(opponent_abbr="AAA"),
        dict(min_games=2),
        dict(as_of="2025-04-01"),
        dict(operation="rank"),
        dict(player_id=None),
    ],
)
def test_unsupported_scope(bundle, changes):
    _, pub = bundle
    query = Query(metric="pts", season="2024-25", aggregation="average", player_id=1)
    assert pub.run(replace(query, **changes), now=NOW) is None


def test_integrity_and_expiry_fail_visibly(bundle):
    _, pub = bundle
    q = Query(metric="pts", season="2024-25", aggregation="average", player_id=1)
    for bad in [
        replace(pub, summaries=pub.summaries[:-1]),
        replace(pub, metadata_json="{}"),
    ]:
        with pytest.raises(SemanticError):
            bad.run(q, now=NOW)
    with pytest.raises(SemanticError):
        pub.run(q, now=datetime(2026, 10, 23, tzinfo=timezone.utc))


def test_shadow_preserves_result_and_records_mismatch(bundle):
    from app.agent.semantic_planner import execute_plan
    from app.agent.semantic_serving import source_players

    e, p = bundle
    shadow = ServingShadow(p, lambda: NOW)
    plan = dict(
        status="query",
        queries=[
            dict(
                metric="pts",
                season="2024-25",
                aggregation="average",
                player_name="Regular Player",
            )
        ],
    )
    actual = execute_plan(plan, e, source_players(e), shadow=shadow)
    assert actual == execute_plan(plan, e, source_players(e))
    assert shadow.outcomes == [dict(status="match")]
    shadow.compare(
        Query(metric="pts", season="2024-25", aggregation="average", player_id=1), {}
    )
    assert shadow.outcomes[-1] == dict(status="mismatch")


def test_atomic_development_publication(tmp_path):
    from app.agent.serving_store import DevelopmentPublications

    snapshot = BigQuerySemanticSource(Client(), project="test-project").capture(
        ["2024-25"]
    )
    snapshot["source_timestamp"] = NOW.isoformat()
    snapshot["sha256"] = snapshot_digest(snapshot)
    store = DevelopmentPublications(tmp_path)
    first = store.publish(
        snapshot,
        expected_snapshot_id=snapshot["sha256"],
        expected_generation=None,
        now=NOW,
    )
    assert (
        store.load(
            expected_snapshot_id=snapshot["sha256"], now=NOW
        ).approval.publication_id
        == first
    )
    with pytest.raises(SemanticError, match="generation"):
        store.publish(
            snapshot,
            expected_snapshot_id=snapshot["sha256"],
            expected_generation=None,
            now=NOW,
        )
    assert (
        store.load(
            expected_snapshot_id=snapshot["sha256"], now=NOW
        ).approval.publication_id
        == first
    )
    with pytest.raises(SemanticError):
        store.load(expected_snapshot_id="incorrect", now=NOW)
    (tmp_path / f"{first}.json").write_text("{}")
    with pytest.raises(SemanticError):
        store.load(expected_snapshot_id=snapshot["sha256"], now=NOW)


@pytest.mark.parametrize("suffix", ["", "/stream"])
def test_shadow_through_real_ask_handler(bundle, suffix):
    import json
    from types import SimpleNamespace

    from app.agent.semantic_serving import source_players
    from app.main import app, get_agent_client, get_repository, get_settings
    from app.repository import BigQueryWarehouseRepository
    from fastapi.testclient import TestClient
    from tests.test_semantic_migration import Model, settings

    evidence, pub = bundle
    shadow = ServingShadow(pub, lambda: NOW)

    class Warehouse:
        serving_shadow = shadow

        def load(self, seasons):
            return {"capture": {"query_count": 0}}, evidence

        def players(self, evidence):
            return source_players(evidence)

    config = settings()
    repo = BigQueryWarehouseRepository(config, client=SimpleNamespace())
    repo._governed_warehouse = Warehouse()
    previous = dict(app.dependency_overrides)
    app.dependency_overrides.update(
        {
            get_repository: lambda: repo,
            get_settings: lambda: config,
            get_agent_client: lambda: Model(),
        }
    )
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/agent/ask" + suffix + "?season=2024-25",
                json={
                    "question": "Regular Player points per game in the 2024-25 regular season"
                },
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
        assert shadow.outcomes == [dict(status="match")]
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


def test_benchmark_reports_limits():
    from scripts.ask_eval.serving import measure_pilot

    snapshot = BigQuerySemanticSource(Client(), project="test-project").capture(
        ["2024-25"]
    )
    snapshot["source_timestamp"] = NOW.isoformat()
    snapshot["sha256"] = snapshot_digest(snapshot)
    report = measure_pilot(snapshot, now=NOW, repetitions=2)
    assert report["passed"] and len(report["records"]) == 12
    assert not report["endpoint_latency_measured"]
    assert not report["warehouse_cost_measured"]


@pytest.mark.parametrize("variant", ["missing", "absent_playoffs", "trade"])
def test_adversarial_cohort_parity(variant):
    snapshot = BigQuerySemanticSource(Client(), project="test-project").capture(
        ["2024-25"]
    )
    if variant == "missing":
        for row in snapshot["rows"]:
            if row["player_id"] == 1:
                row["pts"] = None
    elif variant == "trade":
        snapshot["rows"][0]["team_abbr"] = "NEW"
    else:
        playoff = next(r for r in snapshot["rows"] if r["season_type"] == "Playoffs")
        snapshot["rows"].append(
            dict(playoff, player_id=999, player_name="Other playoff player")
        )
        snapshot["rows"] = [
            r
            for r in snapshot["rows"]
            if not (r["player_id"] == 1 and r["season_type"] == "Playoffs")
        ]
        for coverage in snapshot["coverage"]:
            rows = [
                r
                for r in snapshot["rows"]
                if r["season"] == coverage["season"]
                and r["season_type"] == coverage["phase"]
            ]
            coverage["rows"] = len(rows)
            coverage["data_through"] = max(r["game_date"] for r in rows)
    snapshot["source_timestamp"] = NOW.isoformat()
    snapshot["sha256"] = snapshot_digest(snapshot)
    source = snapshot_evidence(snapshot)
    pub = PilotPublication.build(
        snapshot,
        expected_snapshot_id=source.snapshot_id,
        publication_id="a",
        relation_version="a",
        now=NOW,
    )
    for phase in ["Regular Season", "Playoffs", "Both"]:
        q = Query(
            metric="pts",
            season="2024-25",
            season_type=phase,
            aggregation="average",
            player_id=1,
        )
        assert pub.run(q, now=NOW) == run_query(source, q)


def test_one_fresh_contract_per_consumption(bundle, monkeypatch):
    from app.agent import serving_publication

    evidence, pub = bundle
    original = serving_publication.load_contract
    calls = []

    def load():
        calls.append(True)
        return original()

    monkeypatch.setattr(serving_publication, "load_contract", load)
    query = Query(metric="pts", season="2024-25", aggregation="average", player_id=1)
    assert pub.run(query, now=NOW) == run_query(evidence, query)
    assert len(calls) == 1
    pub.run(query, now=NOW)
    assert len(calls) == 2  # approval does not survive into the next consumption


def test_changed_contract_is_detected_on_next_consumption(bundle, monkeypatch):
    from app.agent import serving_publication

    _, pub = bundle
    original = serving_publication.load_contract()
    query = Query(metric="pts", season="2024-25", aggregation="average", player_id=1)
    assert pub.run(query, now=NOW) is not None
    monkeypatch.setattr(
        serving_publication,
        "load_contract",
        lambda: replace(original, min_games=original.min_games + 1),
    )
    with pytest.raises(SemanticError, match="binding changed"):
        pub.run(query, now=NOW)


def test_mutable_summary_components_rechecked_each_time(bundle):
    _, pub = bundle
    query = Query(metric="pts", season="2024-25", aggregation="average", player_id=1)
    pub.run(query, now=NOW)
    pub.summaries[0].components["pts"] += 1
    with pytest.raises(SemanticError, match="binding changed"):
        pub.run(query, now=NOW)


def test_points_only_cohort_reuse_preserves_full_result(bundle):
    evidence, publication = bundle
    query = Query(metric="pts", aggregation="average", season="2024-25", player_id=1)
    expected = run_query(evidence, query)
    assert publication.run(query, now=NOW, reuse_cohort=True) == expected
    assert publication.run(query, now=NOW, reuse_cohort=True) == expected
    assert len(publication._cohorts) == 1
