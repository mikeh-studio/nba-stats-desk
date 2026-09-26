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


def game_counts(metric):
    """Counts describe appearances, never shared court minutes or inferred injuries."""
    groups = (metric.get("descriptive") or {}).get("groups") or []
    counts = (
        [g.get("observed_games") for g in groups]
        if len(groups) == 2
        else (metric.get("observed_uncertainty") or {}).get("group_n")
    )
    if (
        isinstance(counts, (list, tuple))
        and len(counts) == 2
        and all(type(n) is int and n >= 0 for n in counts)
    ):
        return tuple(counts)
    return None


STAT_WORDS = {
    "pts": "points",
    "ast": "assists",
    "reb": "rebounds",
    "stl": "steals",
    "blk": "blocks",
    "tov": "turnovers",
    "fg3m": "made threes",
    "min": "minutes",
    "fga": "field-goal attempts",
    "fg3a": "three-point attempts",
    "fta": "free-throw attempts",
}


def join_phrases(parts):
    if len(parts) < 2:
        return "".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def descriptive_highlight(metric):
    return {
        "metric": metric["metric"],
        "label": metric.get("label", metric["metric"].upper()),
        "estimate": (metric.get("descriptive") or {}).get("difference"),
        "unit": metric.get("unit", "per game"),
    }


def presented_insights(study, highlights, selected):
    """Present observed results; assessment diagnostics never become user prose."""
    available = [h for h in highlights if h["estimate"] is not None]
    missing = [
        m.get("label", m["metric"].upper())
        for m in study["metrics"]
        if m["metric"] in selected
        and (m.get("descriptive") or {}).get("difference") is None
    ]
    if not available:
        return f"{join_phrases(missing) or 'That comparison'} is unavailable for this comparison."
    player, teammate = study["player_name"], study["teammate_name"]
    lead = []
    details = []
    all_per_game = all(h["metric"] in STAT_WORDS for h in available)
    for h in available:
        key, value = h["metric"], h["estimate"]
        word = STAT_WORDS.get(key, h["label"])
        positive = value > 0
        near_zero = round(abs(value), 1) == 0
        if key == "pts":
            phrase = (
                "scored about the same"
                if near_zero
                else "scored more"
                if positive
                else "scored less"
            )
        elif key in STAT_WORDS:
            phrase = f"recorded {'about the same' if near_zero else 'more' if positive else 'fewer'} {word}"
        else:
            phrase = f"had {'about the same' if near_zero else 'a higher' if positive else 'a lower'} {h['label']}"
        lead.append(phrase)
        if near_zero:
            details.append(f"roughly the same {word}")
        elif key in STAT_WORDS:
            details.append(
                f"{abs(value):.1f} {'more' if positive else 'fewer'} {word}"
                + ("" if all_per_game else " per game")
            )
        else:
            unit = h["unit"]
            details.append(
                f"{h['label']} {'higher' if positive else 'lower'} by {abs(value):.1f} {unit}"
            )
    from app.agent.comparison_detail import relative_change

    for i, h in enumerate(available):
        m = next(m for m in study["metrics"] if m["metric"] == h["metric"])
        relative = relative_change(m)
        if relative is not None:
            a, b = (g["value"] for g in m["descriptive"]["groups"])
            details[i] += f" ({b:.1f} vs {a:.1f}; {relative:+.1f}%)"
    lines = [f"{player} {join_phrases(lead[:2])} when {teammate} was out."]
    # Only state a shared count when every available requested metric agrees.
    counts = [
        game_counts(m)
        for m in study["metrics"]
        if m["metric"] in selected
        and (m.get("descriptive") or {}).get("difference") is not None
    ]
    shared = (
        counts[0]
        if counts and counts[0] is not None and all(c == counts[0] for c in counts)
        else None
    )
    context = (
        f"Across {shared[0]} games when both played and {shared[1]} with {teammate} out, "
        if shared
        else "Compared with games when both played, "
    )
    lines.append(
        context
        + f"{player} averaged {join_phrases(details)}"
        + (" per game." if all_per_game else ".")
    )
    if shared and min(shared) < 5:
        lines.append("This is a small snapshot, so treat it as an early observation.")
    else:
        lines.append(
            f"These results describe those games; they do not establish that {teammate}'s absence caused the differences."
        )
    if counts and not shared:
        lines.append("Game coverage differs or is unavailable for some statistics.")
    if missing:
        lines.append(f"{join_phrases(missing)} is unavailable for this comparison.")
    return "\n\n".join(lines)


def insights(study, selected):
    assessments = [assess(m) for m in study["metrics"] if m["metric"] in selected]
    by_key = {m["metric"]: m for m in study["metrics"]}
    # Keep internal assessment/ranking; present descriptive differences only.
    ranked = sorted(
        assessments,
        key=lambda a: (
            (by_key[a["metric"]].get("descriptive") or {}).get("difference")
            is not None,
            a["rank"],
        ),
        reverse=True,
    )
    highlights = [descriptive_highlight(by_key[a["metric"]]) for a in ranked[:3]]
    return assessments, highlights, presented_insights(study, highlights, selected)
