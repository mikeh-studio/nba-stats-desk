"""League comparison wording preserves the existing evidence and scope contract."""

from copy import deepcopy
from dataclasses import replace
from datetime import date

import pytest
from app.agent.conversation import InMemoryConversationStore
from app.agent.observability import AgentTrace
from app.agent.service import StatsAgent
from fastapi.testclient import TestClient
from scripts.ask_eval.fixtures import frozen_repository
from tests.test_ask_harness import SOURCE
from tests.test_reference_followup_aggregation import post
from tests.test_reference_intelligence import config


def service(source=None):
    return StatsAgent(
        config(),
        frozen_repository(config(), source or SOURCE, "complete"),
        client=object(),  # Any model access fails, including visualization.
    )


def reference_endpoint_pair(monkeypatch, settings, question):
    from app.main import app, get_agent_client, get_repository, get_settings

    repo = frozen_repository(settings, SOURCE, "complete")
    monkeypatch.setitem(app.dependency_overrides, get_settings, lambda: settings)
    monkeypatch.setitem(app.dependency_overrides, get_repository, lambda: repo)
    monkeypatch.setitem(app.dependency_overrides, get_agent_client, lambda: object())
    with TestClient(app) as client:
        return (
            post(client, "json", question, None, request_season=settings.season),
            post(client, "sse", question, None, request_season=settings.season),
        )


@pytest.mark.parametrize("connector", ["in", "for", "during", "over"])
def test_season_connectors_preserve_league_comparison(connector):
    trace = AgentTrace("league-language", "", "fixture")
    result = service().answer(
        "How did Avery Example compare with the league average in assists per game "
        f"{connector} the 2024-25 regular season?",
        trace=trace,
    )
    assert result["status"] == "ok", result
    assert result["ask_route"]["route"] == "league_baseline"
    assert trace.model_calls == 0
    evidence = result["semantic_evidence"]
    assert evidence["contract_version"] == "league_baseline/2"
    assert evidence["player_id"] == 101
    assert evidence["metric"]["key"] == "ast"
    assert evidence["scope"]["season"] == "2024-25"
    assert evidence["scope"]["season_type"] == "Regular Season"
    assert evidence["scope"]["aggregation"] == "average"
    assert evidence["player"]["observed_games"] == 6
    assert evidence["player"]["value"] == 2.5
    assert evidence["baseline"]["observed_games"] == 18
    assert evidence["baseline"]["value"] == pytest.approx(69 / 18)
    assert evidence["difference"] == pytest.approx(2.5 - 69 / 18)
    assert evidence["baseline_scope"]["weighting"] == "player_appearance"
    assert evidence["membership"]["count"] == 18


@pytest.mark.parametrize(
    "scope,expected_games,expected_count,start,end",
    [
        ("over the last 2 games", 2, 6, "2025-04-14", "2025-04-15"),
        ("from 2025-04-11 through 2025-04-13", 3, 9, "2025-04-11", "2025-04-13"),
    ],
)
def test_temporal_connectors_do_not_expand_selected_appearances(
    scope, expected_games, expected_count, start, end
):
    result = service().answer(
        f"Compare Avery Example assists per game {scope} "
        "against the league average during the 2024-25 regular season."
    )
    assert result["status"] == "ok", result
    evidence = result["semantic_evidence"]
    assert evidence["player"]["observed_games"] == expected_games
    assert evidence["membership"]["count"] == expected_count
    assert evidence["baseline_scope"]["start_date"] == start
    assert evidence["baseline_scope"]["end_date"] == end


@pytest.mark.parametrize(
    "modifier", ["among centers", "per 100 possessions", "in total"]
)
def test_new_connectors_do_not_erase_unsupported_constraints(modifier):
    result = service().answer(
        "Compare Avery Example assists per game against league average "
        f"during the 2024-25 regular season {modifier}."
    )
    assert result["status"] != "ok"
    assert result["tables"] == result["charts"] == []


@pytest.mark.parametrize("connector", ["during", "over"])
@pytest.mark.parametrize("scope", ["", "in the 2024-25 regular season "])
def test_dangling_temporal_connectors_do_not_invent_a_period(connector, scope):
    result = service().answer(
        "Compare Avery Example assists per game against league average "
        f"{scope}{connector}."
    )
    assert result["status"] == "unsupported_scope"
    assert result["tables"] == result["charts"] == []


