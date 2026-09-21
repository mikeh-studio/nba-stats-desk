"""Adversarial checks for eligibility, temporal evidence and comparison denominators."""

from copy import deepcopy

import pytest
from app.agent.teammate_readiness import (
    build_panel,
    latest_report,
    summarize_panel,
    validate_memberships,
)


def fixture():
    spec = dict(
        player_id=1,
        teammate_id=2,
        season="2025-26",
        team_abbr="ATL",
        start="2025-11-01",
        end="2025-11-03",
        max_report_age_hours=48,
    )
    games = [
        dict(
            season="2025-26",
            game_id=f"g{i}",
            game_date=f"2025-11-0{i}",
            team_abbr="ATL",
            opponent_abbr="BOS",
            home_away="home",
            scheduled_start_utc=f"2025-11-0{i}T23:00:00Z",
            final=True,
            postponed=False,
        )
        for i in (1, 2, 3)
    ]
    memberships = [
        dict(
            season="2025-26",
            player_id=i,
            team_abbr="ATL",
            valid_from="2025-10-01",
            valid_to="2026-01-01",
            source_urls=["https://example.test/roster"],
            source_published_at=["2025-10-01T00:00:00Z"],
            reviewed_at="2026-09-18T00:00:00Z",
            basis="Reviewed roster",
        )
        for i in (1, 2)
    ]
    stats = [
        dict(
            season="2025-26",
            season_type="Regular Season",
            game_id=f"g{i}",
            game_date=f"2025-11-0{i}",
            player_id=1,
            team_abbr="ATL",
            min=30,
            pts=20 * i,
            ast=4 * i,
            fgm=5,
            fga=10 * i,
            fg3m=1,
            fg3a=4,
            ftm=2,
            fta=3,
            tov=2,
        )
        for i in (1, 2)
    ]
    stats.append({**stats[0], "player_id": 2})
    reports = [
        dict(
            season="2025-26",
            player_id=2,
            team_abbr="ATL",
            game_date=g["game_date"],
            matchup="BOS@ATL",
            injury_status="Out",
            report_timestamp_utc=g["game_date"] + "T20:00:00Z",
            ingested_at_utc="2026-09-18T00:00:00Z",
            source_url="https://example.test/report",
            reason="Injury/Illness",
        )
        for g in games[1:]
    ]
    return stats, reports, games, memberships, spec


def test_preserves_nonparticipation_and_exact_roster_end():
    args = fixture()
    args[3][1]["valid_to"] = "2025-11-03"
    panel = build_panel(*args)
    assert len(panel) == 3
    assert [r["exposure"] for r in panel] == [
        "participated",
        "reported_out_no_appearance",
        "unknown_membership",
    ]
    assert not panel[-1]["included"]
    summary = summarize_panel(panel)
    assert summary["focal_nonparticipation_games"] == 1
    assert summary["differences"]["ast"]["out_minus_participated"] == 4
    assert summary["claim_level"] == "descriptive"


@pytest.mark.parametrize(
    "stamp", ["2025-11-02T23:00:00Z", "2025-11-03T00:00:00Z", "2025-10-30T00:00:00Z"]
)
def test_late_equal_and_stale_reports_excluded(stamp):
    _, reports, games, _, _ = fixture()
    reports[0]["report_timestamp_utc"] = stamp
    assert latest_report(reports, games[1], 2, 48)["status"] == "Unknown"


def test_wrong_matchup_and_wrong_season_are_not_absence():
    args = fixture()
    args[1][0]["matchup"] = "BOS@BKN"
    assert build_panel(*args)[1]["exposure"] == "unknown"
    args[1][0]["matchup"] = "BOS@ATL"
    args[1][0]["season"] = "2024-25"
    assert build_panel(*args)[1]["exposure"] == "unknown"


def test_new_team_bulletin_omission_does_not_preserve_old_out_status():
    args = fixture()
    newer = {
        **args[1][0],
        "player_id": 3,
        "report_timestamp_utc": "2025-11-02T21:00:00Z",
        "source_url": "https://example.test/new-bulletin",
    }
    args[1].append(newer)
    report = latest_report(args[1], args[2][1], 2, 48)
    assert report["status"] == "Unknown"
    assert report["reason"] == "not_listed_in_latest_bulletin"
    assert report["sources"] == [newer["source_url"]]
    row = build_panel(*args)[1]
    assert row["exposure"] == "unknown"
    assert not row["included"]


