"""Shared conformance: hostile providers never gain execution authority."""

import asyncio
from dataclasses import replace
from time import monotonic
from types import SimpleNamespace

import pytest
from app.agent import intelligence as host
from app.agent import intelligence_contract as api
from app.agent.execution import ExecutionFailure
from app.agent.reference_planner import ReferenceProvider
from app.agent.semantics import SemanticError
from tests.test_semantic_migration import settings

PLAYERS = [
    dict(player_id=1, player_name="Alex Example"),
    dict(player_id=2, player_name="Blair Sample"),
]


def context(question="Alex Example points per game in the 2024-25 regular season"):
    return host.planning_context(question, "2024-25", PLAYERS, ["ATL", "BOS"])


def query(**kw):
    return replace(
        api.QueryProposal(
            "pts",
            api.Scope("2024-25", "Regular Season"),
            "summary",
            "average",
            player_name="Alex Example",
        ),
        **kw,
    )


def plan(*queries, status="query"):
    return api.QueryPlan("metrics", status, queries or (query(),))


class FixtureProvider:
    descriptor = replace(ReferenceProvider.descriptor, provider_id="fixture")

    def __init__(self, result):
        self.result = result

    def prompts(self):
        return api.PromptBundle("fixture/1", None)

    def classify(self, context):
        return api.IntentSelection("metrics", False)

    async def plan(self, context, intent, **kwargs):
        return self.result


def run(provider, ctx=None, **kwargs):
    return asyncio.run(
        host.propose(
            provider,
            ctx or context(),
            budget=api.PlanningBudget(monotonic() + 1, 1, 100),
            model=None,
            **kwargs,
        )
    )


@pytest.fixture(autouse=True)
def clear_loader():
    host._load.cache_clear()
    yield
    host._load.cache_clear()


def test_reference_does_not_discover_installed_packages(monkeypatch):
    monkeypatch.setattr(
        host,
        "entry_points",
        lambda **kw: pytest.fail("reference discovers private code"),
    )
    assert isinstance(
        host.get_provider(replace(settings(), openai_api_key=None)), ReferenceProvider
    )


def test_explicit_provider_factory_once_and_never_automatically_activated(monkeypatch):
    calls = []

    def factory():
        calls.append(1)
        return FixtureProvider(plan())

    monkeypatch.setattr(
        host, "entry_points", lambda **kw: [SimpleNamespace(load=lambda: factory)]
    )
    config = replace(settings(), ask_intelligence_provider="fixture")
    a = host.get_provider(config)
    assert host.get_provider(config) is a
    assert calls == [1]
    assert isinstance(host.get_provider(settings()), ReferenceProvider)


@pytest.mark.parametrize(
    "problem",
    [
        "missing",
        "duplicate",
        "api",
        "semantics",
        "routes",
        "credentials",
        "factory",
        "descriptor",
    ],
)
def test_bad_explicit_selection_fails_closed(monkeypatch, problem):
    provider = FixtureProvider(plan())
    if problem == "api":
        provider.descriptor = replace(provider.descriptor, api_version="wrong")
    if problem == "semantics":
        provider.descriptor = replace(
            provider.descriptor, semantic_contract_versions=()
        )
    if problem == "routes":
        provider.descriptor = replace(provider.descriptor, route_ids=("arbitrary",))
    if problem == "credentials":
        provider.descriptor = replace(provider.descriptor, requires_model=True)
    if problem == "descriptor":
        provider.descriptor = {}

    def factory():
        if problem == "factory":
            raise RuntimeError("private secret")
        return provider

    entries = [SimpleNamespace(load=lambda: factory)] * (
        0 if problem == "missing" else 2 if problem == "duplicate" else 1
    )
    monkeypatch.setattr(host, "entry_points", lambda **kw: entries)
    with pytest.raises(host.ProviderConfigurationError) as caught:
        host.get_provider(
            replace(
                settings(), ask_intelligence_provider="fixture", openai_api_key=None
            )
        )
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize(
    "result",
    [
        {},
        {"sql": "SELECT 1"},
        api.ProviderFailure("provider_timeout"),
        api.ProviderFailure("budget_exhausted"),
        api.QueryPlan("metrics", "query", ()),
        api.QueryPlan("arbitrary", "query", (query(),)),
        api.HandlerPlan("overview"),
        plan(query(metric="raw_sql")),
        plan(query(limit=True)),
        plan(query(limit=101)),
        plan(query(aggregation="sum")),
        plan(query(scope=api.Scope("2024-25", "Regular Season", window="arbitrary"))),
        plan(query(scope=api.Scope("2024-25", "Regular Season", as_of="2025-02-30"))),
        plan(query(scope=api.Scope("2024-25", "Regular Season", team_abbr="UNKNOWN"))),
        plan(query(player_name={"sql": "SELECT 1"})),
    ],
)
def test_hostile_result_rejected_before_normalization(result):
    with pytest.raises(ExecutionFailure, match="invalid_provider_output"):
        run(FixtureProvider(result))