def test_new_language_keeps_incomplete_components_unavailable():
    source = deepcopy(SOURCE)
    next(row for row in source["rows"] if row["player_id"] == 202)["ast"] = None
    result = service(source).answer(
        "How did Avery Example compare with league average in assists per game "
        "during the 2024-25 regular season?"
    )
    evidence = result["semantic_evidence"]
    assert result["status"] == "ok"
    assert result["answerability"] == "partial"
    assert evidence["player"]["value"] == 2.5
    assert evidence["difference"] is None
    assert evidence["membership"]["count"] == 18
    assert evidence["baseline"]["observed_games"] == 18
    assert evidence["baseline"]["valid_games"] == 17
    assert result["charts"] == []


@pytest.mark.parametrize(
    "question,status,n",
    [
        (
            "How did Avery Example compare with the league average in assists per game during the 2024-25 regular season?",
            "ok",
            None,
        ),
        (
            "Compare the league average in assists with Avery Example during the 2024-25 regular season.",
            "ok",
            None,
        ),
        (
            "Compare Avery Example assists over the last 2 games against league baseline in 2024-25.",
            "ok",
            2,
        ),
        (
            "Compare Avery Example assists from 2025-04-11 to 2025-04-13 against league average during the 2024-25 regular season.",
            "ok",
            None,
        ),
        (
            "Compare Avery Example recent assists against league average in 2024-25.",
            "ok",
            10,
        ),
        (
            "Compare Avery Example assists during the 2024-25 regular season versus the last 2 games against league average.",
            "unsupported_scope",
            None,
        ),
        (
            "Compare Avery Example assists during the last 2 games versus the 2024-25 regular season against league average.",
            "unsupported_scope",
            None,
        ),
        (
            "Compare Avery Example assists during the 2024-25 regular season versus the last 2 games with the league average.",
            "unsupported_scope",
            None,
        ),
        (
            "Compare Avery Example assists during the 2024-25 regular season versus the last 2 games, league average.",
            "unsupported_scope",
            None,
        ),
        (
            "Compare Avery Example assists during the 2024-25 regular season with the league average over the last 2 games.",
            "ok",
            None,
        ),
        (
            "Compare the league average over the last 2 games with Avery Example assists during the 2024-25 regular season.",
            "ok",
            None,
        ),
        (
            "Compare Avery Example assists in the last 2 games against league average in the last 3 games during 2024-25.",
            "unsupported_scope",
            None,
        ),
        (
            "How did Avery Example assists trend versus the league average during the 2024-25 regular season?",
            "unsupported_scope",
            None,
        ),
        (
            "Show Avery Example's recent scoring trend against the league average.",
            "unsupported_scope",
            None,
        ),
        (
            "Compare Avery Example assists against league average during the recent season.",
            "ok",
            None,
        ),
        (
            "Compare Avery Example assists against league average during the most recent regular season.",
            "ok",
            None,
        ),
        (
            "Compare Avery Example assists against league average during the recent playoffs.",
            "clarification_required",
            None,
        ),
        (
            "Compare Avery Example assists against league average during the recent postseason.",
            "clarification_required",
            None,
        ),
    ],
)
def test_comparison_shape_json_sse_parity_without_model(
    monkeypatch, question, status, n
):
    monkeypatch.setattr(
        "app.agent.time_defaults.semantic_today", lambda: date(2025, 8, 1)
    )
    payload, final = reference_endpoint_pair(monkeypatch, config(), question)
    assert payload["status"] == status, payload
    for field in ("status", "tables", "charts", "semantic_evidence", "ask_route"):
        assert payload.get(field) == final.get(field)
    if status == "ok":
        evidence = payload["semantic_evidence"]
        assert evidence["player_id"] == 101
        assert evidence["metric"]["key"] == "ast"
        assert evidence["scope"]["season"] == "2024-25"
        assert evidence["scope"]["season_type"] == "Regular Season"
        assert evidence["scope"]["n"] == n
        if n == 2:
            assert evidence["player"]["game_ids"] == ["g5", "g4"]
    else:
        assert payload["tables"] == payload["charts"] == []
        for field in ("semantic_evidence", "reference_evidence", "query_plan"):
            assert not payload.get(field)
        assert payload["clarification_options"] == []


