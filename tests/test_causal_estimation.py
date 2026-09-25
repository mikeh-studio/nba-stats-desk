"""Known-effect simulation and gates, separate from real NBA identification."""

from copy import deepcopy

import numpy as np
from scripts.causal_estimation import ASSUMPTIONS, estimate


def study(effect=3):
    rng = np.random.default_rng(23)
    rows = []
    for episode in range(80):
        treatment = episode % 2
        for game in range(4):
            x = float(game)
            rows.append(
                {
                    "game_id": f"{episode}-{game}",
                    "episode_id": episode,
                    "treatment": treatment,
                    "eligibility_verified": True,
                    "exposure_verified": True,
                    "outcome_complete": True,
                    "eligibility_at": "2025-01-01T00:00:00Z",
                    "exposure_at": "2025-01-02T00:00:00Z",
                    "outcome_at": "2025-01-03T00:00:00Z",
                    "covariate_available_at": {"prior_role": "2025-01-01T00:00:00Z"},
                    "covariates": {"prior_role": x},
                    "outcomes": {
                        "ast": float(5 + effect * treatment + 0.3 * x + rng.normal())
                    },
                }
            )
    spec = {
        "version": 1,
        "covariates": ["prior_role"],
        "assumptions": dict.fromkeys(
            ASSUMPTIONS,
            "Synthetic randomized treatment with known data-generating process",
        ),
        "reviewed_by": "fixture",
        "source_references": ["synthetic"],
        "gates": {
            "min_episodes": 12,
            "min_arm_episodes": 4,
            "min_effective_sample": 10,
            "propensity_min": 0.05,
            "max_weighted_smd": 0.2,
        },
    }
    return rows, spec


def test_known_effect_and_null_effect():
    for effect in (0, 3):
        rows, spec = study(effect)
        result = estimate(rows, spec, "ast")
        assert result["status"] == "estimated", result
        assert abs(result["estimate"] - effect) < 0.3
        assert result["interval"][0] < effect < result["interval"][1]


def test_no_review_late_covariates_selection_and_sparse_episodes():
    rows, spec = study()
    assert estimate(rows, None, "ast")["status"] == "not_identified"
    assert estimate(rows[:8], spec, "ast")["status"] == "not_estimable"
    late = deepcopy(rows)
    late[0]["covariate_available_at"]["prior_role"] = late[0]["outcome_at"]
    assert (
        estimate(late, spec, "ast")["reason"]
        == "post_exposure_or_unknown_covariate_timing"
    )
    incomplete = deepcopy(rows)
    incomplete[0]["outcomes"]["ast"] = None
    assert estimate(incomplete, spec, "ast")["reason"] == "incomplete_eligible_panel"