def test_malformed_frozen_instance_is_revalidated():
    q = query()
    object.__setattr__(q, "limit", "ten")
    with pytest.raises(ExecutionFailure, match="invalid_provider_output"):
        run(FixtureProvider(plan(q)))


def test_normalization_precedes_final_comparison_validation():
    q = query(operation="rank", player_name=None)
    ctx = context("Top 5 players by points per game in the 2024-25 regular season")
    raw = plan(q, q, status="compare")
    accepted = run(FixtureProvider(raw), ctx)
    assert host.normalized_plan(accepted, ctx, PLAYERS)["status"] == "query"
    accidental = plan(
        q, replace(q, scope=api.Scope("2023-24", "Regular Season")), status="compare"
    )
    assert (
        host.normalized_plan(run(FixtureProvider(accidental), ctx), ctx, PLAYERS)[
            "status"
        ]
        == "query"
    )
    explicit = context("Compare rankings in 2024-25 versus 2023-24")
    with pytest.raises(SemanticError):
        host.normalized_plan(
            run(FixtureProvider(accidental), explicit), explicit, PLAYERS
        )


def test_comparison_identity_is_publicly_resolved():
    ctx = context("Compare Alex Example points per game in 2024-25 versus 2023-24")
    a = query()
    b = query(player_name="alex example", scope=api.Scope("2023-24", "Regular Season"))
    result = host.normalized_plan(
        run(FixtureProvider(plan(a, b, status="compare")), ctx), ctx, PLAYERS
    )
    assert [q["season"] for q in result["queries"]] == ["2024-25", "2023-24"]
    b = replace(b, player_name="Blair Sample")
    with pytest.raises(SemanticError, match="player contradicts"):
        host.normalized_plan(
            run(FixtureProvider(plan(a, b, status="compare")), ctx), ctx, PLAYERS
        )


def test_forged_provenance_and_qualification_do_not_override_host():
    ctx = context("Alex Example total points in the 2024-25 regular season")
    with pytest.raises(SemanticError, match="aggregation"):
        host.normalized_plan(
            run(FixtureProvider(plan(query(aggregation_source="explicit")))),
            ctx,
            PLAYERS,
        )
    with pytest.raises(SemanticError, match="qualification"):
        host.normalized_plan(
            replace(plan(), qualification="league_leader"), context(), PLAYERS
        )


@pytest.mark.parametrize(
    "error,code",
    [
        (api.GatewayTimeout, "provider_timeout"),
        (api.GatewayBudgetExhausted, "budget_exhausted"),
        (api.GatewayUnavailable, "provider_unavailable"),
    ],
)
def test_gateway_failure_codes_are_host_owned(error, code):
    class Failing(FixtureProvider):
        async def plan(self, *args, **kwargs):
            raise error("private secret")

    with pytest.raises(ExecutionFailure, match=code) as caught:
        run(Failing(None))
    assert "secret" not in str(caught.value)


def test_async_deadline_cancels_provider():
    cancelled = []

    class Slow(FixtureProvider):
        async def plan(self, *args, **kwargs):
            try:
                await asyncio.sleep(5)
            finally:
                cancelled.append(True)

    async def attempt():
        return await host.propose(
            Slow(None),
            context(),
            model=None,
            budget=api.PlanningBudget(monotonic() + 0.01, 1, 10),
        )

    with pytest.raises(ExecutionFailure, match="provider_timeout"):
        asyncio.run(attempt())
    assert cancelled == [True]


