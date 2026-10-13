from datetime import date

import pytest
from app.agent.followup import resolve_followup
from app.agent.performance_overview import overview_scope
from app.agent.semantics import SemanticError
from app.agent.time_defaults import resolve_time_defaults


@pytest.mark.parametrize(
    "phrase,expected",
    [
        ("last year", "2025-26"),
        ("last season", "2025-26"),
        ("previous year", "2025-26"),
        ("prior season", "2025-26"),
        ("past year", "past 12 months"),
        ("past 2 years", "past 24 months"),
        ("in 2025", "from 2025-01-01 through 2025-12-31"),
        ("in January", "in January 2026"),
        ("recently", "last 10 games"),
        ("lately", "last 10 games"),
        ("this year", "2026-27"),
    ],
)
def test_shared_time_defaults(phrase, expected):
    resolved, notes, _ = resolve_time_defaults(
        "Klay Thompson performance " + phrase, date(2026, 10, 12)
    )
    assert expected in resolved
    assert notes
    assert resolve_time_defaults(resolved, date(2026, 10, 12))[0] == resolved


@pytest.mark.parametrize(
    "today,season", [(date(2026, 6, 30), "2024-25"), (date(2026, 7, 1), "2025-26")]
)
def test_completed_season_boundaries(today, season):
    assert season in resolve_time_defaults("Klay last year", today)[0]


def test_month_inherits_conversation_season():
    assert (
        "January 2025"
        in resolve_time_defaults(
            "Klay in January", date(2026, 10, 12), {"scope": {"season": "2024-25"}}
        )[0]
    )


@pytest.mark.parametrize(
    "question",
    [
        "Klay last year and past year",
        "Klay last year from 2025-01-01 through 2025-02-01",
    ],
)
def test_conflicting_periods_still_clarify(question):
    with pytest.raises(SemanticError):
        resolve_time_defaults(question, date(2026, 10, 12))


def test_followup_inherits_resolved_period_and_explicit_time_overrides():
    context = {
        "players": [{"player_id": 1, "player_name": "Klay Thompson"}],
        "scope": {
            "start": "2025-07-01",
            "end": "2026-06-30",
            "seasons": ["2025-26"],
            "phases": ["Regular Season", "Playoffs"],
        },
    }
    inherited, _ = resolve_followup("What about his shooting?", context)
    assert "2025-07-01 through 2026-06-30" in inherited
    changed, _ = resolve_followup("How has he played lately?", context)
    assert "from 2025-07-01" not in changed


def test_rolling_year_exact_dates():
    result = overview_scope("Klay performance past year", "2025-26", date(2026, 10, 12))
    assert result["start"] == date(2025, 10, 13)
    assert result["end"] == date(2026, 10, 12)


def test_asof_date_is_not_an_extra_season():
    scope = overview_scope("Klay performance last year as of 2025-10-12", "2025-26")
    assert scope["start"] == date(2024, 7, 1)
    assert scope["end"] == date(2025, 6, 30)


def test_metric_planner_receives_resolved_calendar_dates():
    from app.agent.semantic_planner import explicit_scope

    normalized, _, _ = resolve_time_defaults(
        "Klay points in January", date(2026, 10, 12)
    )
    assert explicit_scope(normalized)["start_date"] == "2026-01-01"
    assert explicit_scope(normalized)["as_of"] == "2026-01-31"


@pytest.mark.parametrize(
    "phrase,start,end",
    [
        ("this month", "2025-01-01", "2025-01-15"),
        ("last week", "2025-01-06", "2025-01-12"),
        ("in January 2025", "2025-01-01", "2025-01-15"),
    ],
)
def test_metric_calendar_scope_respects_explicit_asof(phrase, start, end):
    from app.agent.semantic_planner import explicit_scope

    hints = explicit_scope(f"Klay points {phrase} as of 2025-01-15")
    assert hints["start_date"] == start
    assert hints["as_of"] == end


@pytest.mark.parametrize(
    "question,expected",
    [
        ("Klay performance in May in the 2025 playoffs", "May 2025"),
        ("Klay performance in November of the 2024-25 season", "November 2024"),
    ],
)
def test_month_year_uses_explicit_season_before_conversation(question, expected):
    resolved, _, _ = resolve_time_defaults(
        question, date(2026, 10, 12), {"scope": {"season": "2025-26"}}
    )
    assert expected in resolved
    scoped = overview_scope(resolved, "2025-26", date(2026, 10, 12))
    assert str(scoped["start"]).startswith(
        "2025-05" if "May" in expected else "2024-11"
    )


def test_month_after_multiseason_context_uses_latest_occurrence():
    resolved, _, _ = resolve_time_defaults(
        "What about January?",
        date(2026, 10, 12),
        {
            "scope": {
                "season": "2025-26",
                "seasons": ["2024-25", "2025-26"],
                "end": "2026-10-12",
            }
        },
    )
    assert resolved == "What about January 2026?"


@pytest.mark.parametrize("phrase", ["this year", "2024-25", "recently"])
def test_overview_asof_within_season_caps_the_period(phrase):
    scoped = overview_scope(
        f"Klay performance {phrase} as of 2025-03-01", "2025-26", date(2026, 10, 12)
    )
    assert scoped["start"] == date(2024, 7, 1)
    assert scoped["end"] == date(2025, 3, 1)


@pytest.mark.parametrize(
    "question",
    [
        "Compare Klay points in November 2025 vs December 2025",
        "Klay points since March 2025",
    ],
)
def test_complex_calendar_metric_requests_are_left_to_planner(question):
    from app.agent.semantic_planner import explicit_scope

    assert "window" not in explicit_scope(question)
