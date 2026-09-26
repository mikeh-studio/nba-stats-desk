"""Exploratory episode-cluster uncertainty for frozen observed contrasts."""

import math
from collections import Counter
from statistics import mean

from scipy.stats import t


def observed_uncertainty(panel, metric):
    rows = [r for r in panel if r["included"]]
    groups = [
        [r for r in rows if r["exposure"] == exposure]
        for exposure in ("participated", "reported_out_no_appearance")
    ]
    counts = [len(g) for g in groups]
    episodes = [Counter(r["episode_id"] for r in g) for g in groups]
    result = {
        "status": "insufficient_evidence",
        "interval": None,
        "p_value": None,
        "group_n": counts,
        "arm_episodes": [len(e) for e in episodes],
        "missing_n": sum(r["outcomes"].get(metric) is None for r in rows),
        "largest_episode_share": max(
            (max(e.values()) / n for e, n in zip(episodes, counts) if n), default=None
        ),
        "excluded_games": len(panel) - len(rows),
        "target": "Focal appearances with verified exposure in this study window",
        "interval_label": "Pointwise 95% episode-cluster interval (exploratory)",
        "representativeness": "Not established beyond the study window and observed appearances",
    }
    if result["missing_n"] or not all(counts):
        return {**result, "reason": "missing_outcomes_or_exposure_arm"}
    means = [mean(r["outcomes"][metric] for r in g) for g in groups]
    difference = means[1] - means[0]
    all_episodes = set().union(*episodes)

    def contrast(sample):
        arms = [
            [r["outcomes"][metric] for r in sample if r["exposure"] == e]
            for e in ("participated", "reported_out_no_appearance")
        ]
        return mean(arms[1]) - mean(arms[0]) if all(arms) else None

    omissions = [
        contrast([r for r in rows if r["episode_id"] != e]) for e in all_episodes
    ]
    valid = [v for v in omissions if v is not None]
    result["leave_episode_out_range"] = [min(valid), max(valid)] if valid else None
    result["leave_episode_out_complete"] = len(valid) == len(omissions)
    ordered = sorted(rows, key=lambda r: (r["game_date"], r["game_id"]))
    midpoint = len(ordered) // 2
    result["chronological_half_differences"] = [
        contrast(ordered[:midpoint]),
        contrast(ordered[midpoint:]),
    ]
    result["direction_stable"] = (
        bool(valid)
        and len(valid) == len(omissions)
        and all(v * difference > 0 for v in valid)
    )
    # Consecutive games from one absence are not independent replications.
    if len(all_episodes) < 8 or min(map(len, episodes)) < 3:
        return {**result, "reason": "too_few_independent_episodes"}
    sums = []
    for episode in all_episodes:
        sums.append(
            sum(
                (-1 if arm == 0 else 1)
                * (r["outcomes"][metric] - means[arm])
                / counts[arm]
                for arm, group in enumerate(groups)
                for r in group
                if r["episode_id"] == episode
            )
        )
    n = len(all_episodes)
    se = math.sqrt(n / (n - 1) * sum(v * v for v in sums))
    if not math.isfinite(se) or se <= 0:
        return {**result, "reason": "degenerate_uncertainty"}
    width = float(t.ppf(0.975, n - 1)) * se
    return {
        **result,
        "status": "estimated_exploratory",
        "reason": None,
        "interval": [difference - width, difference + width],
        "p_value": float(2 * t.sf(abs(difference / se), n - 1)),
    }


def correct_families(studies, core_metrics, pair_count):
    """Separate predeclared observed and causal families; missing slots count."""
    from statsmodels.stats.multitest import multipletests

    metrics = [m for s in studies for m in s["metrics"] if m["metric"] in core_metrics]
    size = pair_count * len(core_metrics)
    for key in ("observed_uncertainty", "causal"):
        records = [m.get(key) or {} for m in metrics]
        ps = [
            r.get("p_value") if r.get("p_value") is not None else 1.0 for r in records
        ]
        corrected = multipletests(ps + [1.0] * (size - len(ps)), method="holm")[1]
        for record, value in zip(records, corrected):
            if record.get("p_value") is not None:
                record.update(holm_p_value=float(value), family_size=size)
