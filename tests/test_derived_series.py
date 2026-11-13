"""Synthetic governed observations: explicit periods and derived appearances."""

from dataclasses import replace
from datetime import date, timedelta

import pytest
from app.agent.derived_series import (
    compute_derived_series,
    derived_series_plan,
    explicit_period,
    period_comparison_plan,
    render_derived_series_answer,
)
from app.agent.plan_execution import execute_plan
from app.agent.semantics import Evidence, Query, SemanticError

PLAYERS = [{"player_id": 1, "player_name": "Alex Example"}]


def evidence(count=6, **changes):
    rows = [
        {
            "season": "2024-25",
            "season_type": "Regular Season",
            "player_id": 1,
            "player_name": "Alex Example",
            "game_id": f"{i:03}",
            "game_date": (date(2024, 11, 1) + timedelta(days=i)).isoformat(),
            "team_abbr": "AAA",
            "opponent_abbr": "BBB",
            "home_away": "HOME",
            "game_result": "W" if i % 2 else "L",
            "pts": i + 1,
            "fgm": 1 if i == 0 else 0,
            "fga": 1 if i == 0 else 9,
            **changes,
        }
        for i in range(count)
    ]
    return Evidence(
        rows,
        frozenset({("2024-25", "Regular Season")}),
        "synthetic",
        "frozen",
        {("2024-25", "Regular Season"): rows[-1]["game_date"]},
        complete=True,
    )


def query(**changes):
    return Query(
        metric="pts", season="2024-25", aggregation="average", player_id=1, **changes
    )


@pytest.mark.parametrize(
    "question,mode,n",
    [
        (
            "Show Alex Example's rolling 3-game points average in the 2024-25 regular season.",
            "rolling",
            3,
        ),
        (
            "Plot Alex Example's points rolling 3 appearance average in the 2024-25 playoffs.",
            "rolling",
            3,
        ),
        (
            "Alex Example cumulative points totals in the last 5 appearances in the 2024-25 regular season",
            "cumulative",
            None,
        ),
        (
            "Alex Example's field goal percentage trend from 2024-11-01 through 2024-11-06 in the 2024-25 regular season",
            "trend",
            None,
        ),
    ],
)
def test_full_series_grammar(question, mode, n):
    plan = derived_series_plan(question, "2024-25", ["Alex Example"])
    assert plan and (plan.mode, plan.rolling_n) == (mode, n)


@pytest.mark.parametrize(
    "question",
    [
        "Alex Example's cumulative field goal percentage in the 2024-25 regular season",
        "Alex Example's rolling 0-game points average in the 2024-25 regular season",
        "Alex Example's rolling 101-game points average in the 2024-25 regular season",
        "Alex Example's rolling 3-game points average in the 2024-25 regular season only fourth quarters",
        "Alex Example's points trend in the 2024-25 regular season compared with the league",
        "Unknown Player points trend in the 2024-25 regular season",
        "Alex Example's points and rebounds trend in the 2024-25 regular season",
    ],
)
def test_unknown_or_unrepresentable_series_is_not_dropped(question):
    assert derived_series_plan(question, "2024-25", ["Alex Example"]) is None


def test_rolling_uses_full_observations_before_display_cap_and_has_explicit_warmup():
    result = compute_derived_series(evidence(110), query(limit=2), "rolling", 3)
    assert result["observed_games"] == 110
    assert result["displayed_games"] == 2
    assert result["warmup_games"] == 2
    assert [row["value"] for row in result["rows"]] == [108, 109]
    first = compute_derived_series(evidence(), query(), "rolling", 3)["rows"]
    assert [row["value"] for row in first] == [None, None, 2, 3, 4, 5]
    assert all(row["warmup"] for row in first[:2])


def test_rolling_ratios_pool_makes_attempts_and_zero_denominator_is_unknown():
    result = compute_derived_series(
        evidence(2),
        replace(query(), metric="fg_pct", aggregation="ratio"),
        "rolling",
        2,
    )
    row = result["rows"][-1]
    assert (
        row["numerator"],
        row["denominator"],
        row["value"],
        row["display_value"],
    ) == (1, 10, 0.1, 10)
    empty_attempts = compute_derived_series(
        evidence(2, fgm=0, fga=0),
        replace(query(), metric="fg_pct", aggregation="ratio"),
        "rolling",
        2,
    )
    assert empty_attempts["rows"][-1]["value"] is None


def test_nulls_remain_unknown_and_rolling_windows_count_selected_appearances():
    source = evidence(3)
    source.rows[1]["pts"] = None
    result = compute_derived_series(source, query(), "rolling", 2)
    assert [r["value"] for r in result["rows"]] == [None, 1, 3]
    assert result["rows"][-1]["observed_games"] == 2
    assert result["rows"][-1]["valid_games"] == 1
    assert result["rows"][-1]["missing_component_games"] == 1
    assert result["rows"][-1]["status"] == "partial"


