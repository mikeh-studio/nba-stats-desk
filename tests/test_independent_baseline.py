"""Independent player/league periods use existing fixture rows and no model."""

from copy import deepcopy
from datetime import date

import pytest
from app.agent.independent_baseline import IndependentBaselineSpec
from app.agent.observability import AgentTrace
from app.agent.reference_ask import parse_reference
from scripts.ask_eval.fixtures import FrozenWarehouse
from tests.test_ask_harness import SOURCE
from tests.test_league_reference_language import reference_endpoint_pair, service
from tests.test_reference_intelligence import config


@pytest.mark.parametrize(
    "question,player_value,league_value,count,player_n,league_n",
    [
        (
            "Compare Avery Example assists during the full 2024-25 regular season against the league average over the last 2 games",
            2.5,
            35 / 6,
            6,
            None,
            2,
        ),
        (
            "Compare Avery Example assists over the last 2 games against the league average during the full 2024-25 regular season",
            4.5,
            69 / 18,
            18,
            2,
            None,
        ),
        (
            "Compare the league average over the last 2 games with Avery Example assists during the full 2024-25 regular season",
            2.5,
            35 / 6,
            6,
            None,
            2,
        ),
        (
            "Compare the league average during the full 2024-25 regular season with Avery Example assists over the last 2 games",
            4.5,
            69 / 18,
            18,
            2,
            None,
        ),
    ],
)
def test_independent_periods_direct_and_json_sse(
    monkeypatch, question, player_value, league_value, count, player_n, league_n
):
    trace = AgentTrace("independent-baseline", "", "fixture")
    direct = service().answer(question, trace=trace)
    assert trace.model_calls == 0
    normal, streamed = reference_endpoint_pair(monkeypatch, config(), question)
    for payload in (direct, normal, streamed):
        assert payload["status"] == "ok", payload
        assert payload["ask_route"]["route"] == "league_baseline"
        evidence = payload["semantic_evidence"]
        assert evidence["contract_version"] == "league_baseline/3"
        assert evidence["independent_periods"] is True
        assert evidence["scope"]["n"] == player_n
        assert evidence["baseline_scope"]["n"] == league_n
        assert evidence["player"]["value"] == pytest.approx(player_value)
        assert evidence["baseline"]["value"] == pytest.approx(league_value)
        assert evidence["difference"] == pytest.approx(player_value - league_value)
        assert evidence["membership"]["count"] == count
        assert evidence["baseline"]["observed_games"] == count
        assert len(evidence["membership"]["sha256"]) == 64
        assert "game_ids" not in evidence["baseline"]
        assert payload["tables"][0]["columns"][-1]["key"] == "period"
        assert "independent periods" in payload["tables"][0]["title"]
        assert payload["claim_validation"]["status"] != "invalid"
    for key in (
        "status",
        "answer",
        "semantic_evidence",
        "tables",
        "charts",
        "ask_route",
    ):
        assert normal[key] == streamed[key]


def test_direct_parser_recognizes_original_recent_season_without_reanchoring(
    monkeypatch,
):
    monkeypatch.setattr(
        "app.agent.time_defaults.semantic_today", lambda: date(2026, 11, 12)
    )
    _, season, spec = parse_reference(
        "Compare Avery Example assists during the last 2 games against league average for 2024-25.",
        [{"player_id": 101, "player_name": "Avery Example"}],
        "2026-27",
        "league_baseline",
        period_question="Compare Avery Example assists during the last 2 games against league average for the recent season.",
    )
    assert isinstance(spec, IndependentBaselineSpec)
    assert season == spec.player_query.season == spec.league_query.season == "2024-25"
    assert spec.player_query.n == 2
    assert spec.league_query.window == "season_to_date"


def test_recent_season_operand_and_global_qualifier_have_distinct_semantics(
    monkeypatch,
):
    monkeypatch.setattr(
        "app.agent.time_defaults.semantic_today", lambda: date(2025, 8, 1)
    )
    own = service().answer(
        "Compare Avery Example assists during the last 2 games against league average for the recent season."
    )
    shared = service().answer(
        "During the recent season, compare Avery Example assists over the last 2 games against the league average."
    )
    assert own["status"] == shared["status"] == "ok"
    assert own["semantic_evidence"]["independent_periods"] is True
    assert own["semantic_evidence"]["baseline"]["observed_games"] == 18
    assert shared["semantic_evidence"].get("independent_periods") is None
    assert shared["semantic_evidence"]["baseline"]["observed_games"] == 6


def test_league_windows_keep_short_peers_and_disclose_actual_counts():
    data = deepcopy(SOURCE)
    data["rows"] = [
        r for r in data["rows"] if r["player_id"] != 202 or r["game_id"] == "g0"
    ]
    payload = service(data).answer(
        "Compare Avery Example assists during the full 2024-25 regular season against league average over the last 2 games"
    )
    assert payload["status"] == "ok", payload
    evidence = payload["semantic_evidence"]
    assert evidence["membership"]["count"] == 5
    assert evidence["baseline_scope"]["partial_window_players"] == 1
    assert "league_partial_appearance_windows" in evidence["warnings"]
    assert any(
        "up to 2" in note and "1 players had fewer" in note
        for note in payload["assumptions"]
    )
    assert (
        next(
            row
            for row in evidence["baseline_scope"]["appearance_windows"]
            if row["player_id"] == 202
        )["observed_games"]
        == 1
    )


