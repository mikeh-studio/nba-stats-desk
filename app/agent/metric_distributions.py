"""Bounded distribution evidence from the exact percentile cohort and appearances."""

from bisect import bisect_right

from app.agent.semantics import aggregate

MAX_GAME_POINTS = 300


def describe(values):
    ordered = sorted(values)
    if not ordered:
        return {
            "n": 0,
            "min": None,
            "q1": None,
            "median": None,
            "q3": None,
            "max": None,
        }

    def quantile(p):
        index = (len(ordered) - 1) * p
        lo = int(index)
        return ordered[lo] + (ordered[min(lo + 1, len(ordered) - 1)] - ordered[lo]) * (
            index - lo
        )

    return {
        "n": len(ordered),
        "min": ordered[0],
        "q1": quantile(0.25),
        "median": quantile(0.5),
        "q3": quantile(0.75),
        "max": ordered[-1],
    }


def histogram(values, discrete=False):
    if not values:
        return []
    lo, hi = min(values), max(values)
    integer_bins = discrete and hi - lo <= 40 and all(v == int(v) for v in values)
    if integer_bins:
        start, width, count = lo - 0.5, 1, int(hi - lo) + 1
    elif hi == lo:
        start, width, count = lo - 0.5, 1, 1
    else:
        start, count = lo, 12
        width = (hi - lo) / count
    edges = [start + i * width for i in range(count + 1)]
    if not integer_bins and hi != lo:
        edges[-1] = hi
    counts = [0] * count
    for value in values:
        counts[min(count - 1, max(0, bisect_right(edges, value) - 1))] += 1
    return [
        {"start": edges[i], "end": edges[i + 1], "count": n}
        for i, n in enumerate(counts)
    ]


def distribution(metric, cohort, own_rows, player_id):
    scale = 100 if metric.unit == "ratio" else 1
    peers = [s["value"] * scale for s in cohort.values()]
    points = []
    missing, zero = 0, 0
    for row in sorted(
        own_rows, key=lambda r: (str(r.get("game_date", "")), r["game_id"])
    ):
        stats = aggregate([row], metric, "ratio" if metric.denominator else "average")
        if stats["value"] is None:
            if stats["missing_component_games"]:
                missing += 1
            else:
                zero += 1
            continue
        points.append(
            {
                "game_id": row["game_id"],
                "date": str(row.get("game_date", "")),
                "opponent": row.get("opponent_abbr"),
                "value": stats["value"] * scale,
                "attempts": stats["components"].get(metric.attempt_field)
                if metric.attempt_field
                else None,
                "attempt_field": metric.attempt_field,
                "components": stats["components"],
            }
        )
    values = [p["value"] for p in points]
    discrete = metric.unit == "count"
    # Use the full sample, before the displayed point cap. A highest-count
    # game is descriptive, not a best-overall-performance or skill claim.
    highest = None
    if discrete and points and not missing:
        peak = max(p["value"] for p in points)
        tied = [p for p in points if p["value"] == peak]
        highest = {
            **tied[0],
            "tied_games": len(tied),
            "selection": "earliest_date_then_game_id",
        }
    return {
        "version": "ask_metric_distribution/1",
        "quantile_method": "linear (n-1)",
        "among_players": {
            "summary": describe(peers),
            "bins": histogram(peers),
            "focal_in_cohort": player_id in cohort,
        },
        "across_games": {
            "summary": describe(values),
            "highest": highest,
            "bins": histogram(values, discrete),
            "observed_games": len(own_rows),
            "missing_games": missing,
            "zero_denominator_games": zero,
            "points": points[-MAX_GAME_POINTS:],
            "displayed_games": min(len(points), MAX_GAME_POINTS),
            "points_truncated": len(points) > MAX_GAME_POINTS,
        },
    }
