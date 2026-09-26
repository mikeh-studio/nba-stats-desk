"""Deterministic, evidence-bound selection and prose for Ask teammate studies."""

# Product heuristics in native per-game units, not scientific or fantasy scoring cutoffs.
MEANINGFUL_CHANGE = dict(
    pts=2.0,
    reb=1.0,
    ast=1.0,
    stl=0.3,
    blk=0.3,
    tov=0.5,
    fg3m=0.5,
    min=3.0,
    plus_minus=3.0,
)


def number(value):
    return "Unavailable" if value is None else f"{value:+.2f}"


def assess(metric):
    d = metric.get("descriptive") or {}
    u = metric.get("observed_uncertainty") or {}
    c = metric.get("causal") or {}
    causal = (
        c.get("estimate") is not None
        and c.get("claim_level") == "causal_estimate_under_assumptions"
    )
    estimate = c.get("estimate") if causal else d.get("difference")
    inference = c if causal else u
    p = inference.get("holm_p_value")
    support = (
        "statistically supported (exploratory)"
        if p is not None and p < 0.05
        else "inconclusive"
        if p is not None
        else "insufficient evidence for significance"
    )
    interval = inference.get("interval")
    threshold = MEANINGFUL_CHANGE.get(metric["metric"])
    practical = "not assessed"
    if estimate is not None and threshold is not None:
        practical = (
            "point estimate exceeds product threshold"
            if abs(estimate) >= threshold
            else "point estimate below product threshold"
        )
        if interval and interval[0] >= -threshold and interval[1] <= threshold:
            practical = "interval lies within product small-change range"
    diagnostics = metric.get("diagnostics") or {}
    refits = diagnostics.get("leave_episode_out") or []
    a = metric.get("association") or {}
    fitted = [
        r.get("adjusted_difference")
        for r in refits
        if r.get("adjusted_difference") is not None
    ]
    adjusted_stable = (
        all(v * a["adjusted_difference"] > 0 for v in fitted)
        if fitted
        and len(fitted) == len(refits)
        and a.get("adjusted_difference") is not None
        else None
    )
    sample_raw = diagnostics.get("same_sample_raw_difference")
    adjusted = a.get("adjusted_difference")
    reversal = (
        sample_raw is not None and adjusted is not None and sample_raw * adjusted < 0
    )
    unstable = (
        u.get("direction_stable") is False or adjusted_stable is False or reversal
    )
    stability = (
        "sensitive to episode or adjustment"
        if unstable
        else "observed direction stable across episode omissions"
        if u.get("direction_stable")
        else "stability not established"
    )
    if causal:
        causal_refits = c.get("leave_episode_out_refits") or []
        complete = bool(causal_refits) and all(
            r.get("estimate") is not None for r in causal_refits
        )
        stable = complete and all(r["estimate"] * estimate > 0 for r in causal_refits)
        unstable = complete and not stable
        stability = (
            "causal direction stable across full episode refits"
            if stable
            else "causal direction changes across episode refits"
            if unstable
            else "causal refit stability not established"
        )
    magnitude = abs(estimate) / threshold if estimate is not None and threshold else 0
    return {
        "metric": metric["metric"],
        "unit": metric.get("unit", "per game"),
        "label": metric.get("label", metric["metric"].upper()),
        "claim": "causal estimate under stated assumptions"
        if causal
        else "observed association",
        "estimate": estimate,
        "interval": interval,
        "interval_label": inference.get("interval_label"),
        "significance": support,
        "holm_p_value": p,
        "practical": practical,
        "meaningful_change_threshold": threshold,
        "stability": stability,
        "adjusted_refit_direction_stable": adjusted_stable,
        "adjustment_reverses_same_sample_direction": reversal,
        "sample": u,
        "causal_n": c.get("n") if causal else None,
        "causal_episodes": c.get("episodes") if causal else None,
        "rank": (
            estimate is not None,
            p is not None,
            not unstable if p is not None else True,
            min(magnitude, 5)
            if p is not None
            else {
                "pts": 9,
                "ast": 8,
                "reb": 7,
                "plus_minus": 6,
                "min": 5,
                "fg3m": 4,
                "tov": 3,
                "stl": 2,
                "blk": 1,
            }.get(metric["metric"], 0),
        ),
    }


def insights(study, selected):
    assessments = [assess(m) for m in study["metrics"] if m["metric"] in selected]
    highlights = sorted(assessments, key=lambda a: a["rank"], reverse=True)[:3]
    lines = []
    for a in highlights:
        if a["estimate"] is None:
            lines.append(f"{a['label']}: evidence is unavailable for this comparison.")
            continue
        uncertainty = (
            f"; pointwise 95% interval {number(a['interval'][0])} to {number(a['interval'][1])}"
            if a["interval"]
            else "; uncertainty cannot be reliably assessed"
        )
        counts = a["sample"].get("group_n")
        episodes = a["sample"].get("arm_episodes")
        sample = (
            (
                f" Observed sample: {counts[0]} with / {counts[1]} Out games"
                + (
                    f", across {episodes[0]} / {episodes[1]} exposure episodes."
                    if episodes
                    else "."
                )
            )
            if counts
            else ""
        )
        if a["causal_n"] is not None:
            sample += f" Causal sample: {a['causal_n']} eligible games, {a['causal_episodes']} episodes."
        lines.append(
            f"{a['label']}: {number(a['estimate'])} {a['unit']} ({a['claim']}{uncertainty}). "
            f"{a['significance'].capitalize()}; {a['stability']}. "
            f"Practical size: {a['practical']}"
            + (
                f" (threshold {a['meaningful_change_threshold']:g} {a['unit']})."
                if a["meaningful_change_threshold"] is not None
                else "."
            )
            + sample
        )
    if not any(
        a["claim"] == "causal estimate under stated assumptions" for a in assessments
    ):
        lines.insert(
            0,
            "These results describe observed changes; the evidence does not currently support attributing them to the teammate's absence.",
        )
    return assessments, highlights, "\n\n".join(lines)