def test_independent_league_missing_components_suppress_difference_and_chart():
    data = deepcopy(SOURCE)
    next(r for r in data["rows"] if r["player_id"] == 202 and r["game_id"] == "g5")[
        "ast"
    ] = None
    payload = service(data).answer(
        "Compare Avery Example assists during the full 2024-25 regular season against league average over the last 2 games"
    )
    assert payload["status"] == "ok", payload
    evidence = payload["semantic_evidence"]
    assert evidence["player"]["value"] == 2.5
    assert evidence["baseline"]["observed_games"] == 6
    assert evidence["baseline"]["valid_games"] == 5
    assert evidence["baseline"]["missing_component_games"] == 1
    assert evidence["difference"] is None
    assert payload["answerability"] == "partial"
    assert payload["charts"] == []


def test_independent_ratios_pool_components_and_keep_zero_attempts_unknown():
    data = deepcopy(SOURCE)
    for row in data["rows"]:
        row["fgm"], row["fga"] = (1, 1) if row["game_id"] == "g4" else (0, 9)
    question = "Compare Avery Example field goal percentage during the full 2024-25 regular season against league average over the last 2 games"
    payload = service(data).answer(question)
    assert payload["status"] == "ok", payload
    evidence = payload["semantic_evidence"]
    assert evidence["player"]["value"] == pytest.approx(1 / 46)
    assert evidence["baseline"]["value"] == pytest.approx(3 / 30)
    assert evidence["difference_unit"] == "percentage points"
    for row in data["rows"]:
        row["fgm"] = row["fga"] = 0
    zero = service(data).answer(question)
    assert zero["semantic_evidence"]["difference"] is None
    assert zero["charts"] == []


def test_independent_periods_validate_complete_cohort_before_math():
    from dataclasses import replace

    from app.agent.independent_baseline import build_independent_baseline
    from app.agent.semantics import SemanticError

    _, evidence = FrozenWarehouse(SOURCE, "complete").load(["2024-25"])
    player, _, spec = parse_reference(
        "Compare Avery Example assists during the full 2024-25 regular season against league average over the last 2 games",
        [{"player_id": 101, "player_name": "Avery Example"}],
        "2024-25",
        "league_baseline",
    )
    with pytest.raises(SemanticError) as error:
        build_independent_baseline(
            replace(evidence, cohort_complete=False), player, spec
        )
    assert error.value.code == "unsupported_coverage"


@pytest.mark.parametrize(
    "suffix",
    [
        "while wearing blue shoes",
        "among centers",
        "only fourth quarters",
        "in total",
        "in wins versus losses",
        "for the player only",
    ],
)
def test_unknown_or_asymmetric_independent_modifiers_are_not_dropped(suffix):
    payload = service().answer(
        "Compare Avery Example assists during the full 2024-25 regular season against league average over the last 2 games "
        + suffix
    )
    assert payload["status"] != "ok", payload
    assert not payload["tables"] and not payload["charts"]


@pytest.mark.parametrize("transport", ["json", "sse"])
@pytest.mark.parametrize(
    "first,followup,direct,independent",
    [
        (
            "Compare Avery Example assists against league average in 2024-25 with at least 4 points",
            "What about rebounds?",
            "Compare Avery Example rebounds against league average in 2024-25 with at least 4 points",
            False,
        ),
        (
            "Compare Avery Example assists over the last 2 games against the league average during the full 2024-25 regular season with at least 4 points",
            "What about rebounds?",
            "Compare Avery Example rebounds over the last 2 games against the league average during the full 2024-25 regular season with at least 4 points",
            True,
        ),
    ],
)
def test_reference_metric_followup_preserves_focal_slot_predicates_and_operands(
    monkeypatch, transport, first, followup, direct, independent
):
    from app import main
    from tests.test_reference_followup_aggregation import (
        endpoint_client,
        post,
        settings,
    )

    client, _, previous = endpoint_client(monkeypatch, settings())
    try:
        with client:
            original = post(client, transport, first, "reference-metric-slot")
            result = post(client, transport, followup, "reference-metric-slot")
            expected = post(client, transport, direct, None)
        assert original["status"] == result["status"] == expected["status"] == "ok", (
            result
        )
        evidence = result["semantic_evidence"]
        assert evidence["metric"]["key"] == "reb"
        assert evidence["scope"]["stat_filters"] == [
            {"component": "pts", "op": "gte", "value": 4.0}
        ]
        assert bool(evidence.get("independent_periods")) == independent
        assert evidence == expected["semantic_evidence"]
        if independent:
            assert evidence["scope"]["window"] == "last_n_games"
            assert evidence["scope"]["n"] == 2
            assert evidence["baseline_scope"]["window"] == "season_to_date"
    finally:
        main.app.dependency_overrides.clear()
        main.app.dependency_overrides.update(previous)


