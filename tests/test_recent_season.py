"""Date-based season recency across NBA opening nights and Ask entry points."""

from datetime import date

import pytest
from app import main
from app.agent.conversation import InMemoryConversationStore
from app.agent.independent_baseline import IndependentBaselineSpec
from app.agent.reference_ask import parse_reference
from app.agent.time_defaults import resolve_recent_season, resolve_time_defaults
from app.seasons import most_recent_season
from scripts.ask_eval.fixtures import frozen_repository
from tests.test_ask_harness import SOURCE
from tests.test_league_reference_language import reference_endpoint_pair, service
from tests.test_reference_followup_aggregation import endpoint_client, post, settings
from tests.test_reference_intelligence import config


@pytest.mark.parametrize(
    "day,expected",
    [
        (date(2024, 10, 21), "2023-24"),
        (date(2024, 10, 22), "2024-25"),
        (date(2025, 7, 1), "2024-25"),
        (date(2025, 10, 20), "2024-25"),
        (date(2025, 10, 21), "2025-26"),
        (date(2026, 1, 1), "2025-26"),
        (date(2026, 8, 1), "2025-26"),
        (date(2026, 10, 19), "2025-26"),
        (date(2026, 10, 20), "2026-27"),
        (date(2026, 11, 12), "2026-27"),
    ],
)
def test_recent_season_follows_opening_nights(day, expected):
    assert most_recent_season(day) == expected


@pytest.mark.parametrize(
    "phrase",
    [
        "recent season",
        "most recent season",
        "most recent NBA season",
        "recent regular season",
    ],
)
def test_recent_season_is_shared_with_calendar_planning(phrase):
    question = f"Show Avery Example assists per game during the {phrase}."
    resolved, notes, _ = resolve_time_defaults(question, date(2025, 8, 1))
    assert "2024-25" in resolved
    assert "recent" not in resolved
    assert notes and "2025-08-01" in notes[0]
    assert resolve_recent_season(resolved, date(2025, 8, 1)) == (resolved, [])


def test_recent_games_keeps_its_own_window():
    question = "Compare Avery Example recent assists against league average."
    assert resolve_recent_season(question, date(2025, 8, 1)) == (question, [])


def test_recent_season_honors_explicit_historical_asof():
    resolved, notes = resolve_recent_season(
        "Avery Example assists in the recent season as of 2025-08-01",
        date(2026, 11, 12),
    )
    assert "2024-25" in resolved
    assert "2025-08-01" in notes[0]


def test_direct_reference_parser_uses_the_same_calendar_and_period_guard(monkeypatch):
    monkeypatch.setattr(
        "app.agent.time_defaults.semantic_today", lambda: date(2025, 8, 1)
    )
    players = [{"player_id": 101, "player_name": "Avery Example"}]
    _, season, query = parse_reference(
        "Compare Avery Example assists against league average during the recent season.",
        players,
        "2023-24",
        "league_baseline",
    )
    assert season == "2024-25"
    assert query.window == "season_to_date"
    _, season, independent = parse_reference(
        "Compare Avery Example assists during the last 2 games against league average for the recent season.",
        players,
        "2023-24",
        "league_baseline",
    )
    assert isinstance(independent, IndependentBaselineSpec)
    assert season == independent.league_query.season == "2024-25"
    assert independent.player_query.n == 2
    assert independent.league_query.window == "season_to_date"


@pytest.mark.parametrize(
    "question,route",
    [
        (
            "Compare Avery Example assists against league average during the recent season.",
            "league_baseline",
        ),
        (
            "Show Avery Example's overview for the most recent NBA season.",
            "player_overview",
        ),
        (
            "Compare Avery Example and Blair Sample for the recent season.",
            "player_comparison",
        ),
        (
            "Avery Example assists per game in the recent season.",
            "governed_metrics",
        ),
    ],
)
def test_recent_season_uses_normal_routes_on_both_transports(
    monkeypatch, question, route
):
    monkeypatch.setattr(
        "app.agent.time_defaults.semantic_today", lambda: date(2025, 8, 1)
    )
    payload, streamed = reference_endpoint_pair(monkeypatch, config(), question)
    for result in (payload, streamed):
        assert result["status"] == "ok", result
        assert result["ask_route"]["route"] == route
        assert any(
            "2024-25" in note and "2025-08-01" in note for note in result["assumptions"]
        )
        assert result["clarification_options"] == []
    for key in (
        "semantic_evidence",
        "overview_evidence",
        "comparison_evidence",
        "tables",
    ):
        assert payload.get(key) == streamed.get(key)
    if route == "league_baseline":
        evidence = payload["semantic_evidence"]
        assert evidence["scope"]["window"] == "season_to_date"
        assert evidence["scope"]["n"] is None
        assert evidence["player"]["observed_games"] == 6
        assert evidence["player"]["value"] == 2.5


def test_recent_season_overrides_selected_archive_without_older_fallback(monkeypatch):
    monkeypatch.setattr(
        "app.agent.time_defaults.semantic_today", lambda: date(2026, 11, 12)
    )
    payload, streamed = reference_endpoint_pair(
        monkeypatch,
        config(),
        "Compare Avery Example assists against league average during the recent season.",
    )
    for result in (payload, streamed):
        assert result["status"] == "unsupported_coverage", result
        assert result["tables"] == []
        assert any("2026-27" in note for note in result["assumptions"])