def test_cumulative_total_starts_at_full_selected_prefix_and_never_zero_fills():
    source = evidence(110)
    source.rows[0]["pts"] = None
    result = compute_derived_series(
        source, replace(query(limit=2), aggregation="total"), "cumulative"
    )
    assert result["rows"][-1]["value"] == sum(range(2, 111))
    assert result["rows"][-1]["missing_component_games"] == 1
    assert result["rows"][-1]["valid_games"] == 109
    assert result["rows"][-1]["status"] == "partial"
    unknown = compute_derived_series(
        evidence(2, pts=None), replace(query(), aggregation="total"), "cumulative"
    )
    assert all(row["value"] is None for row in unknown["rows"])


def test_series_selection_reuses_filters_before_appearance_window():
    scoped = query(window="last_n_games", n=2, game_result="W")
    result = compute_derived_series(evidence(), scoped, "rolling", 2)
    assert result["observed_games"] == 2
    assert [row["game_id"] for row in result["rows"]] == ["003", "005"]
    assert result["rows"][-1]["value"] == 5
    assert result["scope"]["game_result"] == "W"


def test_series_invalid_evidence_and_predicate_missingness_fail_closed():
    with pytest.raises(SemanticError, match="Complete scope"):
        compute_derived_series(replace(evidence(), complete=False), query(), "trend")
    source = evidence()
    source.rows[0]["game_result"] = None
    with pytest.raises(SemanticError) as error:
        compute_derived_series(source, query(game_result="W"), "trend")
    assert error.value.code == "incomplete_evidence"


def test_render_has_existing_chart_envelope_and_discloses_partial_totals():
    plan = derived_series_plan(
        "Show Alex Example's cumulative points in the 2024-25 regular season",
        "2024-25",
        ["Alex Example"],
    )
    payload = render_derived_series_answer(plan, evidence(), PLAYERS)
    assert payload["status"] == "ok"
    assert payload["charts"][0]["type"] == "line"
    assert payload["charts"][0]["series"][0]["points"][-1]["y"] == 21
    source = evidence()
    source.rows[2]["pts"] = None
    partial = render_derived_series_answer(plan, source, PLAYERS)
    assert not partial["charts"]
    assert any("Partial totals" in text for text in partial["assumptions"])
    assert "Cumulative known-component total" in partial["answer"]


def test_one_observation_is_not_a_trend():
    plan = derived_series_plan(
        "Alex Example points trend in the 2024-25 regular season",
        "2024-25",
        ["Alex Example"],
    )
    payload = render_derived_series_answer(plan, evidence(1), PLAYERS)
    assert payload["status"] == "unavailable"
    assert "At least two" in payload["answer"]
    assert not payload["charts"]
    assert not payload["tables"]


@pytest.mark.parametrize(
    "right,window",
    [
        ("last 2 games in the 2024-25 regular season", "last_n_games"),
        (
            "from 2024-11-01 through 2024-11-02 in the 2024-25 regular season",
            "date_range",
        ),
    ],
)
def test_explicit_period_comparison_uses_existing_executor(right, window):
    plan = period_comparison_plan(
        "Compare Alex Example's points per game in the 2024-25 regular season versus "
        + right,
        "2024-25",
        ["Alex Example"],
    )
    assert plan["queries"][0].get("window", "season_to_date") == "season_to_date"
    assert plan["queries"][1]["window"] == window
    result = execute_plan(plan, evidence(), PLAYERS)
    assert result["status"] == "ok"
    assert result["evidence"]["current"]["rows"][0]["value"] == 3.5
    assert result["evidence"]["baseline"]["rows"][0]["value"] == (
        5.5 if window == "last_n_games" else 1.5
    )


def test_comparison_never_swallows_unknown_operand_or_loses_aggregation():
    assert (
        period_comparison_plan(
            "Compare Alex Example points in the 2024-25 regular season versus last 2 games",
            "2024-25",
            ["Alex Example"],
        )
        is None
    )
    assert explicit_period("last 2 games with a teammate absent", "2024-25") is None
    assert explicit_period("last 101 games", "2024-25") is None


@pytest.mark.parametrize(
    "phrase,key",
    [
        ("offensive rebounds", "oreb"),
        ("oreb", "oreb"),
        ("defensive rebounds", "dreb"),
        ("dreb", "dreb"),
        ("personal fouls", "pf"),
        ("pf", "pf"),
    ],
)
def test_reference_scalar_added_source_metrics_keep_full_metric_name(phrase, key):
    from tests.test_reference_intelligence import agent, source

    warehouse = source()
    for row in warehouse.rows:
        row.update(oreb=2, dreb=3, pf=4)
    payload = agent(warehouse).answer(f"Alex Example {phrase} per game in 2024-25")
    assert payload["status"] == "ok", payload
    assert payload["query_plan"]["queries"][0]["metric"] == key
    assert (
        payload["semantic_evidence"]["rows"][0]["value"]
        == {"oreb": 2, "dreb": 3, "pf": 4}[key]
    )