def test_ambiguous_period_has_no_identity_pending_and_accepts_full_restatement():
    agent = service()
    agent.conversation_store = InMemoryConversationStore()
    result = agent.answer(
        "Compare Avery Example assists against league average during the recent playoffs.",
        conversation_id="season-clarification",
    )
    assert result["status"] == "clarification_required"
    assert "Restate the full comparison" in result["answer"]
    assert result["clarification_options"] == []
    assert (
        agent.conversation_store.get_pending_clarification("season-clarification")
        is None
    )
    corrected = agent.answer(
        "Compare Avery Example assists against league average during the 2024-25 regular season.",
        conversation_id="season-clarification",
    )
    assert corrected["status"] == "ok", corrected
    assert corrected["semantic_evidence"]["scope"]["window"] == "season_to_date"
    assert corrected["semantic_evidence"]["player"]["value"] == 2.5


@pytest.mark.parametrize(
    "question,date_range",
    [
        (
            "Compared to the league average, how did Avery Example perform in assists per game in the 2024-25 regular season?",
            False,
        ),
        (
            "Against the league average, how did Avery Example perform in assists per game in the 2024-25 regular season?",
            False,
        ),
        (
            "Compared with the league average how did Avery Example perform in assists per game in the 2024-25 regular season?",
            False,
        ),
        (
            "Against the league average, compare Avery Example assists in 2024-25.",
            False,
        ),
        (
            "In the 2024-25 regular season, compare Avery Example assists with the league average from 2025-04-11 to 2025-04-13.",
            True,
        ),
        (
            "In the 2024-25 regular season compare the league average from 2025-04-11 through 2025-04-13 with Avery Example assists.",
            True,
        ),
        (
            "During the regular season in 2024-25, compare the league average with Avery Example assists from 2025-04-11 to 2025-04-13.",
            True,
        ),
        (
            "In the 2024-25 regular season, against league average how did Avery Example perform in assists per game?",
            False,
        ),
    ],
)
def test_global_periods_and_preposed_baseline_complements_preserve_evidence(
    monkeypatch, question, date_range
):
    payload, final = reference_endpoint_pair(monkeypatch, config(), question)
    assert payload["status"] == final["status"] == "ok", payload
    evidence = payload["semantic_evidence"]
    assert evidence == final["semantic_evidence"]
    assert evidence["player_id"] == 101
    assert evidence["metric"]["key"] == "ast"
    assert evidence["scope"]["season"] == "2024-25"
    assert evidence["scope"]["season_type"] == "Regular Season"
    assert evidence["scope"]["window"] == (
        "date_range" if date_range else "season_to_date"
    )
    assert evidence["player"]["value"] == (2 if date_range else 2.5)
    assert evidence["baseline"]["value"] == pytest.approx(
        30 / 9 if date_range else 69 / 18
    )
    assert evidence["membership"]["count"] == (9 if date_range else 18)
    if date_range:
        assert evidence["scope"]["start_date"] == "2025-04-11"
        assert evidence["scope"]["as_of"] == "2025-04-13"


@pytest.mark.parametrize(
    "question",
    [
        "Compared to the league average, how did Avery Example assists compare during the 2024-25 regular season versus the last 2 games?",
        "Against the league average, how did Avery Example assists trend in the 2024-25 regular season?",
        "In the 2024-25 regular season, compare Avery Example assists last 2 games with the league average last 3 games.",
        "In the 2024-25 regular season, compare the league average last 3 games with Avery Example assists last 2 games.",
    ],
)
def test_global_qualifiers_do_not_hide_period_operations(monkeypatch, question):
    payload, final = reference_endpoint_pair(monkeypatch, config(), question)
    assert payload["status"] == final["status"] == "unsupported_scope", payload
    assert payload["tables"] == payload["charts"] == []
    assert not payload.get("semantic_evidence")


@pytest.mark.parametrize(
    "alias", ["this", "current", "selected", "last", "previous", "prior"]
)
@pytest.mark.parametrize("inverted", [False, True])
def test_relative_seasons_are_reconciled_before_scope_consumption(
    monkeypatch, alias, inverted
):
    selected = "2025-26" if alias in {"this", "current", "selected"} else "2024-25"
    question = (
        f"Compare {alias} season's league average against Avery Example's assists during the 2024-25 playoffs."
        if inverted
        else f"Compare Avery Example's assists during the 2024-25 playoffs against {alias} season's league average."
    )
    payload, final = reference_endpoint_pair(
        monkeypatch, replace(config(), season=selected), question
    )
    assert payload["status"] == final["status"] == "unsupported_scope", payload
    assert "season references" in payload["answer"]
    assert payload["tables"] == payload["charts"] == []
    assert not payload.get("semantic_evidence")


