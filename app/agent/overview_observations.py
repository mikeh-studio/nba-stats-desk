"""Shared display decisions over existing governed metrics, not new statistics."""

NAMES = {
    "pts": "points",
    "ast": "assists",
    "tov": "turnovers",
    "reb": "rebounds",
    "stl": "steals",
    "blk": "blocks",
    "min": "minutes",
    "ts_pct": "true shooting",
}


def complete(metric):
    return metric.get("value") is not None and not metric.get("missing_component_games")


def comparison_change(metric):
    """Honor display eligibility, including explicit suppression of a raw delta."""
    if not complete(metric):
        return None
    if "sample" in metric and not metric["sample"].get("comparison_eligible"):
        return None
    return metric.get("display_change", metric.get("change"))


def changed(metric):
    delta = comparison_change(metric)
    return delta is not None and abs(delta) >= max(0.1, abs(metric["value"]) * 0.02)


def limitations(lookup, keys):
    """Missing and partial observations must not sound like recorded zeroes."""
    notes = []
    for key in keys:
        metric = lookup.get(key, {})
        label = NAMES[key].capitalize()
        verb = "is" if key == "ts_pct" else "are"
        if metric.get("missing_component_games"):
            sample = (
                f" ({metric['valid_games']} of {metric['observed_games']} appearances with data)"
                if "valid_games" in metric and "observed_games" in metric
                else ""
            )
            notes.append(
                f"{label} {verb} incomplete{sample}; missing values are not zero."
            )
        elif metric.get("value") is None:
            reason = (
                " (zero shooting denominator)"
                if key == "ts_pct" and metric.get("denominator") == 0
                else ""
            )
            notes.append(f"{label} {verb} unavailable{reason}.")
    return " ".join(notes)