@pytest.mark.parametrize(
    "changes",
    [
        {"matchup": "BOS@BKN"},
        {"season": "2024-25"},
        {"report_timestamp_utc": "2025-11-02T23:00:00Z"},
    ],
)
def test_ineligible_new_bulletin_does_not_replace_eligible_listing(changes):
    _, reports, games, _, _ = fixture()
    reports.append(
        {
            **reports[0],
            "player_id": 3,
            "report_timestamp_utc": "2025-11-02T21:00:00Z",
            **changes,
        }
    )
    assert latest_report(reports, games[1], 2, 48)["status"] == "Out"


def test_equivalent_timestamps_conflict_and_later_update_resolves():
    _, reports, games, _, _ = fixture()
    reports.append(
        {
            **reports[0],
            "injury_status": "Available",
            "report_timestamp_utc": "2025-11-02T15:00:00-05:00",
        }
    )
    assert latest_report(reports, games[1], 2, 48)["status"] == "Conflicting"
    reports.append(
        {
            **reports[0],
            "injury_status": "Questionable",
            "report_timestamp_utc": "2025-11-02T21:00:00Z",
        }
    )
    assert latest_report(reports, games[1], 2, 48)["status"] == "Questionable"


def test_played_out_conflict_and_zero_minutes_not_inferred_absent():
    args = fixture()
    args[1].append(
        {
            **args[1][0],
            "game_date": "2025-11-01",
            "report_timestamp_utc": "2025-11-01T20:00:00Z",
        }
    )
    assert build_panel(*args)[0]["exposure"] == "conflicting"
    args[0][-1]["min"] = 0
    assert build_panel(*args)[0]["exposure"] == "unknown"


def test_roster_overlap_rejected_and_other_team_excluded():
    args = fixture()
    with pytest.raises(ValueError, match="Overlapping"):
        validate_memberships(args[3] + [deepcopy(args[3][1])])
    args[3][1]["team_abbr"] = "WAS"
    assert build_panel(*args)[1]["exposure"] == "not_teammates"


def test_missing_time_prevents_report_based_absence():
    args = fixture()
    args[2][1]["scheduled_start_utc"] = None
    assert build_panel(*args)[1]["exposure"] == "unknown"


def test_duplicate_fact_and_future_opponent_context_fail():
    args = fixture()
    with pytest.raises(ValueError, match="Duplicate appearance"):
        build_panel(args[0] + [args[0][0]], *args[1:])
    with pytest.raises(ValueError, match="Future opponent"):
        build_panel(
            *args,
            context=[{**args[0][0], "opponent_latest_prior_game_date": "2025-11-01"}],
        )


def test_missing_outcome_never_becomes_zero():
    args = fixture()
    args[0][1]["ast"] = None
    summary = summarize_panel(build_panel(*args))
    assert summary["groups"]["reported_out_no_appearance"]["ast"]["mean"] is None
    assert summary["differences"]["ast"]["out_minus_participated"] is None


def test_unknown_games_break_episodes_and_ratios_use_totals():
    args = fixture()
    panel = build_panel(*args)
    panel[1]["exposure"] = "participated"
    summary = summarize_panel(panel)
    metrics = {
        m["key"]: m for m in summary["groups"]["participated"]["context_metrics"]
    }
    assert metrics["fg_pct"]["value"] == pytest.approx(100 * 10 / 30)
    args[1].clear()
    panel = build_panel(*args)
    assert [r["episode_id"] for r in panel] == [1, 2, 2]
    assert (
        summarize_panel(panel)["differences"]["ast"]["leave_one_episode_out_range"]
        is None
    )


def test_afternoon_capture_uses_same_day_and_daylight_saving_time():
    from scripts.capture_teammate_reports import report_sample

    assert report_sample("2025-12-31T20:00:00Z") == (
        "2025-12-31",
        ("02_30PM", "02PM", "02_00PM"),
    )
    assert report_sample("2025-10-31T23:00:00Z") == ("2025-10-31", ("05PM", "05_00PM"))
    assert report_sample("2025-12-31T18:30:00Z") == ("2025-12-30", ("05PM", "05_00PM"))


def test_readiness_does_not_promote_partial_evidence():
    args = fixture()
    summary = summarize_panel(build_panel(*args))
    assert summary["readiness"]["status"] == "reviewable_descriptive_comparison"
    assert summary["readiness"]["causal_ready"] is False
    args[1].clear()
    assert (
        summarize_panel(build_panel(*args))["readiness"]["status"]
        == "incomplete_descriptive_comparison"
    )
