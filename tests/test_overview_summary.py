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
            "tov": "Turnovers",
            "stl": "Steals",
            "blk": "Blocks",
        }[key],
        "value": value,
        "change": change,
        "missing_component_games": missing,
    }


def summarize(metrics, appearances=43):
    return summary_paragraphs(
        "Example Player", metrics, {"phases": ["Regular Season"]}, appearances
    )


def test_selective_assessment_explains_non_scoring_change():
    paragraphs = summarize(
        [metric("pts", 26.6, 0.01), metric("ast", 4.7, -1.3), metric("tov", 2.7, 0.3)]
    )
    text = " ".join(paragraphs)
    assert "26.6 points" in text and "broadly in line with his baseline" in text
    assert "Turnovers rose 0.3" in text
    assert "assists fell 1.3" in text
    assert "43 regular-season appearances" in text
    assert len(paragraphs) == 3


@pytest.mark.parametrize("value,missing", [(None, 0), (62, 1)])
def test_incomplete_efficiency_is_not_reported(value, missing):
    text = " ".join(
        summarize([metric("pts", 20, 2), metric("ts_pct", value, 3, missing)])
    )
    assert "True shooting moved" not in text


def test_missing_primary_retains_complete_rebounding():
    text = " ".join(summarize([metric("pts", 20, None, 1), metric("reb", 8, 2)]))
    assert "20.0 points" not in text and "Rebounds rose 2.0 to 8.0 per game" in text
    assert "incomplete" not in text


def test_no_appearances_and_thin_sample():
    assert "No recorded appearances" in summarize([], 0)[0]
    assert "small sample" in " ".join(summarize([metric("pts", 20)], 2))
    assert "0.0" not in " ".join(summarize([metric("pts", None)], 2))


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
        "Example Player",
        [metric("pts", 26)],
        {"phases": ["Playoffs"]},
        5,
        facts,
        profile="scoring",
    )
    # Range/peak evidence remains inspectable; the focused narrative no longer
    # repeats a fixed third paragraph of game facts.
    assert len(paragraphs) == 2
    assert "26.0 points per game" in paragraphs[0]
    assert paragraphs[-1].startswith("Scope:")


def test_incomplete_game_scoring_withholds_distribution():
    from app.agent.overview_summary import game_insights

    assert game_insights([{"pts": 30}, {"pts": None}]) == {
        "observed_games": 2,
        "valid_games": 1,
    }
