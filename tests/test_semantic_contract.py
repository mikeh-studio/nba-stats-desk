"""Independent SQL and adversarial checks for the proposed semantic contract."""

from __future__ import annotations

import copy
import json
import sqlite3
from dataclasses import replace

import pytest
from app.agent.semantics import (
    Evidence,
    Query,
    SemanticError,
    load_contract,
    pregame_evidence,
    run_query,
)
from scripts.evaluate_semantics import DEFAULT_FIXTURE, evaluate


@pytest.fixture
def evidence():
    fixture = json.loads(DEFAULT_FIXTURE.read_text())
    return Evidence(
        fixture["rows"],
        frozenset((s, p) for s, p, _ in fixture["coverage"]),
        "synthetic/player_games",
        "frozen-test-snapshot",
        {(s, p): d for s, p, d in fixture["coverage"]},
        complete=True,
    )


def test_all_fixture_cases_pass():
    report = evaluate()
    assert report["total"] == report["passed"] == 24
    assert len({r["id"] for r in report["results"]}) == 24
    assert report["llm_runs"] == report["historical_reference_cases"] == 0
    assert report["human_reviewed"] is False


@pytest.mark.parametrize(
    "metric, aggregation, expression",
    [
        ("pts", "average", "SUM(pts) * 1.0 / COUNT(*)"),
        ("pts", "total", "SUM(pts)"),
        ("fg_pct", "ratio", "SUM(fgm) * 1.0 / NULLIF(SUM(fga), 0)"),
        ("ts_pct", "ratio", "SUM(pts) / NULLIF(2 * (SUM(fga) + .44 * SUM(fta)), 0)"),
    ],
)
def test_period_results_match_independent_sql(
    evidence, metric, aggregation, expression
):
    # Explicit oracle SQL uses source components directly, never contract
    # formulas, helpers, or generated SQL from the system under test.
    with sqlite3.connect(":memory:") as db:
        db.execute(
            "CREATE TABLE games (season, phase, player_id, day, pts, fgm, fga, fta)"
        )
        db.executemany(
            "INSERT INTO games VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                tuple(
                    r[k]
                    for k in (
                        "season",
                        "season_type",
                        "player_id",
                        "game_date",
                        "pts",
                        "fgm",
                        "fga",
                        "fta",
                    )
                )
                for r in evidence.rows
            ],
        )
        expected = dict(
            db.execute(
                f"SELECT player_id, {expression} FROM games WHERE season = ? AND phase = ? AND day <= ? GROUP BY player_id",
                ("2024-25", "Regular Season", "2025-01-07"),
            )
        )
    result = run_query(evidence, Query(metric, "2024-25", aggregation))
    assert {r["player_id"]: r["value"] for r in result["rows"]} == pytest.approx(
        expected
    )


def test_last_and_prior_windows_are_disjoint(evidence):
    base = Query("pts", "2024-25", "average", player_id=1, n=5, window="last_n_games")
    current = run_query(evidence, base)["rows"][0]
    prior = run_query(evidence, replace(base, window="prior_n_games"))["rows"][0]
    assert current["game_ids"] == ["007", "006", "005", "004", "003"]
    assert prior["game_ids"] == ["002", "001"]
    assert not set(current["game_ids"]) & set(prior["game_ids"])


@pytest.mark.parametrize(
    "window, as_of, start, end",
    [
        ("last_week", "2025-01-08", "2024-12-30", "2025-01-05"),
        ("last_month", "2025-03-15", "2025-02-01", "2025-02-28"),
        ("last_month", "2024-03-15", "2024-02-01", "2024-02-29"),
    ],
)
def test_calendar_boundaries(evidence, window, as_of, start, end):
    result = run_query(
        evidence, Query("pts", "2024-25", "average", window=window, as_of=as_of)
    )
    assert result["scope"]["window_start"] == start
    assert result["scope"]["window_end"] == end