def test_date_operands_never_clip_to_an_incompatible_or_single_selected_archive():
    assert (
        explicit_period("from 2024-11-01 through 2024-11-06", "2025-26")["season"]
        == "2024-25"
    )
    assert (
        explicit_period(
            "from 2024-11-01 through 2024-11-06 in the 2025-26 regular season",
            "2025-26",
        )
        is None
    )
    assert explicit_period("from 2024-06-01 through 2024-11-01", "2024-25") is None
    assert explicit_period("3 games", "2024-25") is None


@pytest.mark.parametrize(
    "question,mode",
    [
        (
            "Show Alex Example's rolling 3-game points average in the 2024-25 regular season",
            "rolling",
        ),
        (
            "Show Alex Example's cumulative points in the 2024-25 regular season",
            "cumulative",
        ),
        ("Show Alex Example's points trend in the 2024-25 regular season", "trend"),
        (
            "Show Alex Example's rolling 3-game field goal percentage average in the 2024-25 regular season",
            "rolling",
        ),
    ],
)
def test_series_is_reachable_through_governed_entrypoint(question, mode):
    from app.agent.observability import AgentTrace
    from tests.test_reference_intelligence import agent

    trace = AgentTrace("derived-series", "", "fixture")
    payload = agent().answer(question, trace=trace)
    assert payload["status"] == "ok", payload
    assert payload["semantic_evidence"]["schema_version"] == "derived_series/1"
    assert payload["semantic_evidence"]["mode"] == mode
    assert payload["semantic_evidence"]["observed_games"] == 7
    assert payload["tables"]
    assert payload["charts"]
    assert trace.model_calls == 0


def test_series_filters_are_reachable_and_retained_in_execution():
    from tests.test_reference_intelligence import agent, source

    warehouse = source()
    for row in warehouse.rows:
        row["game_result"] = "W" if int(row["game_id"]) % 2 else "L"
    payload = agent(warehouse).answer(
        "Alex Example's rolling 2-game points average in the 2024-25 regular season in wins"
    )
    assert payload["status"] == "ok", payload
    result = payload["semantic_evidence"]
    assert result["scope"]["game_result"] == "W"
    assert result["observed_games"] == 4
    assert all(int(row["game_id"]) % 2 for row in result["rows"])


@pytest.mark.parametrize(
    "question",
    [
        "Alex Example's rolling 3-game points average in the 2024-25 regular season",
        "Alex Example's cumulative points in the 2024-25 regular season",
        "Alex Example's points trend in the 2024-25 regular season",
        "Compare Alex Example's points per game from 2025-01-01 through 2025-01-03 in the 2024-25 regular season versus from 2025-01-04 through 2025-01-06 in the 2024-25 regular season",
    ],
)
def test_derived_series_and_explicit_period_json_sse_parity(question):
    import json
    from types import SimpleNamespace

    from app.main import app, get_agent_client, get_repository, get_settings
    from app.repository import BigQueryWarehouseRepository
    from fastapi.testclient import TestClient
    from tests.test_reference_intelligence import config, source

    repo = BigQueryWarehouseRepository(config(), client=SimpleNamespace())
    repo._governed_warehouse = source()
    previous = dict(app.dependency_overrides)
    app.dependency_overrides.update(
        {
            get_settings: config,
            get_repository: lambda: repo,
            get_agent_client: lambda: None,
        }
    )
    try:
        with TestClient(app) as client:
            normal = client.post(
                "/api/agent/ask?season=2024-25", json={"question": question}
            )
            stream = client.post(
                "/api/agent/ask/stream?season=2024-25", json={"question": question}
            )
        assert normal.status_code == stream.status_code == 200
        ordinary = normal.json()
        assert ordinary["status"] == "ok", ordinary
        final = next(
            json.loads(line[6:])["payload"]
            for line in stream.text.splitlines()
            if line.startswith("data: ") and json.loads(line[6:]).get("type") == "final"
        )
        for field in (
            "status",
            "answer",
            "tables",
            "charts",
            "semantic_evidence",
            "query_plan",
            "ask_route",
        ):
            assert ordinary[field] == final[field]
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