@pytest.mark.parametrize(
    "alias", ["this", "current", "selected", "last", "previous", "prior"]
)
def test_matching_relative_and_absolute_seasons_preserve_the_shared_scope(
    monkeypatch, alias
):
    selected = "2024-25" if alias in {"this", "current", "selected"} else "2025-26"
    payload, final = reference_endpoint_pair(
        monkeypatch,
        replace(config(), season=selected),
        f"Compare Avery Example's assists during the 2024-25 playoffs against {alias} season's league average.",
    )
    assert payload["status"] == final["status"] == "ok", payload
    evidence = payload["semantic_evidence"]
    assert evidence == final["semantic_evidence"]
    assert evidence["scope"]["season"] == "2024-25"
    assert evidence["scope"]["season_type"] == "Playoffs"
    assert evidence["scope"]["window"] == "season_to_date"


@pytest.mark.parametrize(
    "relative,phase",
    [
        ("this regular season", "regular season"),
        ("this postseason", "playoffs"),
        ("these playoffs", "playoffs"),
    ],
)
@pytest.mark.parametrize(
    "selected,expected", [("2025-26", "unsupported_scope"), ("2024-25", "ok")]
)
def test_phase_qualified_relative_seasons_preserve_phase_and_identity(
    monkeypatch, relative, phase, selected, expected
):
    payload, final = reference_endpoint_pair(
        monkeypatch,
        replace(config(), season=selected),
        f"Compare Avery Example assists during the 2024-25 {phase} against the league average for {relative}.",
    )
    assert payload["status"] == final["status"] == expected, payload
    if expected == "ok":
        evidence = payload["semantic_evidence"]
        assert evidence == final["semantic_evidence"]
        assert evidence["scope"]["season"] == "2024-25"
        assert evidence["scope"]["season_type"] == (
            "Regular Season" if phase == "regular season" else "Playoffs"
        )
        assert evidence["player_id"] == 101
    else:
        assert payload["tables"] == payload["charts"] == []
        assert not payload.get("semantic_evidence")


@pytest.mark.parametrize(
    "relative", ["last playoffs", "previous postseason", "prior regular season"]
)
def test_unresolved_relative_phase_alias_cannot_execute_the_loaded_season(
    monkeypatch, relative
):
    payload, final = reference_endpoint_pair(
        monkeypatch,
        config(),
        f"Compare Avery Example assists against league average during {relative}.",
    )
    assert payload["status"] == final["status"] == "unsupported_scope", payload
    assert payload["tables"] == payload["charts"] == []
    assert not payload.get("semantic_evidence")


@pytest.mark.parametrize(
    "alias", ["this", "current", "selected", "last", "previous", "prior"]
)
@pytest.mark.parametrize("inverted", [False, True])
def test_full_relative_league_period_cannot_become_a_focal_window(
    monkeypatch, alias, inverted
):
    selected = "2024-25" if alias in {"this", "current", "selected"} else "2025-26"
    question = (
        f"Compare {alias} season's league average to Avery Example's assists during the last 2 games."
        if inverted
        else f"Compare Avery Example's assists during the last 2 games to {alias} season's league average."
    )
    payload, final = reference_endpoint_pair(
        monkeypatch, replace(config(), season=selected), question
    )
    assert payload["status"] == final["status"] == "unsupported_scope", payload
    assert payload["tables"] == payload["charts"] == []
    assert not payload.get("semantic_evidence")


@pytest.mark.parametrize(
    "question,date_range",
    [
        (
            "Compared with the league average in assists, how did Avery Example perform in the 2024-25 regular season?",
            False,
        ),
        (
            "Against the league average in assists how did Avery Example perform in the 2024-25 regular season?",
            False,
        ),
        (
            "Against the league average Avery Example assists in the 2024-25 regular season.",
            False,
        ),
        (
            "In the 2024-25 regular season, Avery Example’s assists versus the league average from 2025-04-11 to 2025-04-13.",
            True,
        ),
        (
            "In the 2024-25 regular season Avery Example’s assists versus the league average from 2025-04-11 to 2025-04-13.",
            True,
        ),
    ],
)
def test_operand_boundaries_do_not_depend_on_leading_verbs(
    monkeypatch, question, date_range
):
    payload, final = reference_endpoint_pair(monkeypatch, config(), question)
    assert payload["status"] == final["status"] == "ok", payload
    evidence = payload["semantic_evidence"]
    assert evidence == final["semantic_evidence"]
    assert evidence["scope"]["season"] == "2024-25"
    assert evidence["scope"]["window"] == (
        "date_range" if date_range else "season_to_date"
    )
    assert evidence["player"]["value"] == (2 if date_range else 2.5)
    assert evidence["baseline"]["value"] == pytest.approx(
        30 / 9 if date_range else 69 / 18
    )


