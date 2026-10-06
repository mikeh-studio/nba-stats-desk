import pytest
from app.agent.overview_summary import summary_paragraphs


def metric(key, value, change=None, missing=0):
    return {
        "key": key,
        "label": {
            "pts": "Points",
            "reb": "Rebounds",
            "ast": "Assists",
            "min": "MIN",
            "ts_pct": "TS%",
        }[key],
        "value": value,
        "change": change,
        "missing_component_games": missing,
    }


def summarize(metrics, appearances=43):
    return summary_paragraphs(
        "Example Player", metrics, {"phases": ["Regular Season"]}, appearances
    )


def test_three_paragraph_assessment_keeps_scoring_when_rebounds_change():
    paragraphs = summarize(
        [
            metric("pts", 26.6, 0.01),
            metric("reb", 3.6, 0.3),
            metric("ast", 4.7, -1.3),
            metric("min", 31, -1.2),
            metric("ts_pct", 63.7, 1.9),
        ]
    )
    assert len(paragraphs) == 3
    assert "26.6 points and 4.7 assists" in paragraphs[0]
    assert "63.7%" in paragraphs[0]
    assert "rebounds" not in paragraphs[0]
    assert "scoring stayed broadly similar" in paragraphs[1]
    assert "true shooting rose 1.9 percentage points" in paragraphs[1]
    assert "Minutes per game fell 1.2" in paragraphs[1]
    assert "43 regular-season appearances" in paragraphs[0]
    assert "Complete game-level scoring is unavailable" in paragraphs[2]


@pytest.mark.parametrize(
    "change,phrase",
    [
        (None, "comparison with the previous period is unavailable"),
        (0.01, "broadly similar"),
        (-2, "scoring fell 2.0 per game"),
    ],
)
def test_missing_stable_and_negative_baselines(change, phrase):
    text = " ".join(summarize([metric("pts", 20, change)]))
    assert phrase in text
    assert "improved" not in text


@pytest.mark.parametrize(
    "value,change,missing", [(None, None, 0), (62, None, 0), (62, 3, 1)]
)
def test_efficiency_claim_requires_complete_comparable_data(value, change, missing):
    text = " ".join(
        summarize([metric("pts", 20, 2), metric("ts_pct", value, change, missing)])
    )
    assert "true shooting rose" not in text
    assert "true shooting fell" not in text
    if missing or value is None:
        assert "True shooting was" not in text


def test_missing_primary_uses_complete_metric_and_discloses_partial_data():
    text = " ".join(summarize([metric("pts", 20, None, 1), metric("reb", 8, 2)]))
    assert "incomplete scoring" in text
    assert "20.0 points" not in text
    assert "Partial data for Points" in text


def test_zero_appearances_and_thin_sample_are_explicit():
    assert "No recorded appearances" in summarize([], 0)[0]
    assert "small sample" in " ".join(summarize([metric("pts", 20)], 2))
    text = " ".join(summarize([metric("pts", None)], 2))
    assert "incomplete scoring" in text
    assert "0.0" not in text


def test_game_facts_and_prose_reconcile_with_outlier_and_tied_peak():
    from app.agent.overview_summary import game_insights

    rows = [
        dict(pts=p, game_id=str(i), game_date=f"2026-06-{i + 1:02}")
        for i, p in enumerate([0, 10, 20, 50, 50])
    ]
    facts = game_insights(rows)
    assert facts == dict(
        observed_games=5,
        valid_games=5,
        median_points=20,
        min_points=0,
        max_points=50,
        peak_date="2026-06-05",
        peak_game_id="4",
        peak_ties=2,
        threshold=20,
        threshold_games=3,
    )
    paragraphs = summary_paragraphs(
        "Example Player", [metric("pts", 26)], {"phases": ["Playoffs"]}, 5, facts
    )
    assert "3 of 5 appearances" in paragraphs[2]
    assert "median of 20.0" in paragraphs[2]
    assert "One of the highest-scoring games" in paragraphs[2]
    assert len(" ".join(paragraphs).split()) <= 180


def test_incomplete_game_scoring_withholds_distribution():
    from app.agent.overview_summary import game_insights

    assert game_insights([{"pts": 30}, {"pts": None}]) == {
        "observed_games": 2,
        "valid_games": 1,
    }