def test_actual_transport_retries_consume_same_budget():
    calls = []

    def create(**kw):
        calls.append(1)
        raise ConnectionError("secret endpoint")

    from app.agent.execution import request_policy

    config = replace(settings(), openai_agent_retry_base_delay_seconds=0)
    gateway = host.ModelGateway(
        SimpleNamespace(responses=SimpleNamespace(create=create)),
        "fixture",
        api.PlanningBudget(monotonic() + 5, 1, 100),
    )
    with request_policy(config, "openai"):
        with pytest.raises(api.GatewayBudgetExhausted):
            asyncio.run(
                gateway.generate_plan(system_prompt="fixture", user_payload="{}")
            )
    assert len(calls) == gateway.calls == 1


def test_context_contains_no_evidence_answers_credentials_or_settings():
    prior = {
        "scope": {"season": "2023-24", "season_type": "Regular Season"},
        "players": [PLAYERS[0]],
        "metrics": ["pts"],
        "answer": "SECRET",
        "evidence": {"value": 123},
        "api_key": "SECRET",
    }
    ctx = host.planning_context(
        "What about rebounds?", "2024-25", PLAYERS, ["ATL"], prior
    )
    assert "SECRET" not in repr(ctx)
    assert ctx.prior.scopes[0].season == "2023-24"
    assert ctx.supported_seasons == ("2024-25", "2023-24")


def test_handler_intent_dispatches_without_planning():
    class Handler(FixtureProvider):
        descriptor = replace(FixtureProvider.descriptor, route_ids=("overview",))

        def classify(self, context):
            return api.IntentSelection("overview", False)

        async def plan(self, *args, **kwargs):
            pytest.fail("handler must not plan")

    ctx = replace(context(), routes=(api.RouteCapability("overview", "handler"),))
    assert run(Handler(None), ctx) == api.HandlerPlan("overview")


def test_public_import_graph_has_one_optional_legacy_boundary():
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    private = {
        "app.agent.question_intent",
        "app.agent.semantic_planner",
        "app.agent.planner",
        "app.agent.router",
    }
    for file in (root / "app").rglob("*.py"):
        if file.name == "legacy_loader.py":
            continue
        for node in ast.walk(ast.parse(file.read_text())):
            if isinstance(node, ast.ImportFrom):
                assert node.module not in private, (file, node.lineno)
            elif isinstance(node, ast.Import):
                assert not any(a.name in private for a in node.names), (
                    file,
                    node.lineno,
                )
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in {"import_module", "__import__"}
            ):
                assert not any(
                    isinstance(a, ast.Constant) and a.value in private
                    for a in node.args
                ), (file, node.lineno)


def test_missing_legacy_tree_is_sanitized_and_cannot_fall_back(monkeypatch):
    from app.agent.service import AgentDisabledError, StatsAgent

    monkeypatch.setattr("app.agent.service.legacy_available", lambda: False)
    service = StatsAgent(settings(), SimpleNamespace())
    with pytest.raises(
        AgentDisabledError, match="Legacy repository analytics are unavailable"
    ):
        service.answer("Alex Example points per game")


def test_host_precedence_never_consults_hostile_classification(monkeypatch):
    from tests.test_reference_intelligence import agent

    class Hostile(FixtureProvider):
        def classify(self, context):
            pytest.fail("precedence route called provider")

    monkeypatch.setattr(host, "get_provider", lambda settings: Hostile(None))
    service = agent()
    for question in (
        "How did Alex Example play in the conference finals?",
        "Show Alex Example's overview for the 2024-25 regular season.",
    ):
        service.answer(question)


