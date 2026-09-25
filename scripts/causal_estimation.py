"""Offline cross-fitted AIPW with explicit identification and support gates.

The gate cannot verify unmeasured-confounding assumptions. Human review and
source evidence remain necessary; diagnostic success is not proof of causality.
"""

from __future__ import annotations

import math

import numpy as np
from app.agent.teammate_readiness import utc
from scipy.stats import t as student_t
from sklearn.linear_model import LinearRegression, LogisticRegression
from sklearn.model_selection import StratifiedGroupKFold
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

ASSUMPTIONS = (
    "consistency",
    "conditional_exchangeability",
    "positivity",
    "interference_scope",
    "selection_and_missingness",
)


def estimate(rows, spec, metric):
    unavailable = {
        "status": "not_identified",
        "claim_level": "insufficient_evidence",
        "estimate": None,
        "interval": None,
        "p_value": None,
    }
    if not spec or spec.get("version") != 1:
        return {**unavailable, "reason": "reviewed_causal_specification_required"}
    if (
        not all(
            isinstance(spec.get("assumptions", {}).get(k), str)
            and spec["assumptions"][k].strip()
            for k in ASSUMPTIONS
        )
        or not spec.get("reviewed_by")
        or not spec.get("source_references")
    ):
        return {**unavailable, "reason": "identification_assumptions_or_review_missing"}
    covariates = spec.get("covariates", [])
    if not covariates or any(
        c in ("min", metric, "same_game_minutes") for c in covariates
    ):
        return {**unavailable, "reason": "invalid_pre_exposure_covariates"}
    required_gates = (
        "min_episodes",
        "min_arm_episodes",
        "min_effective_sample",
        "propensity_min",
        "max_weighted_smd",
    )
    gates = spec.get("gates", {})
    if any(k not in gates for k in required_gates) or not (
        gates["min_episodes"] >= 8
        and gates["min_arm_episodes"] >= 3
        and gates["min_effective_sample"] >= 5
        and 0 < gates["propensity_min"] < 0.5
        and 0 < gates["max_weighted_smd"] <= 0.25
    ):
        return {**unavailable, "reason": "explicit_support_gates_required"}
    valid, excluded = [], []
    seen = set()
    for row in rows:
        if row["game_id"] in seen:
            raise ValueError("Duplicate causal game")
        seen.add(row["game_id"])
        if (
            row.get("treatment") not in (0, 1)
            or not row.get("eligibility_verified")
            or not row.get("exposure_verified")
            or not row.get("outcome_complete")
        ):
            excluded.append(
                {
                    "game_id": row["game_id"],
                    "reason": "eligibility_exposure_or_outcome_unverified",
                }
            )
            continue
        stamps = row.get("covariate_available_at", {})
        if any(
            c not in stamps or utc(stamps[c]) >= utc(row["exposure_at"])
            for c in covariates
        ):
            return {
                **unavailable,
                "reason": "post_exposure_or_unknown_covariate_timing",
            }
        if utc(row["eligibility_at"]) > utc(row["exposure_at"]) or utc(
            row["exposure_at"]
        ) >= utc(row["outcome_at"]):
            return {**unavailable, "reason": "invalid_eligibility_or_exposure_timing"}
        vals = [
            row.get("outcomes", {}).get(metric),
            *[row.get("covariates", {}).get(c) for c in covariates],
        ]
        if any(
            v is None
            or isinstance(v, bool)
            or not isinstance(v, (int, float))
            or not math.isfinite(v)
            for v in vals
        ):
            excluded.append(
                {"game_id": row["game_id"], "reason": "missing_metric_or_covariate"}
            )
            continue
        valid.append(row)
    if excluded:
        # A missingness model is not implemented; do not change the estimand by
        # silently restricting the causal analysis to complete cases.
        return {
            **unavailable,
            "reason": "incomplete_eligible_panel",
            "excluded": excluded,
        }
    episodes = {r["episode_id"] for r in valid}
    arms = [{r["episode_id"] for r in valid if r["treatment"] == arm} for arm in (0, 1)]
    if len(episodes) < gates["min_episodes"] or any(
        len(a) < gates["min_arm_episodes"] for a in arms
    ):
        return {
            **unavailable,
            "status": "not_estimable",
            "reason": "insufficient_independent_episodes",
            "episodes": len(episodes),
        }
    x = np.array([[r["covariates"][c] for c in covariates] for r in valid], dtype=float)
    y = np.array([r["outcomes"][metric] for r in valid], dtype=float)
    treatment = np.array([r["treatment"] for r in valid])
    group = np.array([str(r["episode_id"]) for r in valid])
    propensity, m0, m1 = (np.zeros(len(valid)) for _ in range(3))
    for train, test in StratifiedGroupKFold(
        n_splits=2, shuffle=True, random_state=0
    ).split(x, treatment, group):
        if set(treatment[train]) != {0, 1} or any(
            sum(treatment[train] == arm) <= x.shape[1] + 1 for arm in (0, 1)
        ):
            return {
                **unavailable,
                "status": "not_estimable",
                "reason": "unsupported_cross_fit_fold",
            }
        classifier = make_pipeline(
            StandardScaler(), LogisticRegression(random_state=0, max_iter=2000)
        )
        classifier.fit(x[train], treatment[train])
        propensity[test] = classifier.predict_proba(x[test])[:, 1]
        for arm, prediction in ((0, m0), (1, m1)):
            selected = train[treatment[train] == arm]
            model = LinearRegression().fit(x[selected], y[selected])
            prediction[test] = model.predict(x[test])
    bound = gates["propensity_min"]
    if np.any(propensity < bound) or np.any(propensity > 1 - bound):
        return {
            **unavailable,
            "reason": "insufficient_propensity_overlap",
            "propensity_range": [float(propensity.min()), float(propensity.max())],
        }
    weights = [1 / (1 - propensity[treatment == 0]), 1 / propensity[treatment == 1]]
    effective = [float(w.sum() ** 2 / (w @ w)) for w in weights]
    smd = []
    for j in range(x.shape[1]):
        means = [
            np.average(x[treatment == arm, j], weights=weights[arm]) for arm in (0, 1)
        ]
        scale = np.sqrt(sum(np.var(x[treatment == arm, j]) for arm in (0, 1)) / 2)
        smd.append(
            float(abs(means[1] - means[0]) / scale)
            if scale
            else (0.0 if means[0] == means[1] else float("inf"))
        )
    if (
        min(effective) < gates["min_effective_sample"]
        or max(smd) > gates["max_weighted_smd"]
    ):
        return {
            **unavailable,
            "reason": "effective_sample_or_balance_gate_failed",
            "effective_sample": effective,
        }
    scores = (
        m1
        - m0
        + treatment * (y - m1) / propensity
        - (1 - treatment) * (y - m0) / (1 - propensity)
    )
    effect = float(scores.mean())
    cluster_sums = np.array(
        [(scores[group == g] - effect).sum() for g in sorted(set(group))]
    )
    count = len(cluster_sums)
    se = float(
        np.sqrt(count / (count - 1) * (cluster_sums @ cluster_sums)) / len(valid)
    )
    if not math.isfinite(se) or se <= 0:
        return {
            **unavailable,
            "status": "not_estimable",
            "reason": "degenerate_uncertainty",
        }
    critical = float(student_t.ppf(0.975, count - 1))
    return {
        "status": "estimated",
        "claim_level": "causal_estimate_under_assumptions",
        "estimate": effect,
        "interval": [effect - critical * se, effect + critical * se],
        "interval_label": "Pointwise 95% episode-cluster interval",
        "p_value": float(2 * student_t.sf(abs(effect / se), count - 1)),
        "n": len(valid),
        "episodes": count,
        "effective_sample": effective,
        "weighted_smd": dict(zip(covariates, smd)),
        "leave_episode_out_score_range": [
            float(min(scores[group != g].mean() for g in set(group))),
            float(max(scores[group != g].mean() for g in set(group))),
        ],
        "sensitivity_method": "Leave episode out of fixed cross-fitted scores; nuisance models are not refitted",
        "assumptions": spec["assumptions"],
        "limitations": [
            "Assumption-conditional estimate; diagnostic gates cannot establish absence of unmeasured confounding.",
            "Uncertainty is approximate and depends on the declared episode independence.",
        ],
    }
