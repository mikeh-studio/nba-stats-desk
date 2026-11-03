"""Routing expectations independent of serving implementation details."""

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from threading import Barrier

import pytest
from app.agent.common_planner import common_plan
from app.agent.execution import optional_model_budget, request_policy
from app.agent.observability import AgentTrace
from app.agent.semantic_serving import source_players
from app.agent.semantics import Query, run_query
from app.agent.serving_bigquery import encode_publication
from app.agent.serving_publication import digest
from app.agent.serving_runtime import PilotPublication
from app.agent.serving_warehouse import RefreshingServingWarehouse, ServingWarehouse
from tests.support.semantic import settings
from tests.test_serving_bigquery import TABLE, Client, wire
from tests.test_serving_expansion import make_prepared
from tests.test_serving_runtime import NOW


@pytest.fixture
def prepared():
    return make_prepared()


@pytest.fixture
def points(prepared):
    evidence, expanded = prepared
    snapshot = json.loads(expanded.detail_json)
    return evidence, PilotPublication.build(
        snapshot,
        expected_snapshot_id=snapshot["sha256"],
        publication_id="points-routing",
        relation_version="points-routing/1",
        now=NOW,
    )


class Facts:
    def __init__(self, evidence):
        self.evidence = evidence
        self.calls = []

    def load(self, seasons):
        self.calls.append(tuple(seasons))
        return {"capture": {"query_count": 0}}, self.evidence

    def players(self, evidence):
        return source_players(evidence)


class Reader:
    def __init__(self, pub):
        self.pub = pub
        self.calls = []

    def load(self, *, now, include_detail=False):
        self.calls.append(include_detail)
        assert not include_detail, (
            "An undeclared detail capability is not corrupt evidence"
        )
        return self.pub


def ask(
    warehouse,
    question,
    suffix,
    *,
    model_client=None,
    config=None,
    previous_context=None,
):
    from app.main import app, get_agent_client, get_repository, get_settings
    from app.repository import BigQueryWarehouseRepository
    from fastapi.testclient import TestClient

    config = replace(
        config or settings(),
        ask_deterministic_planning_enabled=True,
        agent_history_enabled=False,
        performance_cache_prewarm_enabled=False,
    )
    repo = BigQueryWarehouseRepository(config, client=object())
    repo._governed_warehouse = warehouse
    old = dict(app.dependency_overrides)
    app.dependency_overrides.update(
        {
            get_settings: lambda: config,
            get_repository: lambda: repo,
            get_agent_client: lambda: model_client or object(),
        }
    )
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/agent/ask" + suffix + f"?season={config.season}",
                json={"question": question, "previous_context": previous_context},
            )
        response.raise_for_status()
        if not suffix:
            return response.json()
        events = [
            json.loads(line[6:])
            for line in response.text.splitlines()
            if line.startswith("data: ")
        ]
        return next(e["payload"] for e in events if e.get("type") == "final")
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(old)


@pytest.mark.parametrize("suffix", ["", "/stream"])
@pytest.mark.parametrize("broken", ["missing", "invalid"])
def test_broken_shadow_reference_preserves_actual_fact_answer(
    prepared, tmp_path, caplog, suffix, broken
):
    evidence, _ = prepared
    facts = Facts(evidence)
    path = tmp_path / "reference.json"
    if broken == "invalid":
        path.write_text("{}")
    warehouse = RefreshingServingWarehouse(
        facts, object(), path, lambda: NOW, project="test-project", mode="shadow"
    )
    with caplog.at_level(logging.INFO, logger="app.agent"):
        payload = ask(
            warehouse,
            "Regular Player points per game in 2024-25 regular season",
            suffix,
        )
    assert payload["status"] == "ok"
    assert payload["semantic_evidence"] == run_query(
        evidence,
        Query(metric="pts", aggregation="average", season="2024-25", player_id=1),
    )
    assert facts.calls == [("2024-25",)]
    summaries = [
        json.loads(r.message)
        for r in caplog.records
        if r.name == "app.agent" and r.message.startswith('{"ask_route"')
    ]
    assert summaries[-1]["serving_shadow"] == [
        {"status": "error", "code": "invalid_evidence"}
    ]


