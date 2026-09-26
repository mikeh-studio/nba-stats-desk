"""Inference and Ask claims must agree, including weak and missing evidence."""

from copy import deepcopy

import pytest
from app.agent.research_ask import wants_research_followup
from app.research import CORE_METRICS
from app.research_insights import assess
from app.research_studies import PAIRS, pending, study_answer
from scripts.research_uncertainty import correct_families, observed_uncertainty


def panel(effect=4):
    return [
        dict(
            included=True,
            episode_id=e,
            game_id=f"{e}-{i}",
            game_date=f"2025-11-{e + 1:02}",
            exposure="reported_out_no_appearance" if e % 2 else "participated",
            outcomes={"pts": 20 + effect * (e % 2) + (e % 3 - 1) + i / 2},
        )
        for e in range(12)
        for i in range(3)
    ]


def test_episode_uncertainty_refuses_pseudoreplication_and_missing_values():
    rows = panel()
    result = observed_uncertainty(rows, "pts")
    assert result["interval"][0] < 4 < result["interval"][1]
    assert result["p_value"] < 0.05
    assert result["arm_episodes"] == [6, 6]
    for row in rows:
        row["episode_id"] = int(row["exposure"] != "participated")
    assert observed_uncertainty(rows, "pts")["reason"] == "too_few_independent_episodes"
    rows[0]["outcomes"]["pts"] = None
    assert (
        observed_uncertainty(rows, "pts")["reason"]
        == "missing_outcomes_or_exposure_arm"
    )


def test_null_signed_effect_and_fixed_families():
    null = observed_uncertainty(panel(0), "pts")
    negative = observed_uncertainty(panel(-4), "pts")
    assert null["interval"][0] < 0 < null["interval"][1]
    assert negative["interval"][1] < 0
    m = {
        "metric": "pts",
        "observed_uncertainty": {"p_value": 0.01},
        "causal": {"p_value": 0.001},
    }
    correct_families([{"metrics": [m]}], CORE_METRICS, 3)
    assert m["observed_uncertainty"]["holm_p_value"] == pytest.approx(0.27)
    assert m["causal"]["holm_p_value"] == pytest.approx(0.027)
    assert m["causal"]["family_size"] == 27


def metric():
    return dict(
        metric="pts",
        label="PTS",
        descriptive={"difference": 4},
        observed_uncertainty=observed_uncertainty(panel(), "pts"),
        association=None,
        causal=None,
    )


def test_uncorrected_p_never_becomes_significance_and_no_false_causality():
    m = metric()
    a = assess(m)
    assert a["significance"] == "insufficient evidence for significance"
    assert a["claim"] == "observed association"
    m["observed_uncertainty"]["holm_p_value"] = 0.1
    assert assess(m)["significance"] == "inconclusive"
    m["observed_uncertainty"]["holm_p_value"] = 0.01
    assert assess(m)["significance"] == "statistically supported (exploratory)"
    m["diagnostics"] = {"same_sample_raw_difference": 4, "leave_episode_out": []}
    m["association"] = {"adjusted_difference": -1}
    assert assess(m)["adjustment_reverses_same_sample_direction"]


@pytest.mark.parametrize("pair", PAIRS)
def test_all_pairs_answer_requested_metric_even_if_unavailable(pair):
    study = pending(pair)
    study["metrics"][0] = metric()
    answer = study_answer(study, ["plus_minus"])
    assert len(answer["tables"][0]["rows"]) == 1
    assert answer["research_highlights"][0]["estimate"] is None
    assert "unavailable" in answer["answer"]
    assert study_answer(deepcopy(study))["research_highlights"][0]["metric"] == "pts"


@pytest.mark.parametrize(
    "question",
    [
        "Is that significant?",
        "Is it representative?",
        "Is that statistically supported?",
        "Does one absence episode explain it?",
        "What about plus-minus?",
    ],
)
def test_inference_followups_keep_study_route(question):
    assert wants_research_followup(question)


def test_weak_samples_do_not_promote_extreme_rare_stats():
    study = pending(PAIRS[0])
    for m in study["metrics"]:
        m["descriptive"] = {"difference": 100 if m["metric"] == "stl" else 1}
    answer = study_answer(study)
    assert [m["metric"] for m in answer["research_highlights"]] == ["pts", "ast", "reb"]


def test_causal_stability_uses_causal_refits_not_observed_omissions():
    m = metric()
    m["causal"] = dict(
        estimate=3,
        claim_level="causal_estimate_under_assumptions",
        interval=[1, 5],
        holm_p_value=0.03,
        n=80,
        episodes=16,
        leave_episode_out_refits=[{"estimate": None}],
    )
    a = assess(m)
    assert a["claim"] == "causal estimate under stated assumptions"
    assert a["stability"] == "causal refit stability not established"
    assert a["causal_n"] == 80
    m["causal"]["leave_episode_out_refits"] = [{"estimate": 2}, {"estimate": -1}]
    assert assess(m)["stability"] == "causal direction changes across episode refits"


