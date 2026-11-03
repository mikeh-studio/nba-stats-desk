import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from app.agent.execution import request_policy, warehouse_budget, warehouse_bytes_billed
from app.agent.observability import AgentTrace
from app.agent.semantic_serving import (
    SemanticWarehouse,
    load_available_season,
)
from app.agent.semantic_source import (
    BigQuerySemanticSource,
    snapshot_digest,
)
from app.agent.semantics import Query, run_query
from app.agent.serving_bigquery import BigQueryServingReader
from app.agent.serving_runtime import PilotPublication
from app.agent.serving_warehouse import RefreshingServingWarehouse, ServingWarehouse
from app.repository import BigQueryWarehouseRepository
from google.api_core.exceptions import NotFound
from scripts.ask_eval.fixtures import ControlledModel
from tests.support.semantic import settings
from tests.test_semantic_source import Client as SourceClient
from tests.test_serving_bigquery import Client as WireClient
from tests.test_serving_bigquery import wire
from tests.test_serving_expansion import make_prepared
from tests.test_serving_routing import ask
from tests.test_serving_runtime import NOW


@pytest.fixture
def multi():
    _, prepared = make_prepared()
    snapshot = json.loads(prepared.detail_json)
    old = [
        dict(
            r,
            season="2023-24",
            game_date=r["game_date"].replace("2025", "2024"),
            team_abbr="OLD",
            player_name="Past Identity" if r["player_id"] == 1 else r["player_name"],
        )
        for r in snapshot["rows"]
    ]
    snapshot["rows"] = old + snapshot["rows"]
    snapshot["coverage"] = [
        dict(
            c, season="2023-24", data_through=c["data_through"].replace("2025", "2024")
        )
        for c in snapshot["coverage"]
    ] + snapshot["coverage"]
    snapshot["sha256"] = snapshot_digest(snapshot)
    pub = PilotPublication.build(
        snapshot,
        expected_snapshot_id=snapshot["sha256"],
        publication_id="multi-season",
        relation_version="multi-season/1",
        now=NOW,
        metrics=["pts", "ast"],
        include_detail=True,
    )
    return snapshot, pub


class Frozen:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def capture(self, seasons):
        value = dict(self.snapshot)
        value["rows"] = [r for r in value["rows"] if r["season"] in seasons]
        value["coverage"] = [c for c in value["coverage"] if c["season"] in seasons]
        value["sha256"] = snapshot_digest(value)
        return value


class FullReader:
    def __init__(self, pub):
        self.pub = pub

    def load(self, *, now, include_detail=False):
        return self.pub if include_detail else replace(self.pub, detail_json=None)


@pytest.mark.parametrize("suffix", ["", "/stream"])
def test_cross_season_date_scope_matches_fact_endpoint(multi, suffix):
    snapshot, pub = multi
    question = "Regular Player points per game from 2024-01-01 to 2025-01-02"
    # The player's earlier appearance has a different observed name; the model
    # returns the governed current alias, and both paths must retain both seasons.
    plan = dict(
        status="query",
        message="",
        queries=[
            dict(
                metric="pts",
                aggregation="average",
                player_name="resolved_player_1",
                season="2024-25",
                season_type="Regular Season",
                window="date_range",
                start_date="2024-01-01",
                as_of="2025-01-02",
            )
        ],
    )
    facts = SemanticWarehouse(Frozen(snapshot))
    served = ServingWarehouse(
        facts, FullReader(pub), lambda: NOW, mode="serve", families=("points", "detail")
    )
    base = ask(
        facts,
        question,
        suffix,
        model_client=ControlledModel({"planner_response": plan}),
    )
    candidate = ask(
        served,
        question,
        suffix,
        model_client=ControlledModel({"planner_response": plan}),
    )
    assert base["status"] == candidate["status"] == "ok"
    assert candidate["semantic_evidence"] == base["semantic_evidence"]
    assert candidate["semantic_evidence"]["scope"]["seasons"] == ["2024-25", "2023-24"]
    assert candidate["tables"] == base["tables"]
    assert "validated_points_scope" not in candidate["semantic_plan"]
    assert candidate["source_query"]["publication_id"] == pub.approval.publication_id
    assert (
        candidate["source_query"]["relation_version"] == pub.approval.relation_version
    )