@pytest.mark.parametrize("suffix", ["", "/stream"])
def test_uncovered_season_uses_historical_fact_identity(prepared, suffix):
    evidence, pub = prepared
    historical = replace(
        evidence,
        rows=[
            dict(
                r,
                season="2023-24",
                game_date=r["game_date"].replace("2025", "2024"),
                player_name="Historical Player"
                if r["player_id"] == 1
                else r["player_name"],
            )
            for r in evidence.rows
        ],
        covered_scopes=frozenset(("2023-24", p) for _, p in evidence.covered_scopes),
        data_through={
            ("2023-24", p): d.replace("2025", "2024")
            for (_, p), d in evidence.data_through.items()
        },
        snapshot_id="synthetic-history",
    )
    facts = Facts(historical)
    reader = Reader(pub)
    warehouse = ServingWarehouse(
        facts, reader, lambda: NOW, mode="serve", families=("points", "rich")
    )
    payload = ask(
        warehouse,
        "Historical Player points per game in 2023-24 regular season",
        suffix,
    )
    assert payload["status"] == "ok"
    assert payload["semantic_evidence"]["scope"]["season"] == "2023-24"
    assert payload["semantic_evidence"]["rows"][0]["player_id"] == 1
    assert (
        payload["semantic_evidence"]["provenance"]["snapshot_id"] == "synthetic-history"
    )
    assert facts.calls == [("2023-24",)]
    assert reader.calls == [False]


def test_scope_preflight_precedes_planner(prepared):
    evidence, pub = prepared
    warehouse = ServingWarehouse(
        Facts(evidence), Reader(pub), lambda: NOW, mode="serve"
    )

    def forbidden(*args):
        raise AssertionError("An unrelated serving catalog must not resolve identity")

    assert warehouse.prepare(forbidden, seasons=["2023-24"]) is None


def test_undeclared_detail_is_capability_miss(points):
    evidence, pub = points
    reader = Reader(pub)
    warehouse = ServingWarehouse(
        Facts(evidence),
        reader,
        lambda: NOW,
        mode="serve",
        families=("points", "detail"),
    )
    plan = dict(
        status="query",
        queries=[
            dict(
                metric="pts",
                aggregation="average",
                season="2024-25",
                player_name="Regular Player",
                window="last_n_games",
                n=2,
            )
        ],
    )
    attempt = warehouse.prepare(lambda *_: plan, seasons=["2024-25"])
    assert attempt.result is None
    assert reader.calls == [False]


def test_points_shadow_is_measured_and_request_local(points):
    evidence, pub = points
    warehouse = ServingWarehouse(
        Facts(evidence), Reader(pub), lambda: NOW, mode="shadow"
    )
    barrier = Barrier(2)

    def request(player):
        trace = AgentTrace(str(player), "private question", "fixture")
        with request_policy(settings(), "openai", trace):
            _, e = warehouse.load(["2024-25"])
            candidate = warehouse.serving_shadow
            barrier.wait(timeout=5)
            query = Query(
                metric="pts", aggregation="average", season="2024-25", player_id=player
            )
            candidate.compare(query, run_query(e, query))
            assert warehouse.serving_shadow is candidate
            assert trace.serving_shadow == [{"status": "match"}]
            return id(candidate)

    with ThreadPoolExecutor(max_workers=2) as pool:
        identities = list(pool.map(request, [1, 2]))
    assert len(set(identities)) == 2


