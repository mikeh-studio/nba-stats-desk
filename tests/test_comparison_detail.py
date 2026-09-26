"""Comparison denominators, missingness, and public projection."""

from copy import deepcopy

import pytest
from app.agent.comparison_detail import comparison_detail, relative_change


def metric(key="pts", a=10.04, b=15.06):
    return dict(
        metric=key,
        descriptive=dict(groups=[dict(value=a), dict(value=b)], difference=b - a),
    )


def test_relative_uses_unrounded_values_and_refuses_bad_baselines():
    assert relative_change(metric()) == pytest.approx(50)
    for m in [
        metric(a=0),
        metric(a=-2),
        metric("plus_minus"),
        dict(**metric(), unit="percentage points"),
    ]:
        assert relative_change(m) is None
    missing = metric()
    missing["descriptive"]["groups"][0]["missing_component_games"] = 1
    assert relative_change(missing) is None


def study():
    rows = []
    for i, (pts, minutes, group) in enumerate(
        [
            (10, 10, "participated"),
            (30, 20, "participated"),
            (20, 30, "reported_out_no_appearance"),
        ]
    ):
        rows.append(
            dict(
                included=True,
                game_id=str(i),
                game_date=f"2025-11-0{i + 1}",
                season_type="Regular Season",
                opponent_abbr="ABC",
                exposure=group,
                private_note="secret",
                outcomes=dict(game_id=str(i), pts=pts, min=minutes),
            )
        )
    return dict(metrics=[metric()], panel=rows)


def test_per36_is_ratio_of_totals_and_projection_is_public():
    s = study()
    result = comparison_detail(s)
    assert result["rates"]["pts"][0]["value"] == pytest.approx(48)
    assert result["rates"]["pts"][1]["value"] == pytest.approx(24)
    assert set(result["games"][0]) == {
        "game_id",
        "date",
        "phase",
        "opponent",
        "group",
        "values",
    }
    assert result["games"][0]["values"] == {"pts": 10}
    assert len(result["games"]) == 3
    s["panel"][0]["outcomes"]["min"] = None
    assert comparison_detail(s)["rates"]["pts"][0]["value"] is None
    s = study()
    s["panel"][0]["outcomes"]["pts"] = None
    assert comparison_detail(s)["games"][0]["values"]["pts"] is None
    assert comparison_detail(s)["rates"]["pts"][0]["value"] is None


def test_excluded_games_never_enter_rates_or_distribution():
    s = study()
    excluded = deepcopy(s["panel"][0])
    excluded.update(included=False, game_id="excluded")
    excluded["outcomes"]["pts"] = 999
    s["panel"].append(excluded)
    assert len(comparison_detail(s)["games"]) == 3
    assert comparison_detail(s)["rates"]["pts"][0]["value"] == 48


def test_roster_window_exclusions_are_explicit_context():
    s = study()
    s["panel"].append(
        dict(included=False, focal_participated=True, eligibility="not_teammates")
    )
    assert comparison_detail(s)["outside_teammate_window"] == 1