@pytest.mark.parametrize("transport", ["json", "sse"])
@pytest.mark.parametrize(
    "series,mode,n",
    [
        ("rolling 2-game assists average", "rolling", 2),
        ("cumulative assists", "cumulative", None),
        ("assists trend", "trend", None),
    ],
)
def test_series_current_identity_and_mode_replace_previous_scalar_context(
    monkeypatch, transport, series, mode, n
):
    from app import main
    from tests.test_reference_followup_aggregation import (
        endpoint_client,
        post,
        settings,
    )

    client, stores, previous = endpoint_client(monkeypatch, settings())
    try:
        with client:
            initial = post(
                client,
                transport,
                "Blair Sample assists per game in 2024-25",
                "series-identity",
            )
            current = post(
                client,
                transport,
                f"Show Avery Example {series} in the 2024-25 regular season with at least 4 points",
                "series-identity",
            )
            saved = (
                stores["value"].get_turns("series-identity", max_turns=5)[-1].context
            )
            followup = post(
                client, transport, "What about rebounds per game?", "series-identity"
            )
        assert initial["status"] == current["status"] == "ok", current
        assert current["semantic_evidence"]["scope"]["player_id"] == 101
        assert current["player_profile"]["player"]["player_id"] == 101
        assert saved["players"] == [{"player_id": 101, "player_name": "Avery Example"}]
        assert saved["analysis_type"] == "derived_series"
        assert saved["series_question"]["mode"] == mode
        assert saved["series_question"]["rolling_n"] == n
        assert saved["series_question"]["query"]["stat_filters"] == [
            {"component": "pts", "op": "gte", "value": 4.0}
        ]
        if mode == "cumulative":
            assert followup["status"] == "clarification_required", followup
            assert not followup.get("tables") and not followup.get("charts")
            assert "series" in followup["answer"].casefold()
        else:
            assert followup["status"] == "ok", followup
            series_evidence = followup["semantic_evidence"]
            assert series_evidence["mode"] == mode
            assert series_evidence["rolling_n"] == n
            assert series_evidence["scope"]["player_id"] == 101
            assert series_evidence["metric"]["key"] == "reb"
            assert (
                series_evidence["scope"]["stat_filters"]
                == saved["series_question"]["query"]["stat_filters"]
            )
    finally:
        main.app.dependency_overrides.clear()
        main.app.dependency_overrides.update(previous)


@pytest.mark.parametrize("transport", ["json", "sse"])
@pytest.mark.parametrize(
    "series,mode,n",
    [
        ("rolling 2-game assists average", "rolling", 2),
        ("cumulative assists", "cumulative", None),
        ("assists trend", "trend", None),
    ],
)
def test_metric_only_followup_recomputes_the_current_series_in_its_validated_scope(
    monkeypatch, transport, series, mode, n
):
    from app import main
    from tests.test_reference_followup_aggregation import (
        endpoint_client,
        post,
        settings,
    )

    client, stores, previous = endpoint_client(monkeypatch, settings())
    try:
        with client:
            initial = post(
                client,
                transport,
                "Blair Sample assists per game in 2024-25",
                "series-metric",
            )
            current = post(
                client,
                transport,
                f"Show Avery Example {series} in the 2024-25 regular season with at least 4 points",
                "series-metric",
            )
            followup = post(client, transport, "What about rebounds?", "series-metric")
            saved = stores["value"].get_turns("series-metric", max_turns=5)[-1].context
            percentage = post(
                client, transport, "What about field goal percentage?", "series-metric"
            )
        assert initial["status"] == current["status"] == followup["status"] == "ok", (
            followup
        )
        result = followup["semantic_evidence"]
        assert (
            result["mode"],
            result["rolling_n"],
            result["scope"]["player_id"],
            result["metric"]["key"],
        ) == (mode, n, 101, "reb")
        assert result["scope"]["stat_filters"] == [
            {"component": "pts", "op": "gte", "value": 4.0}
        ]
        assert (
            result["scope"]["as_of"] == current["semantic_evidence"]["scope"]["as_of"]
        )
        assert result["observed_games"] == 4
        assert result["rows"][-1]["value"] == (16 if mode == "cumulative" else 4)
        assert saved["players"] == [{"player_id": 101, "player_name": "Avery Example"}]
        assert saved["series_question"]["mode"] == mode
        assert saved["series_question"]["rolling_n"] == n
        if mode == "cumulative":
            assert percentage["status"] == "clarification_required", percentage
            assert not percentage["tables"] and not percentage["charts"]
        else:
            assert percentage["status"] == "ok", percentage
            assert percentage["semantic_evidence"]["metric"]["key"] == "fg_pct"
            assert percentage["semantic_evidence"]["mode"] == mode
            assert percentage["semantic_evidence"]["rolling_n"] == n
            assert percentage["semantic_evidence"]["scope"]["aggregation"] == "ratio"
    finally:
        main.app.dependency_overrides.clear()
        main.app.dependency_overrides.update(previous)