def test_provider_cannot_relax_explicit_or_default_qualification():
    ctx = context(
        "Top 5 players by points per game in the 2024-25 regular season, minimum 10 games"
    )
    with pytest.raises(SemanticError, match="qualification"):
        host.normalized_plan(
            plan(query(operation="rank", player_name=None, min_games=1)), ctx, PLAYERS
        )
    ctx = context("Top 5 players by points per game in the 2024-25 regular season")
    with pytest.raises(SemanticError, match="Qualification"):
        host.normalized_plan(
            plan(query(operation="rank", player_name=None, min_games=1)), ctx, PLAYERS
        )
    ctx = context(
        "Top 5 players by field goal percentage in the 2024-25 regular season"
    )
    with pytest.raises(SemanticError, match="attempt threshold"):
        host.normalized_plan(
            plan(
                query(
                    metric="fg_pct",
                    aggregation="ratio",
                    operation="rank",
                    player_name=None,
                    min_attempts=1,
                )
            ),
            ctx,
            PLAYERS,
        )


def test_concurrent_reference_plans_do_not_leak_identity_or_metric():
    async def requests():
        provider = ReferenceProvider()
        contexts = [
            context("Alex Example points per game in the 2024-25 regular season"),
            context("Blair Sample rebounds per game in the 2024-25 regular season"),
        ]
        return await asyncio.gather(
            *(
                host.propose(
                    provider,
                    c,
                    model=None,
                    budget=api.PlanningBudget(monotonic() + 1, 0, 0),
                )
                for c in contexts
            )
        )

    a, b = asyncio.run(requests())
    assert (a.queries[0].player_name, a.queries[0].metric) == ("alex example", "pts")
    assert (b.queries[0].player_name, b.queries[0].metric) == ("blair sample", "reb")


def test_provider_cannot_replace_explicit_player_or_season():
    with pytest.raises(SemanticError, match="player contradicts"):
        host.normalized_plan(
            plan(query(player_name="Blair Sample")), context(), PLAYERS
        )
    ctx = context("Alex Example points per game in the 2023-24 regular season")
    with pytest.raises(SemanticError, match="season contradicts"):
        host.normalized_plan(plan(), ctx, PLAYERS)


def test_prompt_revision_logged_without_prompt_text(caplog):
    with caplog.at_level("INFO", logger="nba.agent.intelligence"):
        run(FixtureProvider(plan()))
    assert "revision=fixture/1" in caplog.text


def test_request_identity_catalog_excludes_unmentioned_roster_and_ambiguous_aliases():
    roster = PLAYERS + [
        dict(player_id=3, player_name="Casey Example", aliases=["Example"])
    ]
    roster[0] = dict(roster[0], aliases=["Example"])
    ctx = host.planning_context("Example rebounds per game", "2024-25", roster, [])
    assert ctx.player_names == ctx.player_references == ()
    withheld = host.normalized_plan(plan(query(metric="reb")), ctx, roster)
    assert withheld["status"] == "clarification_required"
    assert {p["player_name"] for p in withheld["identity"]["matches"]} == {
        "Alex Example",
        "Casey Example",
    }
    ctx = host.planning_context("Alex Example rebounds per game", "2024-25", roster, [])
    assert ctx.player_names == ("Alex Example",)
    assert ctx.player_references == (
        api.PlayerReference("resolved_player_1", "Alex Example"),
    )
    schema = host.provider_plan_schema(ctx)["properties"]["plan"]["anyOf"][0]
    assert schema["properties"]["queries"]["items"]["properties"]["player_name"][
        "enum"
    ] == [None, "resolved_player_1"]
    assert "reason" in schema["required"]
    assert schema["properties"]["reason"]["enum"] == [None]
    withheld = host.provider_plan_schema(ctx)["properties"]["plan"]["anyOf"][2]
    assert "ambiguous_aggregation" in withheld["properties"]["reason"]["enum"]


def test_two_mentions_do_not_authorize_a_third_player():
    roster = PLAYERS + [dict(player_id=3, player_name="Casey Example")]
    ctx = host.planning_context(
        "Compare Alex Example and Blair Sample points per game", "2024-25", roster, []
    )
    with pytest.raises(SemanticError, match="player contradicts"):
        host.normalized_plan(plan(query(player_name="Casey Example")), ctx, roster)