def test_trailing_window_uses_shared_anchor_not_player_last_seen(evidence):
    result = run_query(
        evidence,
        Query("pts", "2024-25", "average", player_id=2, window="last_n_days", n=2),
    )
    assert result["scope"]["as_of"] == "2025-01-07"
    assert result["rows"] == []


@pytest.mark.parametrize(
    "anchor,n,start",
    [
        ("2025-01-07", 12, "2024-01-08"),
        ("2024-03-31", 1, "2024-03-01"),
        ("2025-03-31", 1, "2025-03-01"),
        ("2024-02-29", 12, "2023-03-01"),
    ],
)
def test_trailing_calendar_month_boundaries(evidence, anchor, n, start):
    result = run_query(
        evidence,
        Query("pts", "2024-25", "average", window="last_n_months", n=n, as_of=anchor),
    )
    assert result["scope"]["window_start"] == start
    assert result["scope"]["window_end"] == anchor


@pytest.mark.parametrize("n", [None, 0, -1, True, 1.5, 100000])
def test_invalid_month_window_is_rejected(evidence, n):
    with pytest.raises(SemanticError):
        run_query(
            evidence, Query("pts", "2024-25", "average", window="last_n_months", n=n)
        )


def test_month_answer_discloses_selected_season_scope(evidence):
    from app.agent.semantic_answer import render_answer

    result = run_query(
        evidence,
        Query("pts", "2024-25", "average", player_id=2, window="last_n_months", n=12),
    )
    payload = render_answer(
        {"status": "ok", "evidence": result, "resolved_queries": []}, []
    )
    assert "Past 12 calendar months: 2024-01-08 through 2025-01-07" in payload["answer"]
    assert "Includes only 2024-25 Regular Season games" in payload["answer"]


def test_ranking_membership_matches_independent_sql(evidence):
    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE TABLE games (season, phase, player_id, pts, fga, fta)")
        db.executemany(
            "INSERT INTO games VALUES (?, ?, ?, ?, ?, ?)",
            [
                tuple(
                    r[k]
                    for k in ("season", "season_type", "player_id", "pts", "fga", "fta")
                )
                for r in evidence.rows
            ],
        )
        expected = list(
            db.execute("""SELECT player_id, SUM(pts) / (2 * (SUM(fga) + .44 * SUM(fta)))
            FROM games WHERE season = '2024-25' AND phase = 'Regular Season'
            GROUP BY player_id HAVING COUNT(*) >= 5 AND SUM(fga) >= 70""")
        )
    result = run_query(
        evidence, Query("ts_pct", "2024-25", "ratio", operation="rank", min_attempts=70)
    )
    assert [(r["player_id"], r["value"]) for r in result["rows"]] == expected


def test_rank_and_percentile_ties_direction_and_target_filter(evidence):
    rows = copy.deepcopy(evidence.rows)
    for r in rows:
        if r["player_id"] == 3:
            r["tov"] = 3
    ev = replace(evidence, rows=rows)
    q = Query("tov", "2024-25", "average", operation="rank", min_games=1)
    low = run_query(ev, q)
    assert [(r["player_id"], r["rank"], r["percentile"]) for r in low["rows"]] == [
        (1, 1, 50),
        (2, 1, 50),
        (3, 3, 0),
    ]
    high = run_query(ev, replace(q, direction="higher", player_id=3))
    assert high["rows"][0]["rank"] == 1
    assert high["rows"][0]["percentile"] == 100
    assert high["cohort"]["size"] == 3  # Target filter must not shrink the cohort.


def test_ineligible_player_is_explained_for_percentile(evidence):
    result = run_query(
        evidence,
        Query("pts", "2024-25", "average", operation="percentile", player_id=2),
    )
    row = result["rows"][0]
    assert row["eligible"] is False
    assert row["rank"] is row["percentile"] is None
    assert "insufficient_games" in row["exclusion_reasons"]


