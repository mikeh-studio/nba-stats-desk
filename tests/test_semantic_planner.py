import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from app.agent.semantic_planner import execute_plan, plan_question, plan_schema
from app.agent.semantics import Evidence, SemanticError
from scripts.evaluate_semantic_language import mismatches, run_case

SOURCE = Path(__file__).parent / "fixtures/semantics/cases.json"


def client_for(plan):
    def create(**kwargs):
        assert kwargs["store"] is False
        assert kwargs["text"]["format"]["strict"] is True
        return SimpleNamespace(output_text=json.dumps(plan))

    return SimpleNamespace(responses=SimpleNamespace(create=create))


def query_plan():
    return {
        "status": "query",
        "message": "",
        "queries": [
            {
                "metric": "pts",
                "season": "2024-25",
                "aggregation": "average",
                "player_name": "resolved_player_1",
            }
        ],
    }


def test_recognized_entities_reach_planner_and_execute():
    source = json.loads(SOURCE.read_text())
    plan = plan_question(
        client_for(query_plan()),
        model="test",
        question="Regular Player points per game?",
        selected_season="2024-25",
        players=source["players"],
    )
    evidence = Evidence(
        source["rows"],
        frozenset((s, p) for s, p, _ in source["coverage"]),
        "fixture",
        "test",
        {(s, p): d for s, p, d in source["coverage"]},
        complete=True,
    )
    result = execute_plan(plan, evidence, source["players"])
    assert result["evidence"]["rows"][0]["value"] == 10


def test_ambiguous_alias_withholds_before_model_and_preserves_candidates():
    source = json.loads(SOURCE.read_text())
    plan = plan_question(
        None,
        model="test",
        question="What is shared averaging?",
        selected_season="2024-25",
        players=source["players"],
    )
    assert plan["model_calls"] == 0
    result = execute_plan(plan, None, source["players"])
    assert result["status"] == "clarification_required"
    assert len(result["identity"]["matches"]) == 2


@pytest.mark.parametrize(
    "plan",
    [
        {},
        {"status": "query", "queries": [], "message": ""},
        {"status": "raw_sql", "queries": [], "message": ""},
        {"status": "unsupported", "queries": [{}], "message": ""},
    ],
)
def test_invalid_model_plan_is_blocked(plan):
    with pytest.raises(SemanticError):
        plan_question(
            client_for(plan),
            model="test",
            question="points?",
            selected_season="2024-25",
        )


def test_scope_schema_restricts_window_and_limit():
    fields = plan_schema()["properties"]["queries"]["items"]["properties"]
    assert "season_to_date" in fields["window"]["enum"]
    assert "full season" not in fields["window"]["enum"]
    assert fields["limit"]["minimum"] == 1


def test_grader_does_not_pass_wrong_scope_or_zero_for_missing():
    assert mismatches(
        {"scope": {"season": "2025-26"}, "value": 0},
        {"scope.season": "2024-25", "value": None},
    ) == ["scope.season", "value"]


def test_harness_counts_calls_and_handles_zero_call_ambiguity():
    source = json.loads(SOURCE.read_text())
    case = {
        "id": "ambiguous",
        "questions": ["shared points?"],
        "expected": {
            "status": "clarification_required",
            "identity.status": "ambiguous",
        },
    }
    result = run_case(None, "test", {"selected_season": "2024-25"}, source, case, 0, 0)
    assert result["passed"] and result["model_calls"] == 0
    case = {
        "id": "query",
        "questions": ["Regular Player PPG?"],
        "expected": {"status": "ok", "evidence.rows.0.value": 10},
    }
    result = run_case(
        client_for(query_plan()),
        "test",
        {"selected_season": "2024-25"},
        source,
        case,
        0,
        0,
    )
    assert result["passed"] and result["model_calls"] == 1


def test_missing_shooting_qualification_is_explicit_even_with_a_bad_optional_window_count():
    source = json.loads(SOURCE.read_text())
    plan = {
        "status": "query",
        "queries": [
            {
                "metric": "ts_pct",
                "season": "2024-25",
                "aggregation": "ratio",
                "operation": "rank",
                "window": "season_to_date",
                "n": 10,
            }
        ],
        "message": "",
    }
    result = execute_plan(plan, None, source["players"])
    assert result["status"] == "clarification_required"
    assert result["message"] == "Specify an attempt threshold"


def test_fantasy_default_is_supplied_to_model():
    def create(**kwargs):
        payload = json.loads(kwargs["input"][0]["content"])
        assert payload["default_fantasy_metric"] == "fantasy_proxy_weighted"
        assert "defaults to fantasy_proxy_weighted" in kwargs["instructions"]
        plan = query_plan()
        plan["queries"][0]["metric"] = "fantasy_proxy_weighted"
        return SimpleNamespace(output_text=json.dumps(plan))

    plan = plan_question(
        SimpleNamespace(responses=SimpleNamespace(create=create)),
        model="test",
        question="Average Fantasy Score for Regular Player?",
        selected_season="2024-25",
    )
    assert plan["queries"][0]["metric"] == "fantasy_proxy_weighted"


