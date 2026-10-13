import pytest
from app.agent.context_metrics import context_metrics
from app.agent.metric_distributions import describe, distribution, histogram
from app.agent.overview_percentiles import add_percentiles
from app.agent.semantics import load_contract
from tests.test_overview_percentiles import groups


def test_quantiles_have_independent_expected_values():
    assert describe([4, 1, 3, 2]) == {
        "n": 4,
        "min": 1,
        "q1": 1.75,
        "median": 2.5,
        "q3": 3.25,
        "max": 4,
    }
    assert describe([])["median"] is None
    assert describe([0])["q1"] == 0


@pytest.mark.parametrize(
    "values",
    [
        [],
        [0],
        [0] * 50,
        [1, 2, 3, 4],
        [-2, -1, 0, 12],
        [0, 0, 1, 2, 40],
        [0, 73],
        list(range(12)),
    ],
)
def test_histogram_preserves_every_observation_including_maximum(values):
    for discrete in (False, True):
        bins = histogram(values, discrete)
        assert sum(b["count"] for b in bins) == len(values)
        assert all(b["end"] > b["start"] for b in bins)
        if values:
            assert bins[-1]["count"] >= 1
        if discrete and values and max(values) - min(values) <= 40:
            assert all(b["end"] - b["start"] == 1 for b in bins)
        assert len(bins) <= 41


def test_game_distribution_preserves_recorded_zero_events():
    metric = load_contract().metric("stl")
    rows = [
        {"game_id": str(i), "game_date": f"2025-01-{i + 1:02}", "stl": v}
        for i, v in enumerate([0, 0, 1, 2, None])
    ]
    d = distribution(metric, {}, rows, 1)["across_games"]
    assert d["summary"]["n"] == 4 and d["summary"]["median"] == 0.5
    assert d["missing_games"] == 1 and d["zero_denominator_games"] == 0
    assert sum(b["count"] for b in d["bins"]) == 4
    assert [p["value"] for p in d["points"]] == [0, 0, 1, 2]


def test_shooting_game_median_is_separate_from_pooled_percentage():
    rows = [
        {"game_id": str(i), "game_date": f"2025-01-{i + 1:02}", "ftm": m, "fta": a}
        for i, (m, a) in enumerate([(1, 1), (0, 9), (0, 0), (0, 0), (None, 5)])
    ]
    records = context_metrics(rows, [])
    ft = next(r for r in records if r["key"] == "ft_pct")
    add_percentiles([ft], {1: rows}, 1, 5)
    assert ft["value"] == 10
    games = ft["distribution"]["across_games"]
    assert games["summary"]["median"] == 50 and games["summary"]["n"] == 2
    assert (
        games["observed_games"] == 5
        and games["zero_denominator_games"] == 2
        and games["missing_games"] == 1
    )
    assert [p["attempts"] for p in games["points"]] == [1, 9]
    assert ft["distribution"]["among_players"]["summary"]["n"] == 0


def test_among_players_uses_exact_eligible_percentile_cohort():
    grouped = groups()
    for r in grouped[1]:
        r["fga"] = 1
    records = context_metrics(grouped[2], [])
    add_percentiles(records, grouped, 2, 5)
    fg = next(r for r in records if r["key"] == "fg_pct")
    peer = fg["distribution"]["among_players"]
    assert peer["summary"]["n"] == fg["cohort_size"] == 2
    assert peer["summary"]["median"] == 55 and peer["summary"]["q1"] == 52.5
    assert peer["focal_in_cohort"] is True
    assert sum(b["count"] for b in peer["bins"]) == 2
    records = context_metrics(grouped[1], [])
    add_percentiles(records, grouped, 1, 5)
    fg = next(r for r in records if r["key"] == "fg_pct")
    assert fg["distribution"]["among_players"]["focal_in_cohort"] is False
    assert fg["percentile"] is None


def test_dot_payload_is_bounded_but_statistics_and_bins_use_full_sample():
    metric = load_contract().metric("pts")
    rows = [
        {"game_id": str(i), "pts": i, "game_date": "2025-01-01"} for i in range(350)
    ]
    d = distribution(metric, {}, rows, 1)["across_games"]
    assert len(d["points"]) == d["displayed_games"] == 300
    assert d["points_truncated"] is True
    assert d["summary"]["n"] == 350 and d["summary"]["median"] == 174.5
    assert sum(b["count"] for b in d["bins"]) == 350


def test_histogram_membership_matches_returned_boundaries_at_float_edges():
    values = [i * (1 / 12) for i in range(13)]
    bins = histogram(values)
    for i, b in enumerate(bins):
        expected = sum(
            b["start"] <= v < b["end"] or (i == len(bins) - 1 and v == b["end"])
            for v in values
        )
        assert b["count"] == expected
    assert bins[-1]["end"] == max(values)