def test_missing_components_excluded_from_rank_not_zero_filled(evidence):
    rows = copy.deepcopy(evidence.rows)
    rows[0]["pts"] = None
    ev = replace(evidence, rows=rows)
    result = run_query(ev, Query("pts", "2024-25", "average", operation="rank"))
    assert result["status"] == "empty_cohort"
    assert result["rows"] == []
    summary = run_query(ev, Query("pts", "2024-25", "average", player_id=1))["rows"][0]
    assert summary["value"] == 10
    assert summary["missing_component_games"] == 1


@pytest.mark.parametrize(
    "patch, code",
    [
        ({"game_id": 1}, "invalid_data"),
        ({"player_id": None}, "invalid_data"),
        ({"fga": -1}, "invalid_data"),
        ({"fgm": 11}, "invalid_data"),
        ({"pts": float("nan")}, "invalid_data"),
        ({"pts": "oops"}, "invalid_data"),
        ({"game_date": "2026-01-01"}, "invalid_evidence"),
    ],
)
def test_invalid_source_evidence_is_blocked(evidence, patch, code):
    rows = copy.deepcopy(evidence.rows)
    rows[0].update(patch)
    with pytest.raises(SemanticError) as exc:
        run_query(replace(evidence, rows=rows), Query("pts", "2024-25", "average"))
    assert exc.value.code == code


@pytest.mark.parametrize(
    "changes",
    [
        {"n": 0, "window": "last_n_days"},
        {"n": 3},
        {"limit": 0},
        {"min_games": -1},
        {"min_games": True},
        {"direction": "sideways"},
        {"as_of": "bad"},
        {"start_date": "2025-01-01"},
        {"window": "date_range", "start_date": "2025-02-01", "as_of": "2025-01-01"},
    ],
)
def test_invalid_scope_rejected(evidence, changes):
    with pytest.raises(SemanticError) as exc:
        run_query(evidence, replace(Query("pts", "2024-25", "average"), **changes))
    assert exc.value.code == "invalid_scope"


def test_uncovered_archive_never_falls_back(evidence):
    with pytest.raises(SemanticError) as exc:
        run_query(evidence, Query("pts", "2025-26", "average"))
    assert exc.value.code == "unsupported_coverage"


def test_catalog_excludes_unapproved_composites_and_lists_operations():
    contract = load_contract()
    assert "category_score_6cat" not in contract.metrics
    assert contract.metric("fg_pct").aggregations == ("ratio",)
    assert (
        contract.metric("fantasy_proxy_weighted").numerator
        != contract.metric("fantasy_points_simple").numerator
    )
    for metric in contract.metrics.values():
        assert metric.public()["operations"] == [
            "summary",
            "rank",
            "percentile",
            "game_log",
        ]


def test_known_scoring_formulas_are_distinct(evidence):
    simple = run_query(
        evidence, Query("fantasy_points_simple", "2024-25", "average", player_id=1)
    )
    weighted = run_query(
        evidence, Query("fantasy_proxy_weighted", "2024-25", "average", player_id=1)
    )
    assert simple["rows"][0]["value"] == 15
    assert weighted["rows"][0]["value"] == pytest.approx(19.9)


def test_pregame_evidence_requires_timezone_and_no_future_leak():
    reports = json.loads(DEFAULT_FIXTURE.read_text())["reports"]
    kwargs = dict(
        season="2024-25", player_id=1, game_date="2025-01-07", matchup="AAA@BBB"
    )
    assert pregame_evidence(reports, **kwargs, tipoff=None)["status"] == "unverified"
    with pytest.raises(SemanticError):
        pregame_evidence(reports, **kwargs, tipoff="2025-01-07T19:00:00")
    assert (
        pregame_evidence(reports, **kwargs, tipoff="2025-01-07T19:00:00-05:00")[
            "report"
        ]["injury_status"]
        == "Out"
    )
    conflict = dict(reports[0], injury_status="Available")
    with pytest.raises(SemanticError) as exc:
        pregame_evidence(
            [*reports, conflict], **kwargs, tipoff="2025-01-07T19:00:00-05:00"
        )
    assert exc.value.code == "ambiguous_evidence"