@pytest.mark.parametrize("suffix", ["", "/stream"])
def test_rolling_asof_scope_matches_fact_endpoint(multi, suffix):
    snapshot, pub = multi
    question = "Regular Player points per game in the past 12 months as of 2025-01-02"
    plan = dict(
        status="query",
        message="",
        queries=[
            dict(
                metric="pts",
                aggregation="average",
                player_name="resolved_player_1",
                season="2024-25",
                season_type="Regular Season",
                window="last_n_months",
                n=12,
                as_of="2025-01-02",
            )
        ],
    )
    facts = SemanticWarehouse(Frozen(snapshot))
    served = ServingWarehouse(
        facts, FullReader(pub), lambda: NOW, mode="serve", families=("points", "detail")
    )
    base = ask(
        facts,
        question,
        suffix,
        model_client=ControlledModel({"planner_response": plan}),
    )
    candidate = ask(
        served,
        question,
        suffix,
        model_client=ControlledModel({"planner_response": plan}),
    )
    assert base["status"] == candidate["status"] == "ok"
    # Serving legitimately cites the certified multi-season snapshot, while
    # facts captured only the requested season. Values and scope must match.
    left, right = candidate["semantic_evidence"], base["semantic_evidence"]
    assert left["provenance"]["snapshot_id"] == pub.approval.source_snapshot_id
    left["provenance"].pop("snapshot_id")
    right["provenance"].pop("snapshot_id")
    assert left == right
    assert candidate["semantic_evidence"]["scope"]["as_of"] == "2025-01-02"


def test_multi_season_publication_identity_profile_is_season_scoped(multi):
    snapshot, pub = multi
    facts = SemanticWarehouse(Frozen(snapshot))
    served = ServingWarehouse(facts, FullReader(pub), lambda: NOW, mode="serve")
    question = "Regular Player points per game in 2024-25 regular season"
    base, candidate = ask(facts, question, ""), ask(served, question, "")
    assert base["status"] == candidate["status"] == "ok"
    assert candidate["player_profile"] == base["player_profile"]
    assert candidate["player_profile"]["player"]["player_name"] == "Regular Player"
    assert candidate["player_profile"]["player"]["team_abbr"] != "OLD"


@pytest.mark.parametrize("suffix", ["", "/stream"])
def test_missing_optional_baseline_with_native_bq_errors_still_answers(suffix):
    class MissingBaseline(SourceClient):
        def __init__(self):
            super().__init__()
            self.calls = []

        def query(self, sql, **kwargs):
            seasons = [
                p.value
                for p in kwargs["job_config"].query_parameters
                if p.name.startswith("season_")
            ]
            self.calls.append(seasons)
            if "2023-24" in seasons:
                raise NotFound("missing optional archive")
            return super().query(sql, **kwargs)

    client = MissingBaseline()
    facts = SemanticWarehouse(BigQuerySemanticSource(client, project="test-project"))
    payload = ask(
        facts, "How did Regular Player perform in 2024-25 regular season?", suffix
    )
    assert payload["status"] == "ok"
    assert any("2023-24" in c for c in client.calls)
    assert client.calls[-1] == ["2024-25"]
    assert payload["semantic_evidence"]["scope"]["start"] == "2024-07-01"


