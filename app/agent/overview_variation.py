"""Question-specific variation and peak facts, separate from peer standing."""

from datetime import date

from app.agent.overview_observations import NAMES, complete

FOCUS_METRICS = {
    "scoring": "pts",
    "playmaking": "ast",
    "rebounding": "reb",
    "defense": "stl",
    "contribution": "ast",
    "general": "pts",
}


def variation(metric, minimum=5):
    games = metric.get("distribution", {}).get("across_games", {})
    stats = games.get("summary", {})
    if (
        metric.get("key") not in {"pts", "ast", "reb", "stl", "blk", "fg3m"}
        or not complete(metric)
        or stats.get("n", 0) < minimum
        or games.get("missing_games")
        or games.get("zero_denominator_games")
        or stats.get("n") != games.get("observed_games")
    ):
        return None
    key = metric["key"]
    label = NAMES.get(key, metric.get("label", key).lower())
    median, q1, q3 = (stats[k] for k in ("median", "q1", "q3"))
    spread = (
        f"Across his games, the median was {median:g} {label}; "
        f"the middle 50% of game values ran from {q1:g} to {q3:g}."
    )
    peak = games.get("highest")
    peak_text = ""
    if peak and stats["min"] != stats["max"]:
        try:
            date.fromisoformat(peak["date"])
        except (ValueError, TypeError):
            peak = None
        if peak:
            peak_text = f"His high was {peak['value']:g} {label} on {peak['date']}"
            if peak.get("opponent"):
                peak_text += f" against {peak['opponent']}"
            if peak["tied_games"] > 1:
                peak_text += f", the first of {peak['tied_games']} games at that mark"
            peak_text += "."
    return {
        "metric_key": key,
        "summary": stats,
        "highest": peak,
        "spread_text": spread,
        "peak_text": peak_text,
        "text": spread + (" " + peak_text if peak_text else ""),
    }