def test_evaluator_reports_mismatch_instead_of_claiming_success(tmp_path):
    fixture = json.loads(DEFAULT_FIXTURE.read_text())
    fixture["cases"][3]["expect"]["rows.0.value"] = 0.5
    path = tmp_path / "wrong.json"
    path.write_text(json.dumps(fixture))
    result = evaluate(path)
    assert result["passed"] == result["total"] - 1
    assert result["results"][3]["mismatches"]


def test_incomplete_source_snapshot_is_rejected(evidence):
    with pytest.raises(SemanticError) as exc:
        run_query(replace(evidence, complete=False), Query("pts", "2024-25", "average"))
    assert exc.value.code == "incomplete_evidence"


@pytest.mark.parametrize(
    "alias", ["Fantasy Score", "fantasy_points", "fantasy", "fantasy scoring"]
)
def test_fantasy_aliases_default_to_weighted(alias, evidence):
    result = run_query(evidence, Query(alias, "2024-25", "average", player_id=1))
    assert result["metric"]["key"] == "fantasy_proxy_weighted"
    assert result["rows"][0]["value"] == pytest.approx(19.9)


@pytest.mark.parametrize(
    "window,ids", [("last_n_games", ["006", "007"]), ("prior_n_games", ["004", "005"])]
)
def test_game_log_windows_are_chronological(evidence, window, ids):
    result = run_query(
        evidence,
        Query(
            "pts",
            "2024-25",
            "total",
            operation="game_log",
            player_id=1,
            window=window,
            n=2,
        ),
    )
    assert [r["game_id"] for r in result["rows"]] == ids
    assert result["displayed_games"] == result["observed_games"] == 2
    assert "cohort" not in result


def test_game_log_display_limit_discloses_full_scope(evidence):
    result = run_query(
        evidence,
        Query("pts", "2024-25", "total", operation="game_log", player_id=1, limit=2),
    )
    assert result["observed_games"] == 7
    assert result["displayed_games"] == 2
    assert "display_limit_applied_latest_games" in result["warnings"]
    assert [r["game_id"] for r in result["rows"]] == ["006", "007"]


def test_game_log_ratios_need_no_ranking_threshold(evidence):
    result = run_query(
        evidence, Query("fg_pct", "2024-25", "ratio", operation="game_log", player_id=2)
    )
    assert [r["value"] for r in result["rows"]] == [1, 0]
    assert result["rows"][0]["denominator"] == 1
    assert result["rows"][1]["denominator"] == 9


@pytest.mark.parametrize(
    "fields,code",
    [
        ({}, "clarification_required"),
        ({"player_id": 1, "min_games": 5}, "invalid_scope"),
        ({"player_id": 1, "direction": "higher"}, "invalid_scope"),
    ],
)
def test_game_log_rejects_missing_identity_and_ranking_policy(evidence, fields, code):
    with pytest.raises(SemanticError) as exc:
        run_query(
            evidence, Query("pts", "2024-25", "total", operation="game_log", **fields)
        )
    assert exc.value.code == code


def test_game_log_phase_team_and_partial_window(evidence):
    result = run_query(
        evidence,
        Query(
            "pts",
            "2024-25",
            "total",
            operation="game_log",
            player_id=1,
            team_abbr="AAA",
            window="last_n_games",
            n=5,
        ),
    )
    assert [r["game_id"] for r in result["rows"]] == ["001", "002", "003"]
    assert "partial_window" in result["warnings"]
    assert all(r["season_type"] == "Regular Season" for r in result["rows"])


def test_query_phase_default_comes_from_contract(monkeypatch):
    from app.agent import semantics

    contract = replace(load_contract(), default_phase="Playoffs")
    monkeypatch.setattr(semantics, "load_contract", lambda: contract)
    assert Query("pts", "2024-25", "average").season_type == "Playoffs"
    assert Query("pts", "2024-25", "average", season_type="Both").season_type == "Both"