def test_pronoun_requires_host_authorized_prior_identity():
    question = "His rebounds per game"
    ctx = context(question)
    with pytest.raises(SemanticError, match="intended player"):
        host.normalized_plan(plan(query(metric="reb")), ctx, PLAYERS)
    history = {
        "scope": {"season": "2024-25", "season_type": "Regular Season"},
        "players": [PLAYERS[0]],
        "metrics": ["pts"],
    }
    ctx = host.planning_context(question, "2024-25", PLAYERS, [], history)
    assert ctx.player_names == ("Alex Example",)
    assert (
        host.normalized_plan(plan(query(metric="reb")), ctx, PLAYERS)["status"]
        == "query"
    )


@pytest.mark.parametrize(
    "question,scope,field,value",
    [
        (
            "Alex Example points per game in home games",
            api.Scope("2024-25", "Regular Season"),
            "home_away",
            "HOME",
        ),
        (
            "Alex Example points per game from 2025-01-01 through 2025-01-10",
            api.Scope("2024-25", "Regular Season"),
            "window",
            "date_range",
        ),
    ],
)
def test_host_fills_omitted_literal_scope(question, scope, field, value):
    result = host.normalized_plan(plan(query(scope=scope)), context(question), PLAYERS)
    assert result["queries"][0][field] == value


def test_host_rejects_conflicting_nondefault_literal_scope():
    ctx = context("Alex Example points per game in home games")
    with pytest.raises(SemanticError, match="scope contradicts"):
        host.normalized_plan(
            plan(query(scope=api.Scope("2024-25", "Regular Season", home_away="AWAY"))),
            ctx,
            PLAYERS,
        )


@pytest.mark.parametrize(
    "error,code",
    [
        (api.GatewayTimeout, "provider_timeout"),
        (api.GatewayBudgetExhausted, "budget_exhausted"),
        (api.GatewayUnavailable, "provider_unavailable"),
    ],
)
def test_provider_cannot_swallow_a_gateway_failure(monkeypatch, error, code):
    budget = api.PlanningBudget(monotonic() + 1, 1, 100)
    gateway = host.ModelGateway(None, "fixture", budget, context=context())

    def fail(*args):
        raise error("must not leak")

    monkeypatch.setattr(gateway, "_generate", fail)

    class Swallows(FixtureProvider):
        async def plan(self, *args, model, **kwargs):
            try:
                await model.generate_plan(system_prompt="fixture", user_payload="{}")
            except api.GatewayError:
                return api.Withheld("clarification_required", "ambiguous_scope")

    with pytest.raises(ExecutionFailure, match=code):
        asyncio.run(
            host.propose(Swallows(None), context(), model=gateway, budget=budget)
        )


@pytest.mark.parametrize(
    "selected,prefix",
    [("openai", "openai"), ("claude", "anthropic"), ("openrouter", "openrouter")],
)
def test_startup_and_request_readiness_share_one_cached_factory(
    monkeypatch, selected, prefix
):
    from app.agent.execution import request_policy

    calls = []
    provider = FixtureProvider(plan())
    provider.descriptor = replace(provider.descriptor, requires_model=True)

    def factory():
        calls.append(1)
        return provider

    monkeypatch.setattr(
        host, "entry_points", lambda **kw: [SimpleNamespace(load=lambda: factory)]
    )
    empty = replace(
        settings(),
        ask_intelligence_provider="fixture",
        openai_api_key=None,
        anthropic_api_key=None,
        openrouter_api_key=None,
    )
    with pytest.raises(host.ProviderConfigurationError):
        host.get_provider(empty)
    configured = replace(empty, **{prefix + "_api_key": "synthetic-private-key"})
    assert host.get_provider(configured) is provider
    with request_policy(configured, selected):
        assert host.get_provider(configured) is provider
    missing = "claude" if selected == "openai" else "openai"
    with request_policy(configured, missing):
        with pytest.raises(ExecutionFailure, match="provider_unavailable") as caught:
            host.get_provider(configured)
        assert "synthetic-private-key" not in str(caught.value)
    assert calls == [1]


def test_version_one_provider_is_rejected_before_prompt_lookup(monkeypatch):
    provider = FixtureProvider(plan())
    provider.descriptor = replace(provider.descriptor, api_version="nba_intelligence/1")
    provider.prompts = lambda: pytest.fail("incompatible provider prompt lookup")

    def entries(**kwargs):
        assert kwargs["group"] == "nba_stats_desk.intelligence.v2"
        return [SimpleNamespace(load=lambda: lambda: provider)]

    monkeypatch.setattr(host, "entry_points", entries)
    with pytest.raises(host.ProviderConfigurationError):
        host.get_provider(replace(settings(), ask_intelligence_provider="fixture"))


