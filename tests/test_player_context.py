"""Exercise real dbt SELECTs and statistical edge cases with fictional games."""

import sqlite3
from copy import deepcopy

import pytest
from app.agent.context_metrics import context_metrics
from app.agent.player_context import teammate_comparison
from scripts.build_player_context import build_context


def test_shooting_uses_counts_and_per36_uses_total_minutes():
    rows = [
        dict(
            fgm=1,
            fga=2,
            fg3m=1,
            fg3a=1,
            ftm=0,
            fta=0,
            pts=3,
            ast=2,
            min=10,
            tov=1,
            game_id="a",
        ),
        dict(
            fgm=4,
            fga=18,
            fg3m=0,
            fg3a=2,
            ftm=2,
            fta=2,
            pts=10,
            ast=1,
            min=30,
            tov=0,
            game_id="b",
        ),
    ]
    metrics = {m["key"]: m for m in context_metrics(rows, rows[:1])}
    assert metrics["fg_pct"]["value"] == 25
    assert metrics["fg_pct"]["change"] == -25
    assert metrics["fg_pct"]["change_unit"] == "percentage points"
    assert metrics["efg_pct"]["value"] == pytest.approx(27.5)
    assert metrics["ts_pct"]["value"] == pytest.approx(100 * 13 / (2 * (20 + 0.44 * 2)))
    assert metrics["ast_per36"]["value"] == pytest.approx(2.7)
    assert metrics["min"]["value"] == 20
    assert metrics["min"]["unit"] == "minutes per game"
    assert metrics["min"]["change_unit"] == "minutes per game"


def test_partial_denominators_and_empty_windows_are_not_zero():
    rows = [dict(fgm=1, fga=2, game_id="a"), dict(fgm=9, fga=None, game_id="b")]
    metrics = {m["key"]: m for m in context_metrics(rows, rows[:1])}
    assert metrics["fg_pct"]["value"] == 50
    assert metrics["fg_pct"]["valid_games"] == 1
    assert metrics["fg_pct"]["missing_component_games"] == 1
    assert metrics["fg_pct"]["change"] is None
    assert all(m["value"] is None for m in context_metrics([], []))
    assert all(
        m["change"] is None for m in context_metrics(rows, rows, baseline_covered=False)
    )


def game_rows():
    rows = []
    for day in range(1, 8):
        for team, opponent, offset, pts in [
            ("AAA", "BBB", 0, 20),
            ("BBB", "AAA", 10, 18),
        ]:
            for player in range(1, 6):
                rows.append(
                    dict(
                        season="2025-26",
                        season_type="Regular Season",
                        game_id=f"g{day}",
                        game_date=f"2026-01-{day:02}",
                        player_id=offset + player,
                        team_abbr=team,
                        opponent_abbr=opponent,
                        pts=pts,
                        ast=2,
                        min=48,
                        fgm=8,
                        fga=16,
                        fg3m=2,
                    )
                )
    return rows


def report(status="Out", stamp="2026-01-05T20:00:00+00:00"):
    return dict(
        season="2025-26",
        player_id=99,
        team_abbr="AAA",
        game_date="2026-01-06",
        matchup="AAA@BBB",
        injury_status=status,
        reason="Example report",
        report_timestamp_utc=stamp,
        ingested_at_utc="2026-04-01T00:00:00+00:00",
        source_url="https://example.com/injury",
    )


def test_dbt_mart_preserves_grain_and_excludes_same_game_future_and_late_reports():
    rows = game_rows()
    reports = [report(), report("Available", "2026-01-06T01:00:00+00:00")]
    with sqlite3.connect(":memory:") as db:
        audit = build_context(db, rows, reports)
        assert audit["context_rows"] == len(rows)
        r = dict(
            db.execute(
                "select * from player_game_context where game_id='g6' and player_id=1"
            ).fetchone()
        )
        assert r["opponent_prior_win_pct"] == 0
        assert r["opponent_prior_efg_allowed_pct"] == pytest.approx(56.25)
        assert r["opponent_latest_prior_game_date"] == "2026-01-05"
        status = dict(
            db.execute("select * from player_game_reported_status").fetchone()
        )
        assert status["injury_status"] == "Out"
        assert status["ingested_at_utc"].startswith("2026-04")
        assert r["prior_day_reported_status"] == "Unknown"
    altered = deepcopy(rows)
    for row in altered:
        if row["game_id"] in ("g6", "g7"):
            row["pts"] = 1000
    with sqlite3.connect(":memory:") as db:
        build_context(db, altered, reports)
        value = db.execute(
            "select opponent_prior_win_pct from player_game_context where game_id='g6' and player_id=1"
        ).fetchone()[0]
        assert value == r["opponent_prior_win_pct"]