@pytest.mark.parametrize("transport", ["json", "sse"])
@pytest.mark.parametrize(
    "focal,player_value",
    [
        ("from 2025-04-10 to 2025-04-11", 0.5),
        ("over the last 2 games", 4.5),
    ],
)
def test_literal_independent_dates_choose_archive_before_source_or_identity(
    monkeypatch, transport, focal, player_value
):
    from app import main
    from tests.test_reference_followup_aggregation import (
        endpoint_client,
        post,
        settings,
    )

    client, _, previous = endpoint_client(monkeypatch, settings(season="2025-26"))
    question = f"Compare Avery Example assists {focal} against league average from 2025-04-13 to 2025-04-15"
    try:
        with client:
            result = post(client, transport, question, None, request_season="2025-26")
        assert result["status"] == "ok", result
        evidence = result["semantic_evidence"]
        assert evidence["independent_periods"] is True
        assert (
            evidence["scope"]["season"]
            == evidence["baseline_scope"]["season"]
            == "2024-25"
        )
        assert evidence["player"]["value"] == player_value
        assert evidence["baseline"]["value"] == pytest.approx(48 / 9)
        assert evidence["baseline_scope"]["start_date"] == "2025-04-13"
        assert evidence["baseline_scope"]["end_date"] == "2025-04-15"
    finally:
        main.app.dependency_overrides.clear()
        main.app.dependency_overrides.update(previous)


@pytest.mark.parametrize("transport", ["json", "sse"])
def test_equal_appearance_counts_keep_independent_irregular_peer_windows(
    monkeypatch, transport
):
    from app import main
    from scripts.ask_eval.fixtures import frozen_repository
    from tests.test_reference_followup_aggregation import (
        endpoint_client,
        post,
        settings,
    )

    data = deepcopy(SOURCE)
    data["rows"] = [
        row
        for row in data["rows"]
        if not (
            row["player_id"] == 202
            and row["season_type"] == "Regular Season"
            and row["game_id"] in {"g4", "g5"}
        )
    ]
    cfg = settings()
    client, _, previous = endpoint_client(monkeypatch, cfg)
    main.app.dependency_overrides[main.get_repository] = lambda: frozen_repository(
        cfg, data, "complete"
    )
    try:
        with client:
            result = post(
                client,
                transport,
                "Compare Avery Example assists over the last 2 games in the 2024-25 regular season against league average over the last 2 games in the 2024-25 regular season",
                None,
            )
        assert result["status"] == "ok", result
        evidence = result["semantic_evidence"]
        assert evidence["independent_periods"] is True
        assert evidence["scope"]["n"] == evidence["baseline_scope"]["n"] == 2
        assert evidence["player"]["value"] == 4.5
        assert evidence["baseline"]["value"] == pytest.approx(31 / 6)
        assert evidence["baseline"]["observed_games"] == 6
        assert (
            next(
                row
                for row in evidence["baseline_scope"]["appearance_windows"]
                if row["player_id"] == 202
            )["end_date"]
            == "2025-04-13"
        )
    finally:
        main.app.dependency_overrides.clear()
        main.app.dependency_overrides.update(previous)


@pytest.mark.parametrize("transport", ["json", "sse"])
@pytest.mark.parametrize(
    "clause,field,value",
    [("for AAA", "team_abbr", "AAA"), ("against BBB", "opponent_abbr", "BBB")],
)
def test_date_archive_preflight_preserves_case_of_literal_team_predicates(
    monkeypatch, transport, clause, field, value
):
    from app import main
    from tests.test_reference_followup_aggregation import (
        endpoint_client,
        post,
        settings,
    )

    client, _, previous = endpoint_client(monkeypatch, settings(season="2025-26"))
    question = (
        "Compare Avery Example assists from 2025-04-10 to 2025-04-11 against league average from 2025-04-13 to 2025-04-15 "
        + clause
    )
    try:
        with client:
            result = post(client, transport, question, None, request_season="2025-26")
        assert result["status"] == "ok", result
        evidence = result["semantic_evidence"]
        assert evidence["independent_periods"] is True
        assert (
            evidence["scope"]["season"]
            == evidence["baseline_scope"]["season"]
            == "2024-25"
        )
        assert evidence["scope"][field] == evidence["baseline_scope"][field] == value
        assert (
            evidence["player"]["observed_games"]
            == evidence["player"]["valid_games"]
            == 2
        )
        assert evidence["player"]["value"] == 0.5
        assert (
            evidence["baseline"]["observed_games"]
            == evidence["baseline"]["valid_games"]
            == 9
        )
        assert evidence["baseline"]["value"] == pytest.approx(48 / 9)
        assert evidence["difference"] == pytest.approx(0.5 - 48 / 9)
        assert evidence["membership"]["count"] == 9
    finally:
        main.app.dependency_overrides.clear()
        main.app.dependency_overrides.update(previous)