@pytest.mark.parametrize(
    "result",
    [
        plan(query(metric="reb")),
        api.Withheld("clarification_required", "ambiguous_identity"),
    ],
)
def test_bare_surname_retains_host_candidates_without_alias_records(result):
    roster = [
        dict(player_id=1, player_name="Alex Example"),
        dict(player_id=2, player_name="Casey Example"),
    ]
    ctx = host.planning_context(
        "How many rebounds per game did Example average in the 2024-25 regular season?",
        "2024-25",
        roster,
        [],
    )
    assert ctx.player_references == ()
    withheld = host.normalized_plan(result, ctx, roster)
    assert withheld["status"] == "clarification_required"
    assert {p["player_name"] for p in withheld["identity"]["matches"]} == {
        "Alex Example",
        "Casey Example",
    }


@pytest.mark.parametrize("endpoint", ["/api/agent/ask", "/api/agent/ask/stream"])
def test_missing_selected_key_is_a_sanitized_endpoint_failure(monkeypatch, endpoint):
    from app.main import app, get_agent_client, get_repository, get_settings
    from app.repository import BigQueryWarehouseRepository
    from fastapi.testclient import TestClient
    from tests.test_reference_intelligence import config, source

    provider = FixtureProvider(plan())
    provider.descriptor = replace(provider.descriptor, requires_model=True)
    monkeypatch.setattr(
        host,
        "entry_points",
        lambda **kw: [SimpleNamespace(load=lambda: lambda: provider)],
    )
    configured = replace(
        config(),
        ask_intelligence_provider="fixture",
        anthropic_api_key="synthetic-private-key",
        performance_cache_prewarm_enabled=False,
    )
    repo = BigQueryWarehouseRepository(configured, client=SimpleNamespace())
    repo._governed_warehouse = source()
    old = dict(app.dependency_overrides)
    app.dependency_overrides.update(
        {
            get_settings: lambda: configured,
            get_repository: lambda: repo,
            get_agent_client: lambda: None,
        }
    )
    try:
        with TestClient(app) as client:
            response = client.post(
                endpoint + "?season=2024-25",
                json={
                    "question": "Alex Example points per game in the 2024-25 regular season",
                    "provider": "openai",
                },
            )
        assert "provider_unavailable" in response.text
        assert "synthetic-private-key" not in response.text
        assert "agent execution failed" not in response.text
        assert response.status_code == (200 if endpoint.endswith("stream") else 502)
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(old)


@pytest.mark.parametrize(
    "question",
    [
        "Example points per game?",
        "Compare Blair Sample and Example points per game?",
        "Compare Zqxv Blorp and Example points per game?",
        "Compare Zqxv Blorp and Blair Sample and Example points per game?",
    ],
)
def test_identity_candidates_precede_provider_planning(monkeypatch, caplog, question):
    caplog.set_level("INFO", logger="nba.agent.intelligence")
    players = [*PLAYERS, dict(player_id=3, player_name="Casey Example")]
    provider = FixtureProvider(plan())
    monkeypatch.setattr(host, "get_provider", lambda settings: provider)
    monkeypatch.setattr(
        provider,
        "classify",
        lambda context: pytest.fail("Ambiguous identity reached provider"),
    )
    result = host.plan_question(
        None,
        settings=settings(),
        model="synthetic",
        question=question,
        selected_season="2024-25",
        players=players,
    )
    assert result["status"] == "clarification_required"
    assert result["queries"] == []
    assert result["model_calls"] == 0
    assert {row["player_id"] for row in result["identity"]["matches"]} == {1, 3}
    assert "outcome=clarification_required calls=0" in caplog.text