def presentation_study():
    study = pending(PAIRS[0])
    study["scope"] = dict(season="2025-26", start="2025-10-22", end="2025-12-31")
    for key, both, out in [("pts", 21, 15.5), ("ast", 6.2, 8.47), ("reb", 5, 4)]:
        m = next(m for m in study["metrics"] if m["metric"] == key)
        m["descriptive"] = dict(
            participated=both,
            reported_out=out,
            difference=out - both,
            groups=[{"observed_games": 13}, {"observed_games": 2}],
        )
    return study


def test_presenter_answer_counts_once_and_diagnostics_only_in_backend(caplog):
    import json
    import logging

    study = presentation_study()
    original = deepcopy(study)
    with caplog.at_level(logging.INFO, logger="app.research_studies"):
        result = study_answer(study, ["pts", "ast", "reb"])
    answer = result["answer"]
    assert answer.startswith("LeBron James scored less and recorded more assists")
    assert answer.count("13 games when both played and 2 with Luka Doncic out") == 1
    assert (
        "5.5 fewer points, 2.3 more assists and 1.0 fewer rebounds per game" in answer
    )
    assert "early observation" in answer
    assert "2025-10-22 through 2025-12-31" in answer
    visible = json.dumps(result).lower()
    for diagnostic in (
        "insufficient evidence",
        "holm",
        "p-value",
        "product threshold",
        "episode",
        "research_assessments",
    ):
        assert diagnostic not in visible
    assert "insufficient evidence for significance" in caplog.text
    assert study == original
    assert result["tables"][0]["rows"][0][1:4] == ["21.0", "15.5", "-5.5"]
    assert result["tables"][0]["columns"][1]["label"] == "Both played"


def test_presentation_never_substitutes_causal_estimate_for_observed_difference():
    study = presentation_study()
    points = study["metrics"][0]
    points["causal"] = dict(
        estimate=99,
        claim_level="causal_estimate_under_assumptions",
        interval=[90, 110],
        holm_p_value=0.01,
    )
    result = study_answer(study, ["pts"])
    assert "5.5 fewer points" in result["answer"]
    assert result["research_highlights"][0]["estimate"] == -5.5
    assert "99" not in result["answer"]
    assert assess(points)["estimate"] == 99


def test_different_or_missing_game_counts_are_not_presented_as_shared():
    study = presentation_study()
    assists = next(m for m in study["metrics"] if m["metric"] == "ast")
    assists["descriptive"]["groups"][1]["observed_games"] = 1
    result = study_answer(study, ["pts", "ast"])
    assert "Across 13" not in result["answer"]
    assert "Game coverage differs" in result["answer"]
    study["metrics"][0]["descriptive"]["groups"] = []
    result = study_answer(study, ["pts"])
    assert "Across" not in result["answer"]


def test_units_zero_and_turnovers_do_not_become_improvement_claims():
    study = presentation_study()
    tov = next(m for m in study["metrics"] if m["metric"] == "tov")
    tov["descriptive"] = dict(difference=1.2)
    result = study_answer(study, ["tov"])
    assert "1.2 more turnovers per game" in result["answer"]
    assert "improv" not in result["answer"]
    tov["descriptive"]["difference"] = 0
    assert "roughly the same turnovers" in study_answer(study, ["tov"])["answer"]
    fg = next(m for m in study["metrics"] if m["metric"] == "fg_pct")
    fg.update(descriptive=dict(difference=2.7), unit="percentage points", label="FG%")
    assert "2.7 percentage points" in study_answer(study, ["fg_pct"])["answer"]


def test_missing_descriptive_values_do_not_displace_available_highlights():
    study = presentation_study()
    points = study["metrics"][0]
    points["descriptive"] = None
    points["causal"] = dict(
        estimate=99,
        claim_level="causal_estimate_under_assumptions",
        interval=[90, 110],
        holm_p_value=0.01,
    )
    answer = study_answer(study, ["pts", "ast", "reb"])
    assert answer["research_highlights"][0]["metric"] == "ast"
    assert "2.3 more assists" in answer["answer"]
    assert "PTS is unavailable" in answer["answer"]


def test_near_zero_table_difference_has_no_negative_zero():
    study = presentation_study()
    study["metrics"][0]["descriptive"]["difference"] = -0.01
    assert study_answer(study, ["pts"])["tables"][0]["rows"][0][3] == "0.0"


def test_full_season_rendering_discloses_unclassified_games():
    study = presentation_study()
    study["scope"].update(phase="Both", end="2026-06-13", window="full_season")
    study["coverage"] = {"excluded_focal_appearances": 1}
    answer = study_answer(study)
    assert "regular season and playoffs" in answer["answer"]
    assert "excluded from this comparison" in answer["answer"]
    assert "insufficient evidence" not in answer["answer"].lower()
