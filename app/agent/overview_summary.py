"""Question-scoped, deterministic performance prose and inspectable game facts."""

from statistics import median

from app.agent.overview_insights import insight_paragraphs


def game_insights(rows):
    """Scoring distribution from this player's scoped appearances only."""
    valid = [r for r in rows if r.get("pts") is not None]
    result = {"observed_games": len(rows), "valid_games": len(valid)}
    if not valid or len(valid) != len(rows):
        return result
    peak = max(valid, key=lambda r: (r["pts"], str(r["game_date"]), str(r["game_id"])))
    result.update(
        median_points=median(r["pts"] for r in valid),
        min_points=min(r["pts"] for r in valid),
        max_points=peak["pts"],
        peak_date=str(peak["game_date"]),
        peak_game_id=str(peak["game_id"]),
        peak_ties=sum(r["pts"] == peak["pts"] for r in valid),
        threshold=20,
        threshold_games=sum(r["pts"] >= 20 for r in valid),
    )
    return result


def summary_paragraphs(
    name, metrics, scope, appearances, insights=None, profile="general"
):
    phase = (
        " + ".join(scope.get("phases", []))
        .replace("Regular Season", "regular-season")
        .replace("Playoffs", "playoff")
        .replace(" + ", " and ")
    )
    if not appearances:
        return [
            "No recorded appearances are available in the requested dates and phases."
        ]
    return insight_paragraphs(name, metrics, scope, appearances, phase, profile)