@pytest.mark.parametrize(
    "question",
    [
        "Compared with the league average in assists during the 2024-25 regular season, how did Avery Example perform during the last 2 games?",
        "Compared with the league average in assists from 2025-04-11 to 2025-04-13, how did Avery Example perform during the 2024-25 regular season?",
        "Compared with the current season's league average in assists, how did Avery Example perform during the last 2 games?",
        "In the 2024-25 regular season, Avery Example’s assists during the last 2 games versus the league average from 2025-04-11 to 2025-04-13.",
    ],
)
def test_preposed_operand_modifiers_retain_their_periods(monkeypatch, question):
    payload, final = reference_endpoint_pair(monkeypatch, config(), question)
    assert payload["status"] == final["status"] == "unsupported_scope", payload
    assert payload["tables"] == payload["charts"] == []
    assert not payload.get("semantic_evidence")


@pytest.mark.parametrize(
    "operand,date_range",
    [
        ("the 2024-25 league average", False),
        ("this season’s league average", False),
        ("the 2024-25 regular season league average", False),
        ("the 2024-25 league average from 2025-04-11 to 2025-04-13", True),
    ],
)
def test_preposed_operand_preserves_modifiers_before_and_after_the_noun(
    monkeypatch, operand, date_range
):
    payload, final = reference_endpoint_pair(
        monkeypatch,
        config(),
        f"Compared to {operand}, how did Avery Example perform in assists per game?",
    )
    assert payload["status"] == final["status"] == "ok", payload
    evidence = payload["semantic_evidence"]
    assert evidence == final["semantic_evidence"]
    assert evidence["player_id"] == 101
    assert evidence["scope"]["season"] == "2024-25"
    assert evidence["scope"]["season_type"] == "Regular Season"
    assert evidence["scope"]["window"] == (
        "date_range" if date_range else "season_to_date"
    )
    assert evidence["player"]["value"] == (2 if date_range else 2.5)
    assert evidence["baseline"]["value"] == pytest.approx(
        30 / 9 if date_range else 69 / 18
    )
    if date_range:
        assert evidence["scope"]["start_date"] == "2025-04-11"
        assert evidence["scope"]["as_of"] == "2025-04-13"


@pytest.mark.parametrize("possessive", ["", "’s"])
@pytest.mark.parametrize("inverted", [False, True])
def test_pre_noun_league_calendar_is_not_a_shared_window_qualifier(
    monkeypatch, possessive, inverted
):
    league = f"the 2024-25 regular season{possessive} league average"
    focal = "Avery Example's assists during the last 2 games"
    question = (
        f"Compare {league} against {focal}."
        if inverted
        else f"Compare {focal} against {league}."
    )
    payload, final = reference_endpoint_pair(monkeypatch, config(), question)
    assert payload["status"] == final["status"] == "unsupported_scope", payload
    assert payload["tables"] == payload["charts"] == []
    assert not payload.get("semantic_evidence")


@pytest.mark.parametrize(
    "question",
    [
        "Compared to this season’s league average, how did Avery Example perform in assists during the last 2 games?",
        "Against the 2024-25 regular season league average, how did Avery Example perform in assists during the last 2 games?",
        "Compared to the 2024-25 league average from 2025-04-11 to 2025-04-13, how did Avery Example perform in assists during the last 2 games?",
        "Compare Avery Example's last 2 games, the current season's league average in assists.",
        "Compare the current season's league average in assists, Avery Example's last 2 games.",
        "Compare Avery Example's last 2 games the current season's league average in assists.",
    ],
)
def test_unbound_or_preposed_league_period_cannot_collapse_to_a_focal_window(
    monkeypatch, question
):
    payload, final = reference_endpoint_pair(monkeypatch, config(), question)
    assert payload["status"] == final["status"] == "unsupported_scope", payload
    assert payload["tables"] == payload["charts"] == []
    assert not payload.get("semantic_evidence")
