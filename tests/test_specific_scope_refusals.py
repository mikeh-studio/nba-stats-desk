"""Unsupported constraints explain what was withheld, without a substitute answer."""

from dataclasses import replace

import pytest
from app.agent.reference_ask import parse_reference
from app.agent.scope_policy import question_scope
from app.agent.semantics import SemanticError
from tests.test_ask_harness import agent
from tests.test_league_reference_language import reference_endpoint_pair
from tests.test_semantic_migration import settings


def assert_refusal(payload, route):
    assert payload["status"] == "unsupported_scope"
    assert payload["tables"] == payload["charts"] == []
    assert payload["semantic_evidence"] is None
    assert payload["clarification_options"] == []
    assert payload["ask_route"]["route"] == route


@pytest.mark.parametrize(
    "condition",
    [
        "in wins only",
        "wins-only",
        "only in wins",
        "in victories",
        "in games won",
        "in games his team won",
        "in games their team won",
        "in games the team won",
        "in games they won",
        "in games that his team won",
    ],
)
def test_wins_only_baseline_requires_connected_outcome_evidence(condition):
    a = agent()
    a.client = object()
    payload = a.answer(
        f"Compare Avery Example rebounds against league average {condition} "
        "in the 2024-25 regular season."
    )
    assert payload["status"] == "incomplete_evidence"
    assert payload["semantic_evidence"] is None
    assert payload["tables"] == payload["charts"] == []
    assert payload["semantic_plan"]["model_calls"] == 0


@pytest.mark.parametrize(
    "extra",
    ["His team won a game.", "in games they won't play", "in games they won’t play"],
)
def test_unrelated_win_language_does_not_receive_wins_filter_diagnosis(extra):
    a = agent()
    a.client = object()
    payload = a.answer(
        "Compare Avery Example rebounds against league average in 2024-25 " + extra
    )
    assert_refusal(payload, "league_baseline")
    assert "wins only" not in payload["answer"]


@pytest.mark.parametrize(
    "condition,explicit_split",
    [
        ("in wins and losses", False),
        ("in losses and wins", False),
        ("in both wins and losses", False),
        ("in both losses and wins", False),
        ("in wins versus losses", True),
        ("in losses versus wins", True),
        ("in wins vs losses", True),
    ],
)
def test_both_outcomes_are_not_diagnosed_as_wins_only(condition, explicit_split):
    a = agent()
    a.client = object()
    payload = a.answer(
        f"Compare Avery Example rebounds with league average {condition} in 2024-25"
    )
    assert_refusal(payload, "league_baseline")
    assert "wins only" not in payload["answer"]
    assert (
        "separate" in payload["answer"].lower() or "not supported" in payload["answer"]
    )


@pytest.mark.parametrize(
    "window,venue",
    [
        ("last four road games", "road games"),
        ("last four home games", "home games"),
        ("last 4 away games", "road games"),
        ("last five games on the road", "road games"),
        ("past 5 games at home", "home games"),
        ("last 5 games away", "road games"),
        ("last four road appearances", "road games"),
        ("past 5 appearances at home", "home games"),
        ("on the road over the last five games", "road games"),
        ("at home over the last four games", "home games"),
        ("home games over the last four appearances", "home games"),
        ("at home in the last 5 games on the road", "home or road games"),
        ("last 5 home games and road games", "home or road games"),
    ],
)
@pytest.mark.parametrize("selected_season", ["2024-25", "2025-26"])
def test_recent_similarity_names_the_requested_unsupported_venue(
    window, venue, selected_season
):
    a = agent()
    a.settings = replace(a.settings, season=selected_season)
    a.client = object()
    payload = a.answer(
        f"Who is statistically similar to Avery Example in his {window} in 2024-25?"
    )
    assert_refusal(payload, "similarity")
    assert f"cannot filter appearances to {venue}." in payload["answer"]
    assert "across all venues" in payload["answer"]
    assert payload["semantic_plan"]["model_calls"] == 0