def test_reference_pinned_for_whole_request_including_optional_budget(
    prepared, tmp_path
):
    evidence, pub = prepared
    first, a = wire(pub)
    other_table = "ask_serving_" + "b" * 32
    header, b = encode_publication(pub, now=NOW, relation_id=other_table)
    second = replace(first, table=other_table, manifest_digest=digest(header))
    path = tmp_path / "reference.json"
    path.write_text(json.dumps(asdict(first)))

    class Versions(Client):
        def query(self, sql, **kwargs):
            self.records = b if other_table in sql else a
            return super().query(sql, **kwargs)

    client = Versions(a)
    warehouse = RefreshingServingWarehouse(
        Facts(evidence), client, path, lambda: NOW, project="test-project", mode="serve"
    )

    def planner(players, _):
        return common_plan(
            "Regular Player points per game in 2024-25 regular season", players=players
        )

    with request_policy(settings(), "openai"):
        assert warehouse.prepare(planner, seasons=["2024-25"]).result["status"] == "ok"
        temporary = tmp_path / "new.json"
        temporary.write_text(json.dumps(asdict(second)))
        temporary.replace(path)
        with optional_model_budget(2):
            assert (
                warehouse.prepare(planner, seasons=["2024-25"]).result["status"] == "ok"
            )
        assert len(client.calls) == 1 and TABLE in client.calls[0][0]
    with request_policy(settings(), "openai"):
        assert warehouse.prepare(planner, seasons=["2024-25"]).result["status"] == "ok"
        assert len(client.calls) == 2 and other_table in client.calls[-1][0]


def test_shadow_telemetry_is_bounded_and_contains_no_question(points):
    from app.agent.execution import record_serving_shadow

    trace = AgentTrace("id", "private user question", "fixture")
    with request_policy(settings(), "openai", trace):
        for _ in range(65):
            record_serving_shadow({"status": "error", "code": "invalid_evidence"})
    assert len(trace.serving_shadow) == 64
    assert trace.serving_shadow_dropped == 1
    summary = trace.to_log_dict()
    assert "private user question" not in json.dumps(summary)


def test_current_request_does_not_inherit_another_shadow(points):
    evidence, pub = points
    warehouse = ServingWarehouse(
        Facts(evidence), Reader(pub), lambda: NOW, mode="shadow"
    )
    with request_policy(settings(), "openai"):
        warehouse.load(["2024-25"])
        assert warehouse.serving_shadow is not None
    with request_policy(settings(), "openai"):
        assert warehouse.serving_shadow is None


@pytest.mark.parametrize("suffix", ["", "/stream"])
@pytest.mark.parametrize(
    "name,status",
    [
        ("Avery Fich", "ok"),
        ("Avery", "clarification_required"),
        ("Unknown Person", "not_found"),
    ],
)
def test_served_names_preserve_shared_main_policy_and_source_binding(
    prepared, suffix, name, status
):
    from app.agent.semantic_source import snapshot_digest, snapshot_evidence

    _, original = prepared
    snapshot = json.loads(original.detail_json)
    for row in snapshot["rows"]:
        row["player_name"] = {1: "Avery Finch", 2: "Jordan Vale", 3: "Morgan Reed"}[
            row["player_id"]
        ]
    snapshot["sha256"] = snapshot_digest(snapshot)
    evidence = snapshot_evidence(snapshot)
    pub = PilotPublication.build(
        snapshot,
        expected_snapshot_id=snapshot["sha256"],
        publication_id="name-policy",
        relation_version="name-policy/1",
        now=NOW,
        include_detail=True,
    )
    warehouse = ServingWarehouse(
        Facts(evidence), Reader(pub), lambda: NOW, mode="serve"
    )
    question = f"{name} points per game in 2024-25 regular season"
    actual = ask(warehouse, question, suffix)
    assert actual["status"] == status
    if status == "ok":
        canonical = ask(
            warehouse,
            "Avery Finch points per game in 2024-25 regular season",
            suffix,
        )
        assert actual["semantic_evidence"] == canonical["semantic_evidence"]
        assert actual["tables"] == canonical["tables"]
        assert actual["name_resolution_notices"]
    else:
        reference = ask(Facts(evidence), question, suffix)
        assert actual["status"] == reference["status"]
        assert actual["clarification_options"] == reference["clarification_options"]
        assert not actual["tables"]
        assert actual["semantic_plan"]["model_calls"] == 0
    assert actual["source_query"]["publication_id"] == pub.approval.publication_id
