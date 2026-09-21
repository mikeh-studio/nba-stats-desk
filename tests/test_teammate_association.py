"""Check temporal covariates, sample accounting and known adjusted effects."""

import numpy as np
import pytest

pytest.importorskip("statsmodels")
from app.agent.teammate_readiness import build_panel  # noqa: E402
from scripts.teammate_association import (  # noqa: E402
    analyze,
    fit,
    prepare_rows,
    report_burden,
)
from tests.test_teammate_readiness import fixture  # noqa: E402


def game_and_report():
    args = fixture()
    game = build_panel(*args)[1]
    row = {**args[1][0], "player_id": 3, "reason": "Injury/Illness - Knee"}
    return game, row


def test_burden_does_not_count_focal_target_or_g_league_as_other_injury():
    game, row = game_and_report()
    reports = [
        row,
        {**row, "player_id": 1},
        {**row, "player_id": 2},
        {**row, "player_id": 4, "reason": "G League - Two-Way"},
    ]
    assert report_burden(game, reports)["value"] == 1
    assert report_burden(game, [])["value"] is None


def test_burden_uses_whole_latest_bulletin_without_carrying_old_listings():
    game, row = game_and_report()
    later = {
        **row,
        "player_id": 4,
        "injury_status": "Available",
        "report_timestamp_utc": "2025-11-02T21:00:00Z",
    }
    assert report_burden(game, [row, later])["value"] == 0
    late = {**row, "report_timestamp_utc": "2025-11-02T23:00:00Z"}
    assert report_burden(game, [late])["value"] is None
    wrong = {**row, "matchup": "BOS@BKN"}
    assert report_burden(game, [wrong])["value"] is None


def test_burden_conflict_unknown_identity_and_missing_reason_stay_missing():
    game, row = game_and_report()
    assert (
        report_burden(game, [row, {**row, "injury_status": "Available"}])["value"]
        is None
    )
    assert report_burden(game, [{**row, "player_id": None}])["value"] is None
    assert report_burden(game, [{**row, "reason": None}])["value"] is None
    assert report_burden(game, [row, row])["value"] == 1


def synthetic():
    rng = np.random.default_rng(123)
    rows = []
    for i in range(120):
        out = int(i % 4 < 2)
        opp = float(rng.uniform(25, 75))
        rest = int(rng.integers(0, 4))
        home = int(rng.integers(0, 2))
        burden = int(rng.integers(0, 4))
        month = "2025-11" if i < 60 else "2025-12"
        ast = (
            4
            + 2 * out
            + 0.02 * opp
            + 0.3 * rest
            + 0.7 * home
            - 0.2 * burden
            + int(month == "2025-12")
        )
        rows.append(
            dict(
                out=out,
                ast=ast,
                min=30,
                opponent_prior_win_pct=opp,
                rest_days=rest,
                home=home,
                other_reported_injury_out=burden,
                month=month,
                episode_id=i // 2,
            )
        )
    return rows


def test_known_adjusted_effect_and_cluster_reference():
    rows = synthetic()
    result = fit(rows)
    assert result["adjusted_difference"] == pytest.approx(2)
    assert result["validated_significance"] is None
    assert result["n"] == 120
    assert result["episodes"] == 60
    assert fit(rows, "per36")["adjusted_difference"] == pytest.approx(2.4)


def test_rank_deficient_small_and_one_sided_samples_do_not_fit():
    rows = synthetic()
    assert fit(rows[:5])["status"] == "not_estimable"
    assert fit([r for r in rows if r["out"]])["status"] == "not_estimable"
    for r in rows:
        r["home"] = 1
    assert fit(rows)["status"] == "not_estimable"


def test_nonfinite_values_fail_loudly():
    rows = synthetic()
    rows[0]["ast"] = float("nan")
    with pytest.raises(ValueError, match="Non-finite"):
        fit(rows)


def test_missing_covariates_are_excluded_without_imputation():
    args = fixture()
    panel = build_panel(*args)
    result = analyze(panel, args[1])
    assert not result["complete_case_game_ids"]
    assert len(result["excluded"]) == 2
    assert result["primary"]["status"] == "not_estimable"
    assert result["validated_significance"] is None


def test_capture_lookup_includes_nonappearance_players_and_digraph(monkeypatch):
    from scripts import capture_teammate_reports as capture

    monkeypatch.setattr(
        capture.pipeline.players,
        "get_players",
        lambda: [
            {"id": 3, "full_name": "Nikola Đurišić"},
            {"id": 4, "full_name": "Eli John Ndiaye"},
        ],
    )
    lookup = capture.capture_player_lookup(
        [{"player_id": 1, "player_name": "Example Player"}]
    )
    assert lookup["nikola djurisic"] == 3
    assert lookup["eli john ndiaye"] == 4
    assert lookup["example player"] == 1


def test_analysis_rejects_a_different_availability_snapshot():
    args = fixture()
    panel = build_panel(*args)
    args[1][0]["injury_status"] = "Available"
    with pytest.raises(ValueError, match="exposure conflicts"):
        analyze(panel, args[1])


def test_analysis_preserves_short_report_age_for_exposure_and_burden():
    args = fixture()
    args[-1]["max_report_age_hours"] = 12
    old = {
        **args[1][0],
        "game_date": "2025-11-01",
        "report_timestamp_utc": "2025-11-01T10:00:00Z",
    }
    args[1].extend([old, {**old, "player_id": 3}])
    panel = build_panel(*args)
    assert panel[0]["exposure"] == "participated"
    rows = prepare_rows(panel, args[1])
    assert rows[0]["out"] == 0
    assert rows[0]["other_reported_injury_out"] is None
    assert rows[0]["burden_evidence"]["reason"] == "no_eligible_team_report"


def test_analysis_includes_reports_at_the_configured_age_boundary():
    args = fixture()
    args[-1]["max_report_age_hours"] = 12
    args[1][0]["report_timestamp_utc"] = "2025-11-02T11:00:00Z"
    args[1].append({**args[1][0], "player_id": 3})
    panel = build_panel(*args)
    row = prepare_rows(panel, args[1])[1]
    assert row["out"] == 1
    assert row["other_reported_injury_out"] == 1


@pytest.mark.parametrize("age", [None, 0, 49, True, float("nan"), 12])
def test_analysis_rejects_missing_invalid_or_mixed_age_policies(age):
    args = fixture()
    panel = build_panel(*args)
    panel[0]["max_report_age_hours"] = age
    with pytest.raises(ValueError, match="report-age policy"):
        analyze(panel, args[1])
