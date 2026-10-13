from copy import deepcopy

import pytest
from app.agent.context_metrics import context_metrics
from app.agent.overview_percentiles import add_percentiles


def groups():
    return {
        pid: [
            dict(
                game_id=str(day),
                ast=2,
                pts=2 * fgm + 3 + ftm,
                fgm=fgm,
                fga=20,
                fg3m=3,
                fg3a=10,
                ftm=ftm,
                fta=10,
                tov=pid,
                min=pid * 10,
            )
            for day in range(5)
        ]
        for pid, fgm, ftm in [(1, 8, 6), (2, 10, 8), (3, 12, 10)]
    }


def ranked(grouped, pid=2):
    records = context_metrics(grouped[pid], [])
    add_percentiles(records, grouped, pid, 5)
    return {r["key"]: r for r in records}


def test_ratios_counts_and_direction_use_independent_expected_ranks():
    middle = ranked(groups())
    for key in ("fg_pct", "ft_pct", "ts_pct", "min", "tov"):
        assert middle[key]["percentile"] == 50
        assert middle[key]["cohort_size"] == 3
    low = ranked(groups(), 1)
    assert low["tov"]["percentile"] == 100
    assert low["min"]["percentile"] == 0
    assert low["fg_pct"]["percentile"] == 0
    assert low["ft_pct"]["percentile"] == 0
    assert low["fg3m"]["percentile"] == 0  # All tied; strict-worse convention.
    assert low["fg3_pct"]["percentile"] == 0


@pytest.mark.parametrize(
    "key,field,below,boundary",
    [
        ("fg_pct", "fga", 19, 20),
        ("ts_pct", "fga", 19, 20),
        ("ft_pct", "fta", 9, 10),
        ("fg3_pct", "fg3a", 9, 10),
    ],
)
def test_exact_attempt_qualification_boundary(key, field, below, boundary):
    grouped = groups()
    for r in grouped[2]:
        r[field] = below
    result = ranked(grouped)[key]
    assert result["percentile"] is None
    assert result["percentile_status"] == "low_attempts"
    assert result["cohort_size"] == 2
    for r in grouped[2]:
        r[field] = boundary
    assert ranked(grouped)[key]["percentile_status"] == "ranked"


def test_percentiles_use_pooled_attempts_not_average_game_percentages():
    grouped = groups()
    for r, makes, attempts in zip(grouped[2], [1, 29, 0, 0, 0], [1, 99, 0, 0, 0]):
        r.update(fgm=makes, fga=attempts)
    result = ranked(grouped)["fg_pct"]
    assert result["value"] == pytest.approx(30)
    assert result["percentile"] == 0  # Peers shoot 40% and 60%.


def test_tied_ratios_keep_same_percentile_with_different_volumes():
    grouped = groups()
    for r in grouped[3]:
        r.update(fgm=20, fga=40)
    result = ranked(grouped)
    assert result["fg_pct"]["value"] == 50
    assert result["fg_pct"]["percentile"] == 50
    assert ranked(grouped, 3)["fg_pct"]["percentile"] == 50


def test_zero_attempts_partial_components_and_small_cohorts_withhold_rank():
    grouped = groups()
    for r in grouped[2]:
        r.update(ftm=0, fta=0)
    result = ranked(grouped)["ft_pct"]
    assert result["value"] is None and result["percentile"] is None
    assert result["percentile_status"] == "unavailable"
    grouped = groups()
    grouped[2][0]["fta"] = None
    assert ranked(grouped)["ft_pct"]["percentile_status"] == "incomplete_data"
    grouped = groups()
    for pid in (1, 3):
        for r in grouped[pid]:
            r["fga"] = 1
    result = ranked(grouped)["fg_pct"]
    assert result["cohort_size"] == 1 and result["percentile_status"] == "too_few_peers"
    assert result["percentile"] is None


def test_fewer_than_five_games_cannot_rank_even_with_many_attempts():
    grouped = groups()
    grouped[2] = grouped[2][:4]
    for r in grouped[2]:
        r["fga"] = 100
    assert ranked(grouped)["fg_pct"]["percentile_status"] == "insufficient_games"


def test_peer_partial_data_is_excluded_without_mutating_source():
    grouped = groups()
    grouped[1][0]["fgm"] = None
    original = deepcopy(grouped)
    result = ranked(grouped)["fg_pct"]
    assert result["cohort_size"] == 2 and result["percentile"] == 0
    assert grouped == original