@pytest.mark.parametrize("metric", ["screen assists", "screen-assists"])
@pytest.mark.parametrize(
    "template,route",
    [
        ("How did Avery Example perform in {metric} in 2024-25?", "governed_metrics"),
        (
            "How did Avery Example perform in {metric} in wins only in 2024-25?",
            "governed_metrics",
        ),
        (
            "Compare Avery Example {metric} against league average in 2024-25",
            "league_baseline",
        ),
        (
            "Compare Avery Example {metric} with league average in wins versus losses in 2024-25",
            "league_baseline",
        ),
        (
            "Compare Avery Example {metric} trend with league average in 2024-25",
            "league_baseline",
        ),
    ],
)
def test_screen_assists_explains_the_missing_tracking_metric(metric, template, route):
    a = agent()
    a.client = object()
    payload = a.answer(template.format(metric=metric))
    assert_refusal(payload, route)
    assert question_scope(metric)["kind"] == "unavailable_grain"
    assert (
        "Screen assists require tracking data that is not connected"
        in payload["answer"]
    )
    assert "box-score assists" in payload["answer"]
    assert "cannot substitute" in payload["answer"]


@pytest.mark.parametrize(
    "name",
    ["Home Games", "Road Games", "Wins Only", "Screen Assists", "Last Five Home Games"],
)
def test_constraint_words_in_resolved_names_are_not_filters(name):
    player = {"player_id": 123, "player_name": name, "aliases": []}
    _, _, spec = parse_reference(
        f"Who is similar to {name} in the last five games in 2024-25?",
        [player],
        "2024-25",
        "similarity",
    )
    assert spec["n"] == 5
    _, _, query = parse_reference(
        f"Compare {name} assists against league average in 2024-25",
        [player],
        "2024-25",
        "league_baseline",
    )
    assert query.metric == "ast"
    with pytest.raises(SemanticError, match="publication must match") as exc:
        parse_reference(
            f"Who is similar to {name} in 2024-25?",
            [player],
            "2025-26",
            "similarity",
            enforce_profile_season=True,
        )
    assert exc.value.code == "unsupported_coverage"


def test_plain_out_of_season_profile_refuses_before_loading_facts(monkeypatch):
    a = agent()
    a.settings = replace(a.settings, season="2025-26")
    a.client = object()

    def fail_load(*args, **kwargs):
        pytest.fail("A plainly out-of-season profile must not read warehouse facts")

    warehouse = a.semantic_agent.warehouse
    monkeypatch.setattr(getattr(warehouse, "facts", warehouse), "load", fail_load)
    payload = a.answer("Who is similar to Avery Example in 2024-25?")
    assert payload["status"] == "unsupported_coverage"
    assert "publication must match" in payload["answer"]
    assert payload["tables"] == payload["charts"] == []


@pytest.mark.parametrize(
    "question",
    [
        "Compare Avery Example rebounds with league average in wins versus losses in 2024-25",
        "Compare Avery Example rebounds with league average in losses versus wins in 2024-25",
        "Compare Avery Example rebounds with league average in both losses and wins in 2024-25",
        "Who is similar to Avery Example on the road over the last five games in 2024-25?",
        "Who is similar to Avery Example at home over the last four games in 2024-25?",
        "How did Avery Example perform in screen assists in 2024-25?",
        "Compare Avery Example screen assists with league average in wins versus losses in 2024-25",
    ],
)
def test_constraint_refusals_match_at_json_and_sse_endpoints(monkeypatch, question):
    normal, stream = reference_endpoint_pair(monkeypatch, settings(), question)
    for payload in (normal, stream):
        assert payload["status"] == "unsupported_scope"
        assert payload["tables"] == payload["charts"] == []
        assert payload["semantic_evidence"] is None
    assert normal["answer"] == stream["answer"]
    assert normal["ask_route"] == stream["ask_route"]


def test_nearby_supported_references_and_assists_remain_available():
    a = agent()
    a.client = object()
    for question, kind in [
        (
            "Compare Avery Example rebounds against league average in the 2024-25 regular season",
            "league_baseline",
        ),
        (
            "Who is similar to Avery Example in the last five games in 2024-25?",
            "recent_statistical_similarity",
        ),
    ]:
        payload = a.answer(question)
        assert payload["status"] == "ok"
        assert payload["semantic_evidence"]["kind"] == kind
    assert question_scope("Avery Example assists per game in 2024-25")["status"] is None
