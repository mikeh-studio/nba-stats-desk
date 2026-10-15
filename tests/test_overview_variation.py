"""Independent intent, quartile and dated-peak regression examples."""

from copy import deepcopy

import pytest
from app.agent.metric_distributions import distribution
from app.agent.overview_variation import variation
from app.agent.performance_overview import build_overview, overview_scope
from app.agent.semantics import Evidence, load_contract
from tests.test_overview_positions import fixture


def metric(values, key="reb"):
    rows = [
        dict(
            game_id=f"g{i:04}",
            game_date=f"2024-11-{i + 1:02}",
            opponent_abbr="XYZ",
            **{key: value},
        )
        for i, value in enumerate(values)
    ]
    return dict(
        key=key,
        value=4,
        missing_component_games=sum(v is None for v in values),
        distribution=distribution(load_contract().metric(key), {}, rows, 1),
    )


@pytest.mark.parametrize("key", ["pts", "reb", "ast", "stl", "blk"])
def test_variation_tracks_metric_with_known_quartiles_and_dated_high(key):
    v = variation(metric([0, 2, 4, 8, 16], key))
    assert (v["summary"]["q1"], v["summary"]["median"], v["summary"]["q3"]) == (2, 4, 8)
    assert v["highest"]["value"] == 16 and v["highest"]["game_id"] == "g0004"
    assert "2024-11-05 against XYZ" in v["peak_text"]
    assert "best" not in v["text"]
    if key != "pts":
        assert "points" not in v["text"]


def test_ties_choose_earliest_date_without_claiming_unique_high():
    v = variation(metric([8, 0, 8, 2, 4]))
    assert v["highest"]["date"] == "2024-11-01" and v["highest"]["tied_games"] == 2
    assert "first of 2 games" in v["peak_text"]


def test_missing_and_tiny_samples_do_not_claim_representative_spread():
    assert variation(metric([2, 4])) is None
    assert variation(metric([0, 2, None, 8, 16])) is None
    m = metric([0, 2, 4, 8, 16])
    m["distribution"]["across_games"]["observed_games"] = 6
    assert variation(m) is None


def test_flat_values_have_no_arbitrarily_selected_standout():
    v = variation(metric([4] * 5))
    assert v["summary"]["q1"] == v["summary"]["q3"] == 4 and v["peak_text"] == ""


def test_peak_uses_full_rows_not_latest_300_dots():
    from datetime import date, timedelta

    rows = [
        dict(
            game_id=str(i),
            game_date=str(date(2023, 1, 1) + timedelta(days=i)),
            reb=50 if i == 0 else 1,
        )
        for i in range(350)
    ]
    d = distribution(load_contract().metric("reb"), {}, rows, 1)["across_games"]
    assert d["points_truncated"] and all(p["value"] == 1 for p in d["points"])
    assert (
        d["highest"]["value"] == d["summary"]["max"] == 50
        and d["highest"]["game_id"] == "0"
    )


def test_ratio_highs_are_not_promoted_from_tiny_attempt_games():
    rows = [
        dict(game_id=str(i), game_date="2024-11-01", fgm=1, fga=1) for i in range(5)
    ]
    d = distribution(load_contract().metric("fg_pct"), {}, rows, 1)
    assert d["across_games"]["highest"] is None
    assert variation(dict(key="fg_pct", value=100, distribution=d)) is None


def overview(profile="rebounding", word="wings", position="SF", mapping=True):
    grouped, c = fixture(30, position)
    for record in c["records"][20:]:
        record["position"] = "PG" if position != "PG" else "C"
    from app.agent.historical_positions import digest

    c["sha256"] = digest(c)
    for row, value in zip(grouped[1], [0, 2, 4, 8, 16], strict=True):
        row.update(reb=value, ast=value, pts=value, min=30, opponent_abbr="XYZ")
    rows = [r for rs in grouped.values() for r in rs]
    for phase, season, day in [
        ("Playoffs", "2024-25", "2025-05-01"),
        ("Regular Season", "2023-24", "2023-11-01"),
    ]:
        for i in range(5):
            row = deepcopy(grouped[1][0])
            row.update(
                game_id=f"other-{phase}-{i}",
                season=season,
                season_type=phase,
                game_date=day,
                reb=100,
                ast=100,
                pts=100,
            )
            rows.append(row)
    coverage = frozenset((r["season"], r["season_type"]) for r in rows)
    e = Evidence(
        rows,
        coverage,
        "fictional",
        "hash",
        {k: "2025-06-30" for k in coverage},
        complete=True,
        position_context=c if mapping else None,
    )
    q = f"How did Example 1's {profile} compare with other {word} in the 2024-25 regular season?"
    return build_overview(
        q, e, [dict(player_id=1, player_name="Example 1")], overview_scope(q, "2024-25")
    )


@pytest.mark.parametrize(
    "profile,word,position,key",
    [
        ("rebounding", "wings", "SF", "reb"),
        ("playmaking", "big men", "C", "ast"),
        ("scoring", "guards", "PG", "pts"),
    ],
)
def test_comparison_first_and_spread_use_exact_requested_group_and_scope(
    profile, word, position, key
):
    out = overview(profile, word, position)
    ev = out["semantic_evidence"]
    first = out["answer"].split(".")[0]
    assert (
        "ranked" in first
        and ("bigs/centers" if word == "big men" else word) in first
        and "2024-25" in first
    )
    assert "rose" not in out["answer"] and "fell" not in out["answer"]
    assert "baseline" not in out["answer"] and "league-wide" not in out["answer"]
    assert "20 qualifying" in first and "10.5 peer median" in out["answer"]
    assert "median was 4" in out["answer"] and "from 2 to 8" in out["answer"]
    assert "2024-11-05 against XYZ" in out["answer"] and "100" not in out["answer"]
    assert ev["request_fulfillment"]["status"] == "complete"
    assert ev["insights"][0]["game_variation"]["metric_key"] == key
    assert ev["presentation"]["takeaways"][0]["text"].startswith("He ranked")
    p = next(m for m in ev["metrics"] if m["key"] == key)["peer_comparison"]
    assert p["cohort_size"] == p["per36"]["cohort_size"] == 20


def test_missing_membership_is_partial_and_never_substitutes_league():
    out = overview(mapping=False)
    ev = out["semantic_evidence"]
    assert ev["request_fulfillment"]["status"] == "partial"
    assert out["answer"].startswith("The requested wing comparison is not available")
    assert "league-wide" not in out["answer"] and "exceeded" not in out["answer"]
    assert "rose" not in out["answer"] and "fell" not in out["answer"]
    assert "median was 4" in out["answer"]
    assert [t["area"] for t in ev["presentation"]["takeaways"]] == [
        "Variation",
        "Standout",
    ]
    p = next(m for m in ev["metrics"] if m["key"] == "reb")["peer_comparison"]
    assert p["kind"] == "unavailable" and p["cohort_size"] == 0
    assert p["distribution"]["among_players"]["summary"]["n"] == 0
    from app.agent.overview_tracking import overview_events

    assert any(
        e["kind"] == "request_fulfillment" and e["fulfillment_status"] == "partial"
        for e in overview_events(out)
    )
