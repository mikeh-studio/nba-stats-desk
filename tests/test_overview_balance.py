"""Independent content expectations: tables alone do not satisfy an overview."""

import pytest
from app.agent.overview_policy import focus
from app.agent.overview_summary import summary_paragraphs
from app.agent.performance_overview import wants_overview


@pytest.fixture
def metrics():
    # Hand-chosen full-precision observations, not renderer-derived expectations.
    values = dict(pts=24, ast=6, tov=3, reb=4, stl=1.2, blk=0, min=32, ts_pct=60)
    changes = dict(pts=0, ast=-1, tov=1, reb=2, stl=0.1, blk=0, min=2, ts_pct=1)
    return [
        dict(
            key=key,
            label=key,
            value=value,
            change=changes[key],
            display_change=changes[key],
            valid_games=8,
            observed_games=8,
            missing_component_games=0,
            sample=dict(status="descriptive", comparison_eligible=True),
        )
        for key, value in values.items()
    ]


def render(metrics, profile="general", appearances=8):
    return summary_paragraphs(
        "Example Player",
        metrics,
        {
            "phases": ["Playoffs"],
            "comparison_label": "the same season's regular season",
        },
        appearances,
        profile=profile,
    )


@pytest.mark.parametrize(
    "question,profile",
    [
        ("How did Example Player contribute beyond scoring?", "contribution"),
        ("How has Example Player contributed beyond scoring?", "contribution"),
        ("Example Player's non-scoring stats", "contribution"),
        ("Give me Example Player's non scoring statistics", "contribution"),
        ("Give me an overview of Example Player besides scoring", "contribution"),
        ("Give me an overview of Example Player other than scoring", "contribution"),
        ("Give me an all-around assessment of Example Player", "general"),
        ("Give me an all-round assessment of Example Player", "general"),
        ("Give me an overview of Example Player's scoring", "scoring"),
        ("How was Example Player's playmaking?", "playmaking"),
        ("How did Example Player defend?", "defense"),
        ("How was Example Player's rebounding?", "rebounding"),
    ],
)
def test_intent_and_overview_route(question, profile):
    assert focus(question) == profile
    assert wants_overview(question)


@pytest.mark.parametrize(
    "question",
    [
        "Tell me only Example Player's points per game",
        "What about his rebounds and turnovers?",
        "How did Example Player's scoring and rebounding change?",
        "Give me an overview of Example Player's playmaking and defense",
        "Example Player non-scoring stats at home",
        "Example Player non-scoring stats per 36 minutes",
    ],
)
def test_explicit_metrics_and_constraints_are_not_erased(question):
    assert not wants_overview(question)


def test_contribution_contains_creation_without_scoring(metrics):
    text = " ".join(render(metrics, "contribution"))
    assert "6.0 assists" in text and "Turnovers rose 1.0 to 3.0 per game" in text
    assert "Rebounds rose 2.0 to 4.0 per game" in text
    assert "24.0 points" not in text and "True shooting" not in text
    assert "Screens" not in text and "positioning" not in text


@pytest.mark.parametrize("ast,tov", [(1, 1), (-1, -1), (1, -1), (-1, 1)])
def test_creation_quadrants(metrics, ast, tov):
    for m in metrics:
        if m["key"] in ("ast", "tov"):
            m["display_change"] = ast if m["key"] == "ast" else tov
    text = " ".join(render(metrics))
    assert f"assists {'rose' if ast > 0 else 'fell'} 1.0" in text
    assert f"Turnovers {'rose' if tov > 0 else 'fell'} 1.0" in text
    assert "caused" not in text


@pytest.mark.parametrize("key", ["ast", "tov"])
def test_partial_pair_never_claims_full_sample(metrics, key):
    m = next(m for m in metrics if m["key"] == key)
    value = m["value"]
    m.update(missing_component_games=2, valid_games=6)
    text = " ".join(render(metrics))
    assert f"{value:.1f} {'assists' if key == 'ast' else 'turnovers'}" not in text
    assert (
        "Turnovers rose 1.0 to 3.0 per game" if key == "ast" else "6.0 assists"
    ) in text


@pytest.mark.parametrize("kind", ["limited", "baseline", "suppressed"])
def test_raw_changes_cannot_escape_suppression(metrics, kind):
    for m in metrics:
        m["change"] = 100
        if kind == "suppressed":
            m["display_change"] = None
        else:
            m["sample"]["comparison_eligible"] = False
    text = " ".join(render(metrics))
    assert "rose" not in text and "fell" not in text
    assert "6.0 assists" in text and "100.0" not in text


def test_input_order_and_missing_scoring(metrics):
    next(m for m in metrics if m["key"] == "pts")["value"] = None
    assert render(metrics) == render(list(reversed(metrics)))
    text = " ".join(render(metrics))
    assert "24.0 points" not in text and "6.0 assists" in text