def test_literal_date_range_binds_both_endpoints_over_model_omission():
    from app.agent.semantic_planner import explicit_scope

    assert explicit_scope("from 2025-02-01 through 2025-02-14") == {
        "window": "date_range",
        "start_date": "2025-02-01",
        "as_of": "2025-02-14",
        "n": None,
    }
    plan = plan_question(
        client_for(query_plan()),
        model="test",
        question="Regular Player points from 2025-02-01 through 2025-02-14",
        selected_season="2024-25",
    )
    assert plan["queries"][0]["as_of"] == "2025-02-14"
    with pytest.raises(SemanticError):
        explicit_scope("from 2025-02-14 to 2025-02-01")


def test_source_name_is_replaced_by_bounded_reference_before_model():
    source = json.loads(SOURCE.read_text())

    def create(**kwargs):
        payload = json.loads(kwargs["input"][0]["content"])
        assert "Small Sample" not in kwargs["input"][0]["content"]
        assert "resolved_player_2" in payload["question"]
        allowed = kwargs["text"]["format"]["schema"]["properties"]["plan"]["anyOf"][0][
            "properties"
        ]["queries"]["items"]["properties"]["player_name"]["enum"]
        assert allowed == ["resolved_player_2", None]
        plan = query_plan()
        plan["queries"][0]["player_name"] = "resolved_player_2"
        return SimpleNamespace(output_text=json.dumps(plan))

    plan = plan_question(
        SimpleNamespace(responses=SimpleNamespace(create=create)),
        model="test",
        question="Small Sample points?",
        selected_season="2024-25",
        players=source["players"],
    )
    assert plan["queries"][0]["player_name"] == "Small Sample"


def test_named_team_casing_survives_identity_replacement():
    source = json.loads(SOURCE.read_text())

    def create(**kwargs):
        assert "team AAA" in json.loads(kwargs["input"][0]["content"])["question"]
        fields = kwargs["text"]["format"]["schema"]["properties"]["plan"]["anyOf"][0][
            "properties"
        ]["queries"]["items"]["properties"]
        assert fields["team_abbr"]["enum"] == ["AAA", None]
        return SimpleNamespace(output_text=json.dumps(query_plan()))

    plan_question(
        SimpleNamespace(responses=SimpleNamespace(create=create)),
        model="test",
        question="Regular Player points for team AAA",
        selected_season="2024-25",
        players=source["players"],
        teams=["AAA"],
    )


def test_model_schema_binds_query_count_to_status():
    from app.agent.semantic_planner import response_schema

    variants = response_schema(plan_schema())["properties"]["plan"]["anyOf"]
    for variant, expected in zip(variants, (1, 2, 0, 0)):
        assert variant["properties"]["queries"]["minItems"] == expected
        assert variant["properties"]["queries"]["maxItems"] == expected


@pytest.mark.parametrize(
    "question",
    [
        "Rank players by true shooting percentage",
        "Who leads the league in TS%?",
        "Show top ten FG% players",
        "FT% percentile for a player",
    ],
)
def test_missing_attempt_policy_is_deterministic(question):
    plan = plan_question(
        None, model="test", question=question, selected_season="2024-25"
    )
    assert plan["status"] == "clarification_required" and plan["model_calls"] == 0


@pytest.mark.parametrize(
    "question,expected",
    [
        (
            "Compare players from 2025-01-01 through 2025-01-07",
            {
                "window": "date_range",
                "start_date": "2025-01-01",
                "as_of": "2025-01-07",
                "n": None,
            },
        ),
        ("Compare players in the last 7 days", {"window": "last_n_days", "n": 7}),
        (
            "Compare players including regular season and playoffs",
            {"season_type": "Both"},
        ),
    ],
)
def test_shared_explicit_scope_overrides_both_comparison_sides(question, expected):
    raw = {
        "status": "compare",
        "message": "",
        "queries": [
            {"window": "season_to_date", "season_type": "Regular Season"},
            {"window": "season_to_date", "season_type": "Playoffs"},
        ],
    }
    result = plan_question(
        client_for(raw), model="test", question=question, selected_season="2024-25"
    )
    for query in result["queries"]:
        assert all(query[k] == v for k, v in expected.items())


def test_distinct_comparison_ranges_are_not_silently_overwritten():
    raw = {"status": "compare", "message": "", "queries": [{}, {}]}
    result = plan_question(
        client_for(raw),
        model="test",
        question="Compare points from 2025-01-01 through 2025-01-07 versus from 2025-02-01 through 2025-02-07",
        selected_season="2024-25",
    )
    assert result["status"] == "clarification_required"
    assert result["queries"] == []


def test_past_twelve_months_repairs_missing_model_window_count():
    raw = query_plan()
    raw["queries"][0].update(window="last_n_days", n=None)
    plan = plan_question(
        client_for(raw),
        model="test",
        question="Tell me how Jalen Johnson has been performing the past 12 months",
        selected_season="2025-26",
    )
    assert plan["queries"][0]["window"] == "last_n_months"
    assert plan["queries"][0]["n"] == 12


@pytest.mark.parametrize(
    "baseline", ["last 3 months", "last 5 games", "previous 12 months"]
)
def test_month_comparisons_do_not_overwrite_distinct_windows(baseline):
    raw = {"status": "compare", "message": "", "queries": [{}, {}]}
    plan = plan_question(
        client_for(raw),
        model="test",
        question=f"Compare points in the past 12 months versus {baseline}",
        selected_season="2025-26",
    )
    assert plan["status"] == "clarification_required"