def test_missing_shooting_does_not_erase_win_context():
    rows = game_rows()
    rows[0]["fgm"] = None
    with sqlite3.connect(":memory:") as db:
        build_context(db, rows, [])
        r = dict(
            db.execute(
                "select * from player_game_context where game_id='g6' and player_id=1"
            ).fetchone()
        )
        assert r["opponent_prior_win_pct"] == 0
        assert r["opponent_prior_efg_allowed_pct"] is None


def test_conflicting_status_reports_and_wrong_matchup_do_not_become_out():
    wrong = {**report(), "matchup": "AAA@CCC", "player_id": 98}
    with sqlite3.connect(":memory:") as db:
        build_context(db, game_rows(), [report(), report("Available"), wrong])
        statuses = [
            dict(r) for r in db.execute("select * from player_game_reported_status")
        ]
        assert len(statuses) == 1
        assert statuses[0]["injury_status"] == "Unknown"
        assert statuses[0]["conflicting_reports"] == 1


def test_duplicate_source_grain_fails_closed():
    rows = game_rows()
    with (
        sqlite3.connect(":memory:") as db,
        pytest.raises(ValueError, match="audit failed"),
    ):
        build_context(db, rows + [rows[0]], [])


def test_teammate_no_row_is_unknown_and_out_plus_played_is_conflict():
    case = dict(
        player_id=1,
        seasons=["2025-26"],
        phase="Regular Season",
        previous_start="2026-01-01",
        previous_end="2026-01-05",
        start="2026-01-06",
        end="2026-01-07",
    )
    rows = game_rows()
    result = teammate_comparison(rows, [], case, 99)
    assert result["periods"]["current"]["groups"]["unknown"]["games"] == 2
    status = {**report(), "game_id": "g6", "conflicting_reports": 0}
    result = teammate_comparison(rows, [status], case, 99)
    assert (
        result["periods"]["current"]["groups"]["reported_out_no_appearance"]["games"]
        == 1
    )
    result = teammate_comparison(rows, [{**status, "player_id": 2}], case, 2)
    assert result["periods"]["current"]["groups"]["conflicting"]["games"] == 1
    assert result["periods"]["current"]["groups"]["participated"]["games"] == 1


def test_no_phase_or_season_leakage():
    rows = game_rows()
    for row in rows:
        if row["game_id"] == "g6":
            row["season_type"] = "Playoffs"
    with sqlite3.connect(":memory:") as db:
        build_context(db, rows, [])
        r = dict(
            db.execute(
                "select * from player_game_context where game_id='g6' and player_id=1"
            ).fetchone()
        )
        assert r["opponent_prior_games"] == 0
        assert r["opponent_prior_win_pct"] is None


def test_team_minutes_allow_individual_rounding_but_reject_missing_rotation():
    rows = game_rows()
    # Six fictional appearances with rounded minutes can sum to 243.
    for row in rows:
        row["min"] = 40
    for day in range(1, 8):
        for team, player in [("AAA", 6), ("BBB", 16)]:
            template = next(
                r for r in rows if r["game_id"] == f"g{day}" and r["team_abbr"] == team
            )
            rows.append({**template, "player_id": player, "min": 43})
    with sqlite3.connect(":memory:") as db:
        build_context(db, rows, [])
        assert (
            db.execute("select min(box_coverage_ok) from team_game_context").fetchone()[
                0
            ]
            == 1
        )
    rows[0]["min"] = 20
    with sqlite3.connect(":memory:") as db:
        build_context(db, rows, [])
        assert (
            db.execute(
                "select box_coverage_ok from team_game_context where game_id='g1' and team_abbr='AAA'"
            ).fetchone()[0]
            == 0
        )
