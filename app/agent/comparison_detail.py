"""Public, evidence-derived comparison context without private availability records."""

from math import fsum, isfinite

RATE_METRICS = {
    "pts",
    "reb",
    "ast",
    "stl",
    "blk",
    "tov",
    "fg3m",
    "fgm",
    "fga",
    "fg3a",
    "ftm",
    "fta",
}


def number(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and isfinite(value)
    )


def relative_change(metric):
    """Out versus both played, using unrounded averages; signed/ratio scales excluded."""
    d = metric.get("descriptive") or {}
    groups = d.get("groups") or []
    if metric["metric"] == "plus_minus" or metric.get("unit") == "percentage points":
        return None
    if len(groups) != 2 or any(
        not number(g.get("value")) or g.get("missing_component_games", 0)
        for g in groups
    ):
        return None
    a, b = (g["value"] for g in groups)
    if a <= 0 or not number(d.get("difference")):
        return None
    return (b - a) / a * 100


def comparison_detail(study):
    from app.agent.semantics import aggregate, load_contract

    panel = study.get("panel", [])
    if not panel:
        return None
    contract = load_contract()
    included = [r for r in panel if r.get("included") and r.get("outcomes")]
    if len(included) > 110:
        raise ValueError("Comparison exceeds bounded season game count")
    games = []
    for row in sorted(included, key=lambda r: (r["game_date"], r["game_id"])):
        outcome = row["outcomes"]
        values = {}
        for m in study["metrics"]:
            metric = contract.metric(m["metric"])
            values[m["metric"]] = aggregate(
                [outcome], metric, "ratio" if metric.denominator else "average"
            )["display_value"]
        games.append(
            dict(
                game_id=row["game_id"],
                date=row["game_date"],
                phase=row.get("season_type", outcome.get("season_type")),
                opponent=row.get("opponent_abbr"),
                group="both" if row["exposure"] == "participated" else "out",
                values=values,
            )
        )
    rates = {}
    for key in RATE_METRICS:
        arms = []
        for group in ("participated", "reported_out_no_appearance"):
            rows = [r["outcomes"] for r in included if r["exposure"] == group]
            valid = [
                r
                for r in rows
                if number(r.get(key)) and number(r.get("min")) and r["min"] > 0
            ]
            value = (
                36 * fsum(r[key] for r in valid) / fsum(r["min"] for r in valid)
                if valid and len(valid) == len(rows)
                else None
            )
            arms.append(dict(value=value, games=len(rows), valid_games=len(valid)))
        rates[key] = arms
    minutes = (
        next(
            (m.get("descriptive") for m in study["metrics"] if m["metric"] == "min"),
            None,
        )
        or {}
    )
    return dict(
        games=games,
        outside_teammate_window=sum(
            bool(r.get("focal_participated"))
            and r.get("eligibility") == "not_teammates"
            for r in panel
        ),
        rates=rates,
        minutes={k: minutes.get(k) for k in ("participated", "reported_out")},
    )