def test_exact_identity_still_reaches_provider(monkeypatch):
    calls = []
    provider = FixtureProvider(plan())
    original = provider.classify
    monkeypatch.setattr(host, "get_provider", lambda settings: provider)
    monkeypatch.setattr(
        provider,
        "classify",
        lambda context: (calls.append(context), original(context))[1],
    )
    result = host.plan_question(
        None,
        settings=settings(),
        model="synthetic",
        question="Alex Example points per game in the 2024-25 regular season",
        selected_season="2024-25",
        players=PLAYERS,
    )
    assert result["status"] == "query"
    assert len(calls) == 1


def test_shared_alias_clarifies_before_model(monkeypatch):
    players = [
        dict(player_id=1, player_name="Alex Example", aliases=["shared"]),
        dict(player_id=2, player_name="Blair Sample", aliases=["shared"]),
    ]
    provider = FixtureProvider(plan())
    monkeypatch.setattr(host, "get_provider", lambda settings: provider)
    monkeypatch.setattr(
        provider,
        "classify",
        lambda context: pytest.fail("Alias ambiguity reached provider"),
    )
    for question in ("What is shared averaging?", "shared points?"):
        result = host.plan_question(
            None,
            settings=settings(),
            model="synthetic",
            question=question,
            selected_season="2024-25",
            players=players,
        )
        assert result["status"] == "clarification_required"
        assert result["model_calls"] == 0
        assert result["identity"]["status"] == "ambiguous"
        assert len(result["identity"]["matches"]) == 2


@pytest.mark.parametrize("typed", [False, True])
@pytest.mark.parametrize(
    "scope", ["in the 2024-25 regular season", "from 2025-01-01 through 2025-01-07"]
)
def test_shared_alias_choice_resumes_one_model_plan(
    monkeypatch, scripted_intelligence, typed, scope
):
    from app.agent import semantic_answer
    from app.agent.conversation import InMemoryConversationStore
    from app.agent.service import StatsAgent
    from tests.test_semantic_migration import Model, Warehouse

    original_players = semantic_answer.source_players

    def shared_players(evidence):
        players = original_players(evidence)
        for player in players:
            if player["player_id"] in (1, 2):
                player["aliases"] = [*player["aliases"], "shared"]
        return players

    monkeypatch.setattr(semantic_answer, "source_players", shared_players)
    model = Model()
    store = InMemoryConversationStore()
    agent = StatsAgent(
        settings(),
        SimpleNamespace(),
        client=model,
        semantic_warehouse=Warehouse(),
        conversation_store=store,
    )
    question = "shared points per game " + scope
    first = agent.answer(question, conversation_id="alias-resume")
    assert first["status"] == "clarification_required"
    assert not model.calls
    choice = next(p for p in first["clarification_options"] if p["player_id"] == 1)
    final = agent.answer(
        choice["player_name"] if typed else question,
        conversation_id="alias-resume",
        selected_player=None if typed else choice,
    )
    assert final["status"] == "ok", final
    assert final["semantic_plan"]["model_calls"] == 1
    assert len(model.calls) == 1
    assert final["semantic_plan"]["queries"][0]["player_name"] == choice["player_name"]
    assert store.get_pending_clarification("alias-resume") is None


@pytest.mark.parametrize("question", ["", "   ", "Example points? " + "x" * 2000])
def test_question_bounds_precede_identity_scanning(monkeypatch, question):
    from app.agent import name_matching

    monkeypatch.setattr(host, "get_provider", lambda settings: FixtureProvider(plan()))
    monkeypatch.setattr(
        host,
        "_literal_player_resolutions",
        lambda *args: pytest.fail("Invalid question reached literal identity scanning"),
    )
    monkeypatch.setattr(
        name_matching,
        "prepare_names",
        lambda *args, **kwargs: pytest.fail("Invalid question reached fuzzy scanning"),
    )
    with pytest.raises(SemanticError) as error:
        host.plan_question(
            None,
            settings=settings(),
            model="synthetic",
            question=question,
            selected_season="2024-25",
            players=PLAYERS,
        )
    assert error.value.code == "invalid_scope"


def test_candidate_preflight_does_not_weaken_normal_name_validation():
    from app.agent.name_matching import PlayerNameError, prepare_names

    with pytest.raises(PlayerNameError) as error:
        prepare_names("Compare Zqxv Blorp and Alex Example points?", PLAYERS)
    assert error.value.result["status"] == "not_found"