def test_season_and_repository_fallback_preserve_budget():
    class OldSeason(SourceClient):
        def query(self, sql, **kwargs):
            seasons = [
                p.value
                for p in kwargs["job_config"].query_parameters
                if p.name.startswith("season_")
            ]
            if seasons == ["2024-25"]:
                raise NotFound("missing archive")
            self.rows = [
                dict(
                    r,
                    season="2023-24",
                    game_date=r["game_date"].replace("2025", "2024"),
                )
                for r in self.rows
            ]
            return super().query(sql, **kwargs)

    trace = AgentTrace("id", "private", "fixture")
    warehouse = SemanticWarehouse(
        BigQuerySemanticSource(OldSeason(), project="test-project")
    )
    with request_policy(settings(), "openai", trace):
        _, evidence, season = load_available_season(
            warehouse.load, ["2024-25", "2023-24"]
        )
        assert season == "2023-24" and evidence.rows
        assert warehouse_budget(100_000_000) == 99_999_900
    assert [
        r["bytes_billed"] for r in trace.data_reads if r["kind"] == "warehouse"
    ] == [0, 100]

    class RepositoryClient:
        def __init__(self):
            self.calls = 0

        def query(self, sql, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise NotFound("missing prepared identity relation")
            return SimpleNamespace(
                total_bytes_billed=100,
                total_bytes_processed=100,
                result=lambda **kw: [dict(player_id=1, player_name="Fixture")],
            )

    client = RepositoryClient()
    repo = BigQueryWarehouseRepository(settings(), client=client)
    with request_policy(settings(), "openai"):
        assert repo._fetch_player_identity(1)["player_id"] == "1"
    assert client.calls == 2


def test_billing_inference_never_zeroes_ambiguous_execution():
    assert warehouse_bytes_billed(None, TimeoutError()) is None
    assert (
        warehouse_bytes_billed(
            SimpleNamespace(error_result=None), NotFound("lost result")
        )
        is None
    )
    assert (
        warehouse_bytes_billed(SimpleNamespace(error_result={"reason": "stopped"}))
        is None
    )
    assert (
        warehouse_bytes_billed(SimpleNamespace(error_result={"reason": "notFound"}))
        == 0
    )


def test_native_repository_workers_share_trace_budget_and_deadline(monkeypatch):
    import time

    from app.agent.execution import CURRENT

    calls = []

    class Client:
        def query(self, sql, **kwargs):
            assert CURRENT.get() is not None
            assert 0 < kwargs["timeout"] <= settings().agent_request_deadline_seconds
            calls.append(kwargs["job_config"].maximum_bytes_billed)

            def result(**kw):
                time.sleep(0.01)
                return [dict(value=1)]

            return SimpleNamespace(
                total_bytes_billed=10_000_000, total_bytes_processed=100, result=result
            )

    repo = BigQueryWarehouseRepository(settings(), client=Client())
    monkeypatch.setattr(
        repo,
        "_fetch_player_identity",
        lambda _: dict(player_id=1, player_name="Fixture"),
    )
    monkeypatch.setattr(
        repo, "_fetch_player_detail_row", lambda _: repo._query("SELECT 1")[0]
    )
    monkeypatch.setattr(
        repo, "_fetch_player_game_log_payload", lambda *a, **kw: repo._query("SELECT 1")
    )
    monkeypatch.setattr(repo, "_fetch_player_trends", lambda _: repo._query("SELECT 1"))
    monkeypatch.setattr(
        repo, "_fetch_similarity_anchor", lambda _: repo._query("SELECT 1")
    )
    monkeypatch.setattr(
        repo, "_get_similar_players", lambda *a, **kw: ("fresh", None, [])
    )
    monkeypatch.setattr(repo, "_fetch_chart_baseline_row", lambda: {})
    monkeypatch.setattr(repo, "_build_player_detail_payload", lambda **kw: kw)
    trace = AgentTrace("id", "private", "fixture")
    with request_policy(settings(), "openai", trace):
        assert repo.get_player_detail(1)
        assert warehouse_budget(100_000_000) == 60_000_000
    assert len(trace.data_reads) == 4
    assert sorted(calls) == [70_000_000, 80_000_000, 90_000_000, 100_000_000]


def test_table_api_reads_are_bounded_recorded_and_not_recharged_on_cache_hit():
    from app.repository._constants import RECENT_PERFORMANCE_TABLE_FIELDS

    class Client:
        def get_table(self, table, **kw):
            assert 0 < kw["timeout"] <= 30
            return SimpleNamespace(schema=RECENT_PERFORMANCE_TABLE_FIELDS)

        def list_rows(self, table, **kw):
            assert kw["max_results"] == 5000 and 0 < kw["timeout"] <= 30
            return [dict(season="2024-25")]

    repo = BigQueryWarehouseRepository(settings(), client=Client())
    trace = AgentTrace("id", "private", "fixture")
    with request_policy(settings(), "openai", trace):
        assert repo._fetch_recent_performance_table_rows_api()
        assert repo._fetch_recent_performance_table_rows_api()
    assert len(trace.data_reads) == 2
    assert all(r["bytes_billed"] == 0 for r in trace.data_reads)


def test_expired_unrelated_reference_does_not_read_candidate(multi, tmp_path):
    snapshot, pub = multi
    # Declare only 2024-25 in a trusted routing reference, request historical 2023-24.
    single = json.loads(make_prepared()[1].detail_json)
    one = PilotPublication.build(
        single,
        expected_snapshot_id=single["sha256"],
        publication_id="one",
        relation_version="one",
        now=NOW,
    )
    reference, _ = wire(one)
    from dataclasses import asdict

    path = tmp_path / "reference.json"
    path.write_text(json.dumps(asdict(reference)))
    facts = SemanticWarehouse(Frozen(snapshot))
    wrapper = RefreshingServingWarehouse(
        facts,
        object(),
        path,
        lambda: NOW.replace(minute=10),
        project="test-project",
        mode="serve",
    )
    payload = ask(
        wrapper, "Past Identity points per game in 2023-24 regular season", ""
    )
    assert payload["status"] == "ok"
    assert payload["semantic_evidence"]["scope"]["season"] == "2023-24"


def test_cached_core_and_game_log_do_not_wait_for_cold_detail():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    evidence, pub = make_prepared()
    reference, rows = wire(pub)
    entered, release = Event(), Event()

    class SlowDetail(WireClient):
        def query(self, sql, **kwargs):
            if "WHERE kind = 'detail'" in sql and "player_id = @player_id" not in sql:
                entered.set()
                assert release.wait(5)
            return super().query(sql, **kwargs)

    client = SlowDetail(rows)
    reader = BigQueryServingReader(client, reference)
    reader.load(now=NOW)
    with ThreadPoolExecutor(max_workers=3) as pool:
        full = pool.submit(reader.load, now=NOW, include_detail=True)
        assert entered.wait(5)
        try:
            assert (
                pool.submit(reader.load, now=NOW).result(timeout=1).detail_json is None
            )
            query = Query(
                metric="pts",
                season="2024-25",
                aggregation="total",
                operation="game_log",
                player_id=1,
            )
            assert pool.submit(reader.run_game_log, query, now=NOW).result(
                timeout=1
            ) == run_query(evidence, query)
        finally:
            release.set()
        assert full.result(timeout=5).detail_json is not None


@pytest.mark.parametrize(
    "metric,aggregation", [("min", "total"), ("pts_per36", "ratio")]
)
def test_fractional_combined_phase_ties_match_exact_fact_ranks(metric, aggregation):
    from app.agent.semantics import load_contract
    from tests.test_semantic_source import source_rows

    template = source_rows()[0]
    rows = []
    for player, values in [
        (
            1,
            [
                ("Regular Season", 0.1, 2),
                ("Regular Season", 0.2, 2),
                ("Playoffs", 0.3, 2),
            ],
        ),
        (2, [("Regular Season", 0.6, 6), ("Playoffs", 0.0, 0)]),
    ]:
        for index, (phase, minutes, points) in enumerate(values):
            row = dict(
                template,
                player_id=player,
                player_name=f"Fractional {player}",
                game_id=f"{player}{index:02d}",
                season_type=phase,
                game_date="2025-01-01" if phase == "Regular Season" else "2025-04-01",
                min=minutes,
                pts=points,
            )
            rows.append(row)
    snap = BigQuerySemanticSource(SourceClient(rows), project="test-project").capture(
        ["2024-25"]
    )
    snap["source_timestamp"] = NOW.isoformat()
    snap["sha256"] = snapshot_digest(snap)
    from app.agent.semantic_source import snapshot_evidence

    evidence = snapshot_evidence(snap)
    pub = PilotPublication.build(
        snap,
        expected_snapshot_id=snap["sha256"],
        publication_id="fractional",
        relation_version="fractional/1",
        now=NOW,
        metrics=[metric],
    )
    from app.agent.semantics import Query

    query = Query(
        metric=metric,
        aggregation=aggregation,
        season="2024-25",
        season_type="Both",
        operation="rank",
        min_games=1,
        min_attempts=0 if load_contract().metric(metric).denominator else None,
    )
    expected = run_query(evidence, query)
    assert expected["rows"][0]["rank"] == expected["rows"][1]["rank"] == 1
    assert pub.run_expanded(query, now=NOW) == expected
    # The wire reader must retain the residuals, not only phase subtotals.
    ref, records = wire(pub)
    restored = BigQueryServingReader(WireClient(records), ref).load(now=NOW)
    assert restored.run_expanded(query, now=NOW) == expected


@pytest.mark.parametrize("suffix", ["", "/stream"])
@pytest.mark.parametrize("reference_state", ["missing", "corrupt"])
def test_current_season_identity_avoids_unrelated_reference(
    multi, tmp_path, suffix, reference_state
):
    from datetime import datetime

    from app.agent.semantic_source import capture_timestamp
    from app.seasons import DEFAULT_SEASON

    snapshot, _ = multi
    season = DEFAULT_SEASON
    # A current-season rookie exists only in fresh facts, never in the old catalog.
    from app.seasons import season_bounds

    day = str(season_bounds(season)[0])
    rows = [
        dict(
            r,
            season=season,
            game_date=day,
            player_name="Current Rookie" if r["player_id"] == 1 else r["player_name"],
        )
        for r in snapshot["rows"]
        if r["season"] == "2024-25"
    ]
    snap = dict(
        snapshot,
        rows=rows,
        coverage=[
            dict(c, season=season, data_through=day)
            for c in snapshot["coverage"]
            if c["season"] == "2024-25"
        ],
    )
    snap["sha256"] = snapshot_digest(snap)
    facts = SemanticWarehouse(Frozen(snap))
    path = tmp_path / "reference.json"
    if reference_state == "corrupt":
        path.write_text("{}")
    warehouse = RefreshingServingWarehouse(
        facts,
        object(),
        path,
        lambda: datetime.fromisoformat(capture_timestamp()),
        project="test-project",
        mode="serve",
    )
    # Controlled model binds identity; deterministic historical parser declines this season.
    plan = dict(
        status="query",
        message="",
        queries=[
            dict(
                metric="pts",
                aggregation="average",
                season=season,
                season_type="Regular Season",
                player_name="resolved_player_1",
            )
        ],
    )
    payload = ask(
        warehouse,
        "Current Rookie points per game this season",
        suffix,
        config=replace(settings(), season=season),
        model_client=ControlledModel({"planner_response": plan}),
    )
    assert payload["status"] == "ok", payload
    assert payload["semantic_evidence"]["rows"][0]["player_id"] == 1
    assert payload["semantic_evidence"]["scope"]["season"] == season


@pytest.mark.parametrize("error_type", [KeyError, TypeError])
def test_internal_serving_runner_error_is_not_a_bad_user_plan(error_type):
    from app.agent.semantic_planner import execute_plan

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

    def fail(_):
        raise error_type("private integrity detail")

    result = execute_plan(
        plan,
        None,
        [dict(player_id=1, player_name="Regular Player", aliases=[])],
        query_runner=fail,
    )
    assert result["status"] == "invalid_evidence"


def test_points_gate_keeps_narrow_pilot_on_expanded_publication(multi):
    snapshot, pub = multi
    facts = SemanticWarehouse(Frozen(snapshot))
    warehouse = ServingWarehouse(
        facts, FullReader(pub), lambda: NOW, mode="serve", families=("points",)
    )
    plan = dict(
        status="query",
        queries=[
            dict(
                metric="pts",
                aggregation="average",
                season="2024-25",
                player_name="Regular Player",
                min_games=1,
            )
        ],
    )
    assert warehouse.prepare(lambda *_: plan, seasons=["2024-25"]).result is None


def test_wire_validation_certificate_is_bound_to_approval(multi):
    from app.agent.semantics import SemanticError
    from app.agent.serving_publication import digest

    _, pub = multi
    ref, records = wire(pub)
    header = json.loads(records[0]["record_json"])
    header["validation_results"]["summary_digest"] = "0" * 64
    records[0]["record_json"] = json.dumps(header)
    ref = replace(ref, manifest_digest=digest(header))
    with pytest.raises(SemanticError, match="Invalid serving wire"):
        BigQueryServingReader(WireClient(records), ref).load(now=NOW)


@pytest.mark.parametrize("suffix", ["", "/stream"])
def test_ambiguity_clarification_and_pending_payload_match_facts(multi, suffix):
    snapshot, _ = multi
    snapshot = dict(
        snapshot,
        rows=[
            dict(r, player_name="Regular Player") if r["player_id"] == 2 else r
            for r in snapshot["rows"]
        ],
    )
    snapshot["sha256"] = snapshot_digest(snapshot)
    pub = PilotPublication.build(
        snapshot,
        expected_snapshot_id=snapshot["sha256"],
        publication_id="ambiguous",
        relation_version="ambiguous/1",
        now=NOW,
        include_detail=True,
    )
    facts = SemanticWarehouse(Frozen(snapshot))
    served = ServingWarehouse(facts, FullReader(pub), lambda: NOW, mode="serve")
    question = "Regular Player points per game in 2024-25 regular season"
    base, candidate = ask(facts, question, suffix), ask(served, question, suffix)
    assert base["status"] == candidate["status"] == "clarification_required"
    assert candidate["clarification_options"] == base["clarification_options"]
    assert candidate["semantic_plan"] == base["semantic_plan"]
    assert candidate["clarification_question"] == base["clarification_question"]
    assert all(
        option["team_abbr"] and option["label"] == option["player_name"]
        for option in candidate["clarification_options"]
    )


def test_governed_profile_fanout_stays_on_caller_and_finishes_its_ledger(monkeypatch):
    from threading import get_ident

    from app.repository import _bigquery

    caller, seen = get_ident(), []
    monkeypatch.setattr(
        _bigquery._PLAYER_QUERY_POOL,
        "submit",
        lambda *a, **kw: pytest.fail("Ask must not occupy the page pool"),
    )

    class Client:
        def query(self, *args, **kwargs):
            seen.append(get_ident())
            return SimpleNamespace(
                total_bytes_billed=123, total_bytes_processed=123, result=lambda **_: []
            )

    repo = BigQueryWarehouseRepository(settings(), client=Client())
    repo._fetch_player_identity = lambda _: dict(
        player_id=1, player_name="Regular Player"
    )
    repo._fetch_player_detail_row = lambda _: (_ for _ in ()).throw(
        ValueError("synthetic profile failure")
    )
    repo._fetch_player_game_log_payload = lambda *a, **kw: []
    repo._fetch_player_trends = lambda _: repo._query("SELECT synthetic_late_worker")
    repo._fetch_similarity_anchor = lambda _: None
    repo._get_similar_players = lambda *a, **kw: ("unavailable", None, [])
    trace = AgentTrace("worker-drain", "private question", "fixture")
    with request_policy(settings(), "openai", trace):
        with pytest.raises(ValueError, match="synthetic profile failure"):
            repo.get_player_detail(1)
    assert seen == [caller]
    assert len(trace.data_reads) == 1
    assert trace.data_reads[0]["bytes_billed"] == 123


@pytest.mark.parametrize(
    "location",
    ["publication", "core", "detail", "player", "cohort", "version", "budget"],
)
def test_deadline_after_lock_acquisition_releases_shared_lock(
    multi, tmp_path, location
):
    from dataclasses import asdict
    from threading import Lock

    from app.agent import execution
    from app.agent.semantics import Query
    from app.agent.serving_warehouse import PublicationCache

    snapshot, pub = multi
    ref, records = wire(pub)
    reader = BigQueryServingReader(WireClient(records), ref)

    class ExpiresOnce:
        def __init__(self):
            self.lock, self.first = Lock(), True

        def acquire(self, **kwargs):
            result = self.lock.acquire(**kwargs)
            if result and self.first:
                self.first = False
                execution.CURRENT.get().deadline = execution.monotonic() - 1
            return result

        def release(self):
            self.lock.release()

        def locked(self):
            return self.lock.locked()

    lock = ExpiresOnce()
    query = Query(metric="pts", aggregation="average", season="2024-25", player_id=1)
    if location == "publication":
        cache = PublicationCache(reader, lambda: NOW)
        cache._locks[False] = lock
        invoke = cache.get
    elif location == "version":
        path = tmp_path / "reference.json"
        path.write_text(json.dumps(asdict(ref)))
        warehouse = RefreshingServingWarehouse(
            SemanticWarehouse(Frozen(snapshot)),
            reader.client,
            path,
            lambda: NOW,
            project="test-project",
            mode="serve",
        )
        warehouse._lock = lock
        invoke = warehouse._select
    elif location == "cohort":
        object.__setattr__(pub, "_cohort_lock", lock)

        def invoke():
            return pub.run_expanded(query, now=NOW, reuse_cohort=True)
    elif location == "budget":
        invoke = execution.serialized_warehouse_read(lambda: True)
    else:
        if location != "core":
            reader.load(now=NOW)
        setattr(
            reader,
            {"core": "_core_lock", "detail": "_detail_lock", "player": "_player_lock"}[
                location
            ],
            lock,
        )
        invoke = (
            (lambda: reader.run_game_log(replace(query, operation="game_log"), now=NOW))
            if location == "player"
            else lambda: reader.load(now=NOW, include_detail=location == "detail")
        )
    with request_policy(settings(), "openai") as policy:
        if location == "budget":
            policy.warehouse_lock = lock
        with pytest.raises(execution.ExecutionFailure):
            invoke()
    assert not lock.locked()
    with request_policy(settings(), "openai") as policy:
        if location == "budget":
            policy.warehouse_lock = lock
        assert invoke() is not None
    assert not lock.locked()


def test_live_shadow_capture_mapping_requires_equal_per_game_inputs(multi):
    from app.agent.semantic_source import snapshot_evidence
    from app.agent.semantics import Query
    from app.agent.serving_runtime import ServingShadow

    snapshot, pub = multi
    evidence = snapshot_evidence(snapshot)
    fresh = replace(evidence, snapshot_id="new-capture", source="new-capture-relations")
    query = Query(metric="pts", aggregation="average", season="2024-25", player_id=1)
    shadow = ServingShadow(
        pub, lambda: NOW, expanded=True, reference_evidence=fresh, budget_seconds=1
    )
    shadow.compare(query, run_query(fresh, query))
    assert shadow.outcomes == [
        {"status": "match", "code": "capture_provenance_differs"}
    ]
    rows = [dict(row) for row in fresh.rows]
    candidates = [
        row
        for row in rows
        if row["season"] == "2024-25"
        and row["season_type"] == "Regular Season"
        and row["player_id"] == 1
        and row.get("pts") is not None
    ]
    candidates[0]["pts"] += 1
    candidates[1]["pts"] -= 1
    changed = replace(fresh, rows=rows)
    shadow = ServingShadow(
        pub, lambda: NOW, expanded=True, reference_evidence=changed, budget_seconds=1
    )
    shadow.compare(query, run_query(changed, query))
    assert shadow.outcomes == [{"status": "mismatch", "code": "source_content_differs"}]


def test_shadow_is_lazy_tolerates_cold_read_and_honors_families(multi, monkeypatch):
    from app.agent import execution
    from app.agent.semantics import Query

    snapshot, pub = multi
    ref, records = wire(pub)
    clock = [1000.0]
    monkeypatch.setattr(execution, "monotonic", lambda: clock[0])

    class Slow(WireClient):
        def query(self, *args, **kwargs):
            clock[0] += 1.2
            return super().query(*args, **kwargs)

    client = Slow(records)
    facts = SemanticWarehouse(Frozen(snapshot))
    warehouse = ServingWarehouse(
        facts, BigQueryServingReader(client, ref), lambda: NOW, mode="shadow"
    )
    trace = AgentTrace("cold-shadow", "private", "fixture")
    with request_policy(settings(), "openai", trace) as policy:
        _, e = warehouse.load(["2024-25"])
        assert not client.calls
        query = Query(
            metric="pts", aggregation="average", season="2024-25", player_id=1
        )
        warehouse.serving_shadow.compare(query, run_query(e, query))
        assert trace.serving_shadow == [
            {"status": "match", "code": "capture_provenance_differs"}
        ]
        assert policy.warehouse_ledger["bytes_used"] == 100
    assert len(client.calls) == 1
    warehouse.families = frozenset()
    with request_policy(settings(), "openai", trace):
        _, e = warehouse.load(["2024-25"])
        warehouse.serving_shadow.compare(query, run_query(e, query))
    assert trace.serving_shadow[-1] == {"status": "ineligible"}
    assert len(client.calls) == 1


def test_shadow_refresh_errors_are_contained_and_retry_is_bounded(multi):
    from app.agent.semantics import Query
    from google.auth.exceptions import RefreshError

    snapshot, _ = multi

    class Broken:
        calls = 0

        def load(self, **kwargs):
            self.calls += 1
            raise RefreshError("private credential failure")

    broken = Broken()
    warehouse = ServingWarehouse(
        SemanticWarehouse(Frozen(snapshot)), broken, lambda: NOW, mode="shadow"
    )
    query = Query(metric="pts", aggregation="average", season="2024-25", player_id=1)
    trace = AgentTrace("refresh-shadow", "private", "fixture")
    for _ in range(2):
        with request_policy(settings(), "openai", trace):
            _, e = warehouse.load(["2024-25"])
            reference = run_query(e, query)
            warehouse.serving_shadow.compare(query, reference)
            assert reference["status"] == "ok"
    assert broken.calls == 1
    assert trace.serving_shadow == [
        {"status": "error", "code": "shadow_failure"},
        {"status": "error", "code": "shadow_retry_deferred"},
    ]


def test_failed_serving_job_is_cancelled_and_unknown_charge_is_retained(multi):
    from app.agent.semantics import Query

    snapshot, pub = multi
    ref, _ = wire(pub)
    cancelled = []

    class Timeout:
        def query(self, *a, **kw):
            def result(**_):
                raise TimeoutError("private timeout")

            return SimpleNamespace(
                total_bytes_billed=None,
                total_bytes_processed=None,
                result=result,
                cancel=lambda **kw: cancelled.append(True),
            )

    warehouse = ServingWarehouse(
        SemanticWarehouse(Frozen(snapshot)),
        BigQueryServingReader(Timeout(), ref),
        lambda: NOW,
        mode="shadow",
    )
    trace = AgentTrace("timeout-shadow", "private", "fixture")
    with request_policy(settings(), "openai", trace) as policy:
        _, e = warehouse.load(["2024-25"])
        query = Query(
            metric="pts", aggregation="average", season="2024-25", player_id=1
        )
        warehouse.serving_shadow.compare(query, run_query(e, query))
        assert 0 < policy.warehouse_ledger["bytes_used"] <= 50_000_000
    assert cancelled == [True]
    assert trace.data_reads[-1]["bytes_billed"] is None
    assert trace.serving_shadow == [{"status": "error", "code": "source_unavailable"}]


@pytest.mark.parametrize("suffix", ["", "/stream"])
@pytest.mark.parametrize(
    "question",
    [
        "How did Regular Player perform in 2024-25 regular season?",
        "Regular Player assists per game in 2024-25 regular season",
    ],
)
def test_disabled_families_do_not_depend_on_missing_reference(
    multi, tmp_path, suffix, question
):
    snapshot, _ = multi
    facts = SemanticWarehouse(Frozen(snapshot))
    warehouse = RefreshingServingWarehouse(
        facts,
        object(),
        tmp_path / "missing.json",
        lambda: NOW,
        project="test-project",
        mode="serve",
        families=("points",),
    )
    base = ask(facts, question, suffix)
    candidate = ask(warehouse, question, suffix)
    assert candidate["status"] == base["status"] == "ok"
    assert candidate["tables"] == base["tables"]


@pytest.mark.parametrize(
    "families", [("points", "rich"), ("points", "detail"), ("points",)]
)
def test_points_pilot_does_not_expand_with_unrelated_flags(multi, families):
    snapshot, pub = multi
    warehouse = ServingWarehouse(
        SemanticWarehouse(Frozen(snapshot)),
        FullReader(pub),
        lambda: NOW,
        mode="serve",
        families=families,
    )
    plan = dict(
        status="query",
        queries=[
            dict(
                metric="pts",
                aggregation="average",
                season="2024-25",
                player_name="Regular Player",
                min_games=1,
            )
        ],
    )
    assert warehouse.prepare(lambda *_: plan, seasons=["2024-25"]).result is None


def test_points_expiry_during_planning_is_visible_failure(multi):
    from datetime import timedelta

    snapshot, pub = multi
    clock = [NOW]
    warehouse = ServingWarehouse(
        SemanticWarehouse(Frozen(snapshot)),
        FullReader(pub),
        lambda: clock[0],
        mode="serve",
    )

    def planner(*_):
        clock[0] += timedelta(seconds=301)
        return dict(
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

    result = warehouse.prepare(planner, seasons=["2024-25"])
    assert result.result["status"] == "invalid_evidence"


def test_subminimum_warehouse_budget_is_explicitly_exhausted():
    from app.agent.execution import ExecutionFailure

    with request_policy(settings(), "openai") as policy:
        policy.warehouse_ledger["bytes_used"] = (
            policy.warehouse_byte_limit - 9 * 1024 * 1024
        )
        with pytest.raises(ExecutionFailure, match="query_budget_exceeded"):
            warehouse_budget(100_000_000)


def test_prepared_scope_union_updates_profile_appearances(multi):
    snapshot, pub = multi
    warehouse = ServingWarehouse(
        SemanticWarehouse(Frozen(snapshot)),
        FullReader(pub),
        lambda: NOW,
        mode="serve",
        families=("detail",),
    )
    plan = dict(
        status="query",
        queries=[
            dict(
                metric="pts",
                aggregation="average",
                season="2024-25",
                player_name="Regular Player",
                window="date_range",
                start_date="2024-01-01",
                as_of="2025-01-02",
            )
        ],
    )

    def normalize(plan, appearances):
        plan["queries"][0]["seasons"] = ["2024-25", "2023-24"]
        return plan

    result = warehouse.prepare(
        lambda *_: plan, seasons=["2024-25"], normalizer=normalize
    )
    assert result.result["status"] == "ok"
    assert result.required_seasons == ("2024-25", "2023-24")
    assert {row["season"] for row in result.appearances} == {"2024-25", "2023-24"}
    assert (
        result.source_query["publication_expires_at"]
        == pub.approval.expires_at.isoformat()
    )


def test_game_log_bad_json_is_evidence_error_and_unlocks(multi):
    from app.agent.semantics import Query, SemanticError

    _, pub = multi
    ref, records = wire(pub)
    row = next(r for r in records if r["kind"] == "detail" and r["player_id"] == 1)
    row["record_json"] = "{broken"
    reader = BigQueryServingReader(WireClient(records), ref)
    with pytest.raises(SemanticError) as error:
        reader.run_game_log(
            Query(
                metric="pts",
                aggregation="total",
                season="2024-25",
                player_id=1,
                operation="game_log",
            ),
            now=NOW,
        )
    assert error.value.code == "invalid_evidence"
    assert not reader._player_lock.locked()


def test_promotion_requires_useful_lifetime(multi):
    from datetime import timedelta

    from app.agent.semantics import SemanticError
    from scripts.publish_ask_serving import promotion_lifetime

    _, pub = multi
    assert (
        promotion_lifetime(pub, pub.approval.expires_at - timedelta(seconds=60)) == 60
    )
    with pytest.raises(SemanticError, match="Insufficient"):
        promotion_lifetime(pub, pub.approval.expires_at - timedelta(seconds=59))
    with pytest.raises(ValueError):
        promotion_lifetime(pub, NOW, float("nan"))


def test_phase_only_hint_does_not_require_detail_family():
    from app.agent.serving_warehouse import question_may_use_serving

    assert question_may_use_serving(
        "Regular Player points per game in 2024-25 including regular season and playoffs",
        {"points"},
    )


@pytest.mark.parametrize("expired", [False, True])
def test_development_cli_receipt_is_bounded_and_late_candidate_is_not_promoted(
    multi, tmp_path, monkeypatch, capsys, expired
):
    import sys
    from datetime import timedelta

    from app.agent.semantic_source import snapshot_evidence
    from app.agent.serving_bigquery import ServingReference, encode_publication
    from app.agent.serving_publication import digest
    from google.cloud import bigquery
    from scripts import publish_ask_serving as cli

    snapshot, _ = multi
    source = tmp_path / "source.json"
    source.write_text("{}")
    pointer = tmp_path / "reference.json"
    history = tmp_path / "reference.json.history"
    history.mkdir()
    for i in range(25):
        (history / f"{i:032x}.json").write_text("{}")
    times = [NOW, NOW + timedelta(seconds=241)] if expired else [NOW]

    class Clock:
        @staticmethod
        def now(tz):
            return times.pop(0) if len(times) > 1 else times[0]

    class Publisher:
        def __init__(self, *args, **kwargs):
            pass

        def publish(self, pub, now):
            table = "ask_serving_" + "a" * 32
            header, _ = encode_publication(pub, now=now, relation_id=table)
            return ServingReference(
                "test-project",
                "isolated_dev",
                table,
                digest(header),
                pub.approval.source_snapshot_id,
                tuple(pub.approval.scopes),
            )

    monkeypatch.setattr(cli, "datetime", Clock)
    monkeypatch.setattr(
        cli, "load_snapshot", lambda _: (snapshot, snapshot_evidence(snapshot))
    )
    monkeypatch.setattr(cli, "DevelopmentBigQueryPublisher", Publisher)
    monkeypatch.setattr(bigquery, "Client", lambda **kw: object())
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "publish",
            "--snapshot",
            str(source),
            "--expected-source",
            snapshot["sha256"],
            "--project",
            "test-project",
            "--dataset",
            "isolated_dev",
            "--reference-output",
            str(pointer),
        ],
    )
    if expired:
        from app.agent.semantics import SemanticError

        with pytest.raises(SemanticError, match="Insufficient"):
            cli.main()
        assert not pointer.exists()
        assert len(list(history.glob("*.json"))) == 25
    else:
        assert cli.main() == 0
        output = json.loads(capsys.readouterr().out)
        receipt = json.loads(
            __import__("pathlib").Path(output["receipt_file"]).read_text()
        )
        assert receipt["candidate_reference"] == json.loads(pointer.read_text())
        assert receipt["previous_reference"] is None
        assert output["remaining_seconds"] == 300
        assert len(list(history.glob("*.json"))) == 20


# Public orchestration tests use authored plans through the provider contract.
pytestmark = pytest.mark.usefixtures("scripted_intelligence")
