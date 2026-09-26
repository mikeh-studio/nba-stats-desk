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
