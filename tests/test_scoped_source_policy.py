"""Candidate SQL/parameters captured at the real source dispatch boundary."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from app.agent.espn_dev_source import ESPNDevSource, RetainedReviewSource
from app.agent.semantic_serving import SemanticWarehouse, warehouse_for_repository
from app.agent.semantic_source import (
    BigQuerySemanticSource,
    ScopedSemanticSource,
    snapshot_evidence,
)
from app.agent.semantics import SemanticError
from app.agent.source_policy import (
    SourcePolicy,
    configure_source_policy,
    source_policy_scope,
)
from app.repository import BigQueryWarehouseRepository
from google.api_core.exceptions import NotFound
from google.cloud import bigquery

from test_semantic_source import Client, source_rows

SEASON = "2024-25"
RELATION = "test-project.nba_gold.fct_player_game_stats"


def policy(**changes):
    return SourcePolicy(
        relations=((SEASON, RELATION),),
        phases=("Regular Season",),
        snapshot_at="2025-06-30T12:00:00+00:00",
        allowed_player_ids=frozenset({1, 2}),
        source_mode="retained_local_review",
        **changes,
    )


def regular_rows():
    return [r for r in source_rows() if r["season_type"] == "Regular Season"]


class CapturingClient(Client):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.calls = []

    def query(self, sql, **kwargs):
        self.calls.append((sql, kwargs))
        return super().query(sql, **kwargs)


def source(client, restriction):
    return RetainedReviewSource(
        client,
        project="test-project",
        source_policy=restriction,
        max_rows=restriction.max_rows,
        maximum_bytes_billed=restriction.maximum_bytes_billed,
    )


@pytest.fixture(autouse=True)
def reset_policy():
    configure_source_policy(None)
    yield
    configure_source_policy(None)


def settings(season=SEASON):
    return SimpleNamespace(
        season=season,
        project_id="test-project",
        gold_dataset="nba_gold",
        metadata_dataset="nba_meta",
        agent_position_context_path=None,
        agent_cache_ttl_seconds=300,
        ask_local_espn_dev=True,
        ask_serving_mode="enforce",
    )


def test_real_scoped_retained_capture_is_one_read_with_typed_phase_and_provenance():
    client = CapturingClient(rows=regular_rows())
    value = source(client, policy()).capture_scoped([SEASON], ["Regular Season"])
    evidence = snapshot_evidence(value)
    assert len(client.calls) == 1
    sql, args = client.calls[0]
    assert f"FROM `{RELATION}` FOR SYSTEM_TIME AS OF @snapshot_at" in sql
    assert "WHERE season = @season_0 AND season_type IN UNNEST(@phases)" in sql
    params = {p.name: p.to_api_repr() for p in args["job_config"].query_parameters}
    assert params["phases"]["parameterType"] == {
        "type": "ARRAY",
        "arrayType": {"type": "STRING"},
    }
    assert params["phases"]["parameterValue"]["arrayValues"] == [
        {"value": "Regular Season"}
    ]
    assert params["season_0"]["parameterValue"]["value"] == SEASON
    assert (
        params["snapshot_at"]["parameterValue"]["value"].replace(" ", "T")
        == policy().snapshot_at
    )
    assert args["job_config"].maximum_bytes_billed == 100_000_000
    assert args["timeout"] == 30
    assert evidence.covered_scopes == frozenset({(SEASON, "Regular Season")})
    assert evidence.complete and evidence.cohort_complete
    assert value["source_mode"] == "retained_local_review"
    assert value["review_metadata"]["archive_status"] == "unavailable"
    assert value["review_metadata"]["retained_relations"] == [RELATION]
    assert value["coverage_notes"] == value["scope_assumptions"]


def test_each_union_branch_filters_phase_before_read():
    restriction = replace(
        policy(), relations=((SEASON, RELATION), ("2023-24", RELATION))
    )
    rows = regular_rows() + [
        {**r, "season": "2023-24", "game_id": "prior-" + r["game_id"]}
        for r in regular_rows()
    ]
    client = CapturingClient(rows=rows)
    value = source(client, restriction).capture_scoped(
        [SEASON, "2023-24"], ["Regular Season"]
    )
    assert client.sql.count("season_type IN UNNEST(@phases)") == 2
    assert "season = @season_1" in client.sql
    assert len(snapshot_evidence(value).covered_scopes) == 2


@pytest.mark.parametrize(
    "kind", ["broad", "phase", "season", "relation", "bytes", "rows", "mode"]
)
def test_denied_capture_does_not_dispatch(kind):
    client = CapturingClient(rows=regular_rows())
    restriction = policy()
    candidate = source(client, restriction)
    seasons, phases = [SEASON], ["Regular Season"]
    if kind == "phase":
        phases = ["Playoffs"]
    if kind == "season":
        seasons = ["2023-24"]
    if kind == "relation":
        candidate.source_for_season = lambda _: "test-project.other.facts"
    if kind == "bytes":
        candidate.byte_budget += 1
    if kind == "rows":
        candidate.max_rows -= 1
    if kind == "mode":
        candidate.source_mode = "warehouse"
    with pytest.raises(SemanticError):
        candidate.capture(seasons) if kind == "broad" else candidate.capture_scoped(
            seasons, phases
        )
    assert client.calls == []


@pytest.mark.parametrize(
    "defect", ["phase", "season", "truncated", "oversized", "null_id", "null_name"]
)
def test_wrong_or_incomplete_returned_rows_never_become_evidence(defect):
    rows = regular_rows()
    restriction = policy()
    total = None
    if defect == "phase":
        rows[0] = {**rows[0], "season_type": "Playoffs"}
    if defect == "season":
        rows[0] = {**rows[0], "season": "2023-24"}
    if defect == "truncated":
        total = len(rows) + 10
    if defect == "oversized":
        restriction = replace(restriction, max_rows=1)
    if defect == "null_id":
        rows[0] = {**rows[0], "player_id": None}
    if defect == "null_name":
        rows[0] = {**rows[0], "player_name": None}
    client = CapturingClient(rows=rows, total=total)
    with pytest.raises(SemanticError):
        source(client, restriction).capture_scoped([SEASON], ["Regular Season"])
    assert len(client.calls) == 1


def test_cache_does_not_reuse_broad_phase_or_other_source_mode():
    client = CapturingClient(rows=source_rows())
    warehouse = SemanticWarehouse(
        BigQuerySemanticSource(client, project="test-project")
    )
    assert len(warehouse.load([SEASON])[1].covered_scopes) == 2
    client.rows = regular_rows()
    client.total = len(client.rows)
    warehouse.source = ScopedSemanticSource(
        source(client, policy()), ["Regular Season"]
    )
    assert warehouse.load([SEASON])[1].covered_scopes == frozenset(
        {(SEASON, "Regular Season")}
    )
    warehouse.load([SEASON])
    assert len(client.calls) == 2
    warehouse.source.phases = ("Playoffs",)
    with pytest.raises(SemanticError):
        warehouse.load([SEASON])
    assert len(client.calls) == 2


def test_process_policy_applies_to_cached_broad_source_and_repository_without_ask():
    client = CapturingClient(rows=source_rows())
    warehouse = SemanticWarehouse(
        BigQuerySemanticSource(client, project="test-project")
    )
    warehouse.load([SEASON])
    configure_source_policy(policy())
    with pytest.raises(SemanticError):
        warehouse.load([SEASON])
    repo = BigQueryWarehouseRepository(settings(), client=client)
    with pytest.raises(SemanticError):
        repo.get_health()
    with pytest.raises(SemanticError):
        repo._query("SELECT 1")
    assert len(client.calls) == 1


def test_factory_replaces_old_broad_cache_and_never_enters_espn_or_serving():
    client = CapturingClient(rows=regular_rows())
    repo = BigQueryWarehouseRepository(settings(), client=client)
    repo._governed_warehouse = object()
    configure_source_policy(policy())
    warehouse = warehouse_for_repository(repo)
    assert isinstance(warehouse.source, ScopedSemanticSource)
    assert isinstance(warehouse.source.source, RetainedReviewSource)
    assert (
        warehouse.load([SEASON])[0]["review_metadata"]["archive_status"]
        == "unavailable"
    )
    assert len(client.calls) == 1


@pytest.mark.parametrize("method", ["capture", "historical_capture"])
def test_scoped_policy_denies_legacy_fallback_before_archive_or_espn(method):
    client = CapturingClient(error=NotFound("synthetic missing archive"))
    configure_source_policy(policy())
    with pytest.raises(SemanticError):
        getattr(
            ESPNDevSource(BigQuerySemanticSource(client, project="test-project")),
            method,
        )([SEASON])
    assert not client.calls


def identity_rows():
    return [
        dict(
            season=SEASON,
            season_type="Regular Season",
            player_id=1,
            player_name="Regular Player",
            team_abbr="AAA",
        )
    ]


def test_fixed_identity_query_is_id_season_phase_timestamp_and_byte_bounded():
    client = CapturingClient(rows=identity_rows())
    repo = BigQueryWarehouseRepository(
        settings(), client=client, source_policy=policy()
    )
    assert repo._fetch_player_identity(1)["player_name"] == "Regular Player"
    assert "AND player_id IN UNNEST(@player_ids)" in client.sql
    assert "season_type IN UNNEST(@phases)" in client.sql
    assert "FOR SYSTEM_TIME AS OF @snapshot_at" in client.sql
    params = {p.name: p.to_api_repr() for p in client.config.query_parameters}
    assert params["player_ids"]["parameterValue"]["arrayValues"] == [{"value": "1"}]
    assert client.config.maximum_bytes_billed == policy().maximum_bytes_billed
    assert len(client.calls) == 1


@pytest.mark.parametrize("kind", ["id", "season", "blank", "legacy", "health"])
def test_denied_identity_and_unrelated_queries_have_zero_sends(kind):
    client = CapturingClient(rows=identity_rows())
    repo = BigQueryWarehouseRepository(
        settings("2023-24" if kind == "season" else SEASON),
        client=client,
        source_policy=policy(),
    )
    with pytest.raises(SemanticError):
        if kind == "id":
            repo._fetch_player_identity(99)
        if kind == "season":
            repo._scoped_identity_rows([1], season="2023-24")
        if kind == "blank":
            repo.search_players("")
        if kind == "legacy":
            repo._query(f"SELECT * FROM `{RELATION}`")
        if kind == "health":
            repo.get_health()
    assert not client.calls


@pytest.mark.parametrize(
    "kind", ["wrong_id", "wrong_phase", "wrong_season", "ambiguous", "truncated"]
)
def test_identity_invalid_or_ambiguous_results_fail_without_fallback(kind):
    rows = identity_rows()
    restriction = policy()
    if kind == "wrong_id":
        rows[0]["player_id"] = 99
    if kind == "wrong_phase":
        rows[0]["season_type"] = "Playoffs"
    if kind == "wrong_season":
        rows[0]["season"] = "2023-24"
    if kind == "ambiguous":
        rows.append({**rows[0], "player_name": "Other Name"})
    if kind == "truncated":
        rows *= 2
        restriction = replace(restriction, identity_max_rows=1)
    client = CapturingClient(rows=rows)
    repo = BigQueryWarehouseRepository(
        settings(), client=client, source_policy=restriction
    )
    with pytest.raises(SemanticError):
        repo._fetch_player_identity(1)
    assert len(client.calls) == 1


def test_missing_identity_and_duplicate_names_are_not_silently_resolved():
    client = CapturingClient(rows=[])
    repo = BigQueryWarehouseRepository(
        settings(), client=client, source_policy=policy()
    )
    assert repo._fetch_player_identity(1) is None
    client.rows = identity_rows() + [{**identity_rows()[0], "player_id": 2}]
    assert len(repo.search_players("Regular Player")) == 2


@pytest.mark.parametrize("defect", ["shape", "type", "scope", "destination", "bytes"])
def test_authorization_compares_generated_shape_and_typed_parameters(defect):
    restriction = policy()
    client = CapturingClient(rows=regular_rows())
    source(client, restriction).capture_scoped([SEASON], ["Regular Season"])
    sql, config = client.sql, client.config
    if defect == "shape":
        sql += "; SELECT 1"
    if defect == "type":
        config.query_parameters = [
            bigquery.ScalarQueryParameter(
                "snapshot_at", "STRING", restriction.snapshot_at
            ),
            *config.query_parameters[1:],
        ]
    if defect == "scope":
        config.query_parameters = [
            *config.query_parameters[:-1],
            bigquery.ArrayQueryParameter("phases", "STRING", ["Playoffs"]),
        ]
    if defect == "destination":
        config.destination = "test-project.output.saved"
    if defect == "bytes":
        config.maximum_bytes_billed += 1
    with pytest.raises(SemanticError):
        restriction.authorize(
            sql=sql, config=config, seasons=[SEASON], phases=restriction.phases
        )
    # Authorization itself never dispatches; the sole callback was the valid capture above.
    assert len(client.calls) == 1


def test_process_and_context_policies_cannot_silently_override_each_other():
    configure_source_policy(policy())
    with pytest.raises(SemanticError):
        with source_policy_scope(replace(policy(), phases=("Playoffs",))):
            pass


def test_loaded_scoped_snapshot_is_reused_for_identity_and_roster_is_bounded():
    client = CapturingClient(rows=regular_rows())
    restriction = policy()
    repo = BigQueryWarehouseRepository(
        settings("2026-27"), client=client, source_policy=restriction
    )
    warehouse = warehouse_for_repository(repo)
    value, evidence = warehouse.load([SEASON])
    assert len(evidence.rows) == len(regular_rows())
    assert {
        p["player_id"] for p in warehouse.players(evidence)
    } <= restriction.allowed_player_ids
    assert repo._fetch_player_identity(1)["player_id"] == 1
    assert len(client.calls) == 1
    assert value["source_timestamp"] == restriction.snapshot_at


def test_unknown_capture_or_identity_shape_is_denied_at_real_dispatch(monkeypatch):
    import app.agent.semantic_source as semantic_source
    import app.repository._bigquery as repository

    client = CapturingClient(rows=regular_rows())
    monkeypatch.setattr(semantic_source, "capture_sql", lambda *a: "SELECT 1; SELECT 2")
    with pytest.raises(SemanticError):
        source(client, policy()).capture_scoped([SEASON], ["Regular Season"])
    monkeypatch.setattr(
        repository, "identity_sql", lambda *a: "SELECT * FROM unrelated"
    )
    repo = BigQueryWarehouseRepository(
        settings(), client=client, source_policy=policy()
    )
    with pytest.raises(SemanticError):
        repo._fetch_player_identity(1)
    assert client.calls == []


@pytest.mark.parametrize("ids", [[True], ["1"], [1.0], [], [99]])
def test_unapproved_or_untyped_identity_ids_are_denied_before_read(ids):
    client = CapturingClient(rows=identity_rows())
    repo = BigQueryWarehouseRepository(
        settings(), client=client, source_policy=policy()
    )
    with pytest.raises(SemanticError):
        repo._scoped_identity_rows(ids)
    assert client.calls == []


def test_optional_position_artifact_is_denied_before_source_dispatch():
    client = CapturingClient(rows=regular_rows())
    candidate = source(client, policy())
    candidate.position_context_path = "unapproved-position-context.json"
    with pytest.raises(SemanticError):
        candidate.capture_scoped([SEASON], ["Regular Season"])
    assert client.calls == []


@pytest.mark.parametrize(
    "changes",
    [
        {"maximum_bytes_billed": 100_000_001},
        {"max_rows": 100_001},
        {"identity_max_rows": 1001},
        {"submission_timeout": 31},
        {"result_timeout": 121},
        {"snapshot_at": "2025-06-30"},
        {"phases": ("Both",)},
        {"allowed_player_ids": frozenset({True})},
    ],
)
def test_invalid_trusted_policy_is_rejected(changes):
    with pytest.raises((SemanticError, ValueError)):
        replace(policy(), **changes)


class TabledataClient:
    """Record actual low-level callbacks, including non-query read APIs."""

    def __init__(self):
        self.callbacks = []

    def get_table(self, table_id, **kwargs):
        from app.repository._constants import RECENT_PERFORMANCE_TABLE_FIELDS

        self.callbacks.append(("get_table", table_id))
        return SimpleNamespace(schema=RECENT_PERFORMANCE_TABLE_FIELDS)

    def list_rows(self, table_id, **kwargs):
        self.callbacks.append(("list_rows", table_id))
        return []

    def query(self, sql, **kwargs):
        self.callbacks.append(("query", sql))
        raise AssertionError("Unapproved query dispatch")


@pytest.mark.parametrize("installed", ["explicit", "process"])
@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize(
    "entry",
    [
        "api",
        "try_api",
        "initial",
        "dates",
        "games",
        "players",
        "player",
    ],
)
def test_source_policy_denies_tabledata_before_metadata_cache_or_fallback(
    installed, cached, entry
):
    from time import monotonic

    from app.agent.execution import CURRENT

    assert CURRENT.get() is None
    client = TabledataClient()
    restriction = policy()
    repo = BigQueryWarehouseRepository(
        settings(),
        client=client,
        source_policy=restriction if installed == "explicit" else None,
    )
    if cached:
        repo._recent_performance_rows_cache = (
            monotonic(),
            [{"unapproved_cached_row": True}],
        )
    if installed == "process":
        configure_source_policy(restriction)
    calls = {
        "api": repo._fetch_recent_performance_table_rows_api,
        "try_api": repo._try_fetch_recent_performance_table_rows,
        "initial": repo.get_recent_performance_initial,
        "dates": repo.get_recent_performance_dates,
        "games": lambda: repo.get_recent_performance_games(game_date="2025-01-01"),
        "players": lambda: repo.get_recent_performance_players(game_date="2025-01-01"),
        "player": lambda: repo.get_recent_performance_player(
            1, game_id="synthetic-game"
        ),
    }
    with pytest.raises(SemanticError) as exc:
        calls[entry]()
    assert exc.value.code == "source_policy_denied"
    assert client.callbacks == []


def test_default_tabledata_api_and_cache_remain_available_without_source_policy():
    from time import monotonic

    client = TabledataClient()
    repo = BigQueryWarehouseRepository(settings(), client=client)
    assert repo._fetch_recent_performance_table_rows_api() == []
    table = "test-project.nba_gold.recent_performance_workbench"
    assert client.callbacks == [("get_table", table), ("list_rows", table)]
    repo._recent_performance_rows_cache = (
        monotonic(),
        [{"synthetic_cached_row": True}],
    )
    assert repo._fetch_recent_performance_table_rows_api() == [
        {"synthetic_cached_row": True}
    ]
    assert client.callbacks == [("get_table", table), ("list_rows", table)]


@pytest.mark.parametrize(
    "question",
    [
        "Tell me about Regular Player performance in 2024-25 regular season",
        "Give me a player overview of Regular Player in the 2024-25 regular season.",
    ],
)
def test_plain_overview_omits_only_unapproved_optional_prior_capture(question):
    from app.agent.semantic_answer import SemanticAsk

    client = CapturingClient(rows=regular_rows())
    restriction = policy()
    warehouse = SemanticWarehouse(
        ScopedSemanticSource(source(client, restriction), restriction.phases)
    )
    agent = SemanticAsk(
        SimpleNamespace(season="2026-27", agent_conversation_max_turns=6), warehouse
    )
    value = agent.answer(
        question,
        client=None,
        model="synthetic",
    )
    assert value["status"] == "ok"
    assert len(client.calls) == 1
    params = {p.name: p for p in client.config.query_parameters}
    assert params["season_0"].value == SEASON
    assert "season_1" not in params
    scope = value["semantic_evidence"]["scope"]
    assert scope["start"] == "2024-07-01"
    assert scope["end"] == "2025-06-30"
    assert scope["phases"] == ["Regular Season"]
    assert scope["previous_start"] == "2023-07-01"
    assert scope["previous_end"] == "2024-06-30"
    assert "2023-24" in scope["seasons"]
    metrics = value["semantic_evidence"]["metrics"]
    assert all(m["change"] is None for m in metrics)
    assert all(m["previous"]["value"] is None for m in metrics)
    assert all(
        "against his baseline" not in insight["text"]
        for insight in value["semantic_evidence"]["insights"]
    )
    assert value["source_coverage"]["retained_seasons"] == [SEASON]
    captured = warehouse.cached_snapshot([SEASON])
    assert {(c["season"], c["phase"]) for c in captured["coverage"]} == {
        (SEASON, "Regular Season")
    }
    assert captured["requested_scopes"] == [[SEASON, "Regular Season"]]
    assert value["source_coverage"]["archive_status"] == "unavailable"


@pytest.mark.parametrize(
    "question",
    [
        "Tell me about Regular Player performance from 2023-11-01 through 2024-11-01 regular season",
        "Tell me about Regular Player performance in 2024-25 regular season relative to his baseline",
    ],
)
def test_overview_never_drops_unapproved_required_window_or_explicit_baseline(question):
    from app.agent.semantic_answer import SemanticAsk

    client = CapturingClient(rows=regular_rows())
    restriction = policy()
    warehouse = SemanticWarehouse(
        ScopedSemanticSource(source(client, restriction), restriction.phases)
    )
    agent = SemanticAsk(
        SimpleNamespace(season="2026-27", agent_conversation_max_turns=6), warehouse
    )
    value = agent.answer(question, client=None, model="synthetic")
    assert value["status"] != "ok"
    assert not client.calls


def test_unrestricted_plain_overview_keeps_optional_prior_season_capture():
    from app.agent.semantic_answer import SemanticAsk

    rows = regular_rows() + [
        {
            **r,
            "season": "2023-24",
            "game_date": str(r["game_date"])
            .replace("2024", "2023")
            .replace("2025", "2024"),
            "game_id": "prior-" + r["game_id"],
        }
        for r in regular_rows()
    ]
    client = CapturingClient(rows=rows)
    warehouse = SemanticWarehouse(
        BigQuerySemanticSource(client, project="test-project")
    )
    agent = SemanticAsk(
        SimpleNamespace(season="2026-27", agent_conversation_max_turns=6), warehouse
    )
    value = agent.answer(
        "Tell me about Regular Player performance in 2024-25 regular season",
        client=None,
        model="synthetic",
    )
    assert value["status"] == "ok"
    params = {p.name: p for p in client.config.query_parameters}
    assert {p.value for name, p in params.items() if name.startswith("season_")} == {
        "2024-25",
        "2023-24",
    }
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    "modifier",
    [
        "with year-over-year statistics",
        "with YoY statistics",
        "with year over year statistics",
        "with season-over-season statistics",
        "relative to last season",
        "with growth analysis",
        "with additional context",
    ],
)
def test_explicit_or_ambiguous_overview_comparison_never_omits_prior_dependency(
    modifier,
):
    from app.agent.semantic_answer import SemanticAsk

    client = CapturingClient(rows=regular_rows())
    restriction = policy()
    warehouse = SemanticWarehouse(
        ScopedSemanticSource(source(client, restriction), restriction.phases)
    )
    agent = SemanticAsk(
        SimpleNamespace(season="2026-27", agent_conversation_max_turns=6), warehouse
    )
    value = agent.answer(
        "Give me a player overview of Regular Player in the 2024-25 regular season "
        + modifier
        + ".",
        client=None,
        model="synthetic",
    )
    assert value["status"] in {"source_policy_denied", "clarification_required"}
    assert client.calls == []


@pytest.mark.parametrize(
    "question, expected",
    [
        (
            "Give me a player overview of Regular Player in the 2024-25 regular season.",
            True,
        ),
        ("Tell me about Regular Player performance in 2024-25 regular season", True),
        ("How did Regular Player perform during 2024-25 regular season?", True),
        (
            "Tell me about Regular Player performance from 2023-11-01 through 2024-11-01 regular season",
            True,
        ),
        (
            "Give me a player overview of Regular Player with YoY statistics in the 2024-25 regular season.",
            False,
        ),
        (
            "Give me a year-over-year overview of Regular Player in the 2024-25 regular season.",
            False,
        ),
    ],
)
def test_optional_baseline_omission_requires_recognized_plain_overview(
    question, expected
):
    from app.agent.semantic_answer import plain_scoped_overview

    assert plain_scoped_overview(question) is expected


@pytest.mark.parametrize("suffix", ["", "/stream"])
@pytest.mark.parametrize("extra", ["", " with YoY statistics"])
def test_plain_hyphenated_name_overview_at_actual_http_routes(suffix, extra):
    import json
    from datetime import date

    import app.main as main
    from app.agent.runtime_policy import configure_runtime_policy
    from fastapi.testclient import TestClient
    from tests.test_runtime_policy import policy as runtime_policy
    from tests.test_runtime_policy import sdk
    from tests.test_semantic_migration import settings as app_settings

    restriction = replace(
        policy(),
        relations=(("2025-26", RELATION),),
        snapshot_at="2026-06-30T12:00:00+00:00",
    )
    rows = []
    for row in regular_rows():
        day = date.fromisoformat(row["game_date"])
        rows.append(
            {
                **row,
                "season": "2025-26",
                "game_date": day.replace(year=day.year + 1).isoformat(),
                "player_name": {
                    1: "Avery Example-Sample",
                    2: "Blair Example",
                    3: "Casey Sample",
                }[row["player_id"]],
            }
        )
    data = CapturingClient(rows=rows)
    config = replace(app_settings("2026-27"), performance_cache_prewarm_enabled=False)
    repo = BigQueryWarehouseRepository(config, client=data)
    model_sends = []
    model = sdk(
        lambda request: (
            model_sends.append(request)
            or pytest.fail("Overview must use no model transport")
        )
    )
    previous = dict(main.app.dependency_overrides)
    main.app.dependency_overrides.update(
        {
            main.get_settings: lambda: config,
            main.get_repository: lambda: repo,
            main.get_agent_client: lambda: model,
        }
    )
    configure_runtime_policy(runtime_policy(source_policy=restriction))
    try:
        with TestClient(main.app) as client:
            response = client.post(
                "/api/agent/ask" + suffix,
                json={
                    "question": "Give me a player overview of Avery Example-Sample for the 2025–26 regular season"
                    + extra
                    + "."
                },
            )
            assert response.status_code == 200, response.text
            value = (
                response.json()
                if not suffix
                else next(
                    json.loads(line[6:])["payload"]
                    for line in response.text.splitlines()
                    if line.startswith("data: ")
                    and json.loads(line[6:]).get("type") == "final"
                )
            )
            if extra:
                assert value["status"] != "ok"
                assert not data.calls
            else:
                assert value["status"] == "ok", value
                assert len(data.calls) == 1
                scope = value["semantic_evidence"]["scope"]
                assert scope["start"] == "2025-07-01" and scope["end"] == "2026-06-30"
                assert scope["phases"] == ["Regular Season"]
                assert value["source_coverage"]["retained_seasons"] == ["2025-26"]
            assert not model_sends
    finally:
        configure_runtime_policy(None)
        main.app.dependency_overrides.clear()
        main.app.dependency_overrides.update(previous)
        model.close()