@pytest.mark.parametrize(
    "question,status",
    [
        (
            "Compare Avery Example assists during the recent season against the league average over the last 2 games.",
            "ok",
        ),
        (
            "Compare Avery Example assists during the last 2 games against the league average for the recent season.",
            "ok",
        ),
        (
            "Compare Avery Example assists during the last 2 games against the league average during the most recent regular season.",
            "ok",
        ),
        (
            "Compared to the league average for the recent season, how did Avery Example perform in assists over the last 2 games?",
            "unsupported_scope",
        ),
    ],
)
def test_recent_season_preserves_explicit_independent_league_period(
    monkeypatch, question, status
):
    monkeypatch.setattr(
        "app.agent.time_defaults.semantic_today", lambda: date(2025, 8, 1)
    )
    payload, streamed = reference_endpoint_pair(
        monkeypatch,
        config(),
        question,
    )
    for result in (payload, streamed):
        assert result["status"] == status, result
        if status == "ok":
            assert result["semantic_evidence"]["independent_periods"] is True
        else:
            assert result["tables"] == []


@pytest.mark.parametrize(
    "question,n,count,player,league",
    [
        (
            "During the recent season, compare Avery Example assists over the last 2 games against the league average.",
            2,
            6,
            4.5,
            35 / 6,
        ),
        (
            "During the recent season, compare Avery Example assists with the league average from 2025-04-11 to 2025-04-13.",
            None,
            9,
            2.0,
            10 / 3,
        ),
        (
            "During the most recent regular season, how did Avery Example's assists compare with the league average over the last 2 games?",
            2,
            6,
            4.5,
            35 / 6,
        ),
    ],
)
def test_global_recent_season_can_qualify_a_shared_window(
    monkeypatch, question, n, count, player, league
):
    monkeypatch.setattr(
        "app.agent.time_defaults.semantic_today", lambda: date(2025, 8, 1)
    )
    payload, streamed = reference_endpoint_pair(
        monkeypatch,
        config(),
        question,
    )
    for result in (payload, streamed):
        assert result["status"] == "ok", result
        evidence = result["semantic_evidence"]
        assert evidence["scope"]["n"] == n
        assert evidence["baseline"]["observed_games"] == count
        assert evidence["player"]["value"] == pytest.approx(player)
        assert evidence["baseline"]["value"] == pytest.approx(league)


def test_followup_retains_resolved_season_when_clock_changes(monkeypatch):
    monkeypatch.setattr(
        "app.agent.time_defaults.semantic_today", lambda: date(2025, 8, 1)
    )
    agent = service()
    agent.conversation_store = InMemoryConversationStore()
    first = agent.answer(
        "Compare Avery Example assists against league average during the recent season.",
        conversation_id="recent-season",
    )
    assert first["status"] == "ok", first
    monkeypatch.setattr(
        "app.agent.time_defaults.semantic_today", lambda: date(2026, 11, 12)
    )
    followup = agent.answer("What about rebounds?", conversation_id="recent-season")
    assert followup["status"] == "ok", followup
    assert followup["semantic_evidence"]["scope"]["season"] == "2024-25"


@pytest.mark.parametrize("transport", ["json", "sse"])
@pytest.mark.parametrize("mixed_period", [False, True])
def test_identity_selection_retains_original_period_and_pinned_season(
    monkeypatch, transport, mixed_period
):
    monkeypatch.setattr(
        "app.agent.time_defaults.semantic_today", lambda: date(2025, 8, 1)
    )
    cfg = settings()
    client, _, previous = endpoint_client(monkeypatch, cfg)
    repo = frozen_repository(cfg, SOURCE, "ambiguous")
    main.app.dependency_overrides[main.get_repository] = lambda: repo
    question = (
        "Compare Avery Example assists during the last 2 games against league average for the recent season."
        if mixed_period
        else "Compare Avery Example assists against league average during the recent season."
    )
    try:
        with client:
            first = post(client, transport, question, "season-selection")
            assert first["status"] == "clarification_required", first
            monkeypatch.setattr(
                "app.agent.time_defaults.semantic_today", lambda: date(2026, 11, 12)
            )
            resumed = post(
                client,
                transport,
                "Avery Example",
                "season-selection",
                selected_player_id=101,
                selected_player_name="Avery Example",
            )
        assert any(
            "2024-25" in note and "2025-08-01" in note
            for note in resumed["assumptions"]
        )
        if mixed_period:
            assert resumed["status"] == "ok", resumed
            evidence = resumed["semantic_evidence"]
            assert evidence["independent_periods"] is True
            assert evidence["scope"]["season"] == "2024-25"
            assert evidence["baseline_scope"]["season"] == "2024-25"
            assert evidence["scope"]["n"] == 2
            assert evidence["baseline_scope"]["n"] is None
            assert evidence["player"]["value"] == 4.5
            assert evidence["baseline"]["observed_games"] == 18
        else:
            assert resumed["status"] == "ok", resumed
            assert resumed["semantic_evidence"]["scope"]["season"] == "2024-25"
            assert resumed["semantic_evidence"]["baseline"]["observed_games"] == 18
    finally:
        main.app.dependency_overrides.clear()
        main.app.dependency_overrides.update(previous)
