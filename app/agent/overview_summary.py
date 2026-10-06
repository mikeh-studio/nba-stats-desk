"""Question-scoped, deterministic performance prose and inspectable game facts."""

from statistics import median


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


def summary_paragraphs(name, metrics, scope, appearances, insights=None):
    def complete(m):
        return m.get("value") is not None and not m.get("missing_component_games")

    def changed(m):
        return (
            complete(m)
            and m.get("change") is not None
            and abs(m["change"]) >= max(0.1, abs(m["value"]) * 0.02)
        )

    by_key = {m["key"]: m for m in metrics}
    phases = scope.get("phases", [])
    phase = (
        "regular-season"
        if phases == ["Regular Season"]
        else "playoff"
        if phases == ["Playoffs"]
        else "regular-season and playoff"
    )
    if not appearances:
        return [
            "No recorded appearances are available in the requested dates and phases."
        ]
    pts, ast, ts = (by_key.get(k, {}) for k in ("pts", "ast", "ts_pct"))
    first = f"Across {appearances} {phase} appearances, {name}"
    production = [
        f"{m['value']:.1f} {m['label'].lower()}" for m in (pts, ast) if complete(m)
    ]
    if production:
        first += " averaged " + " and ".join(production) + " per game."
    else:
        first += (
            " has incomplete scoring and assist data, limiting the overall assessment."
        )
    if complete(ts):
        first += f" True shooting was {ts['value']:.1f}%, combining field-goal and free-throw scoring efficiency."
    ranked = [m for m in (pts, ast) if complete(m) and m.get("percentile") is not None]
    standout = max(ranked, key=lambda m: m["percentile"], default=None)
    if standout and standout["percentile"] >= 90:
        first += f" {standout['label']} stood out at the {standout['percentile']:.0f}th percentile among qualified players in the same scope."

    baseline = scope.get("comparison_label") or (
        f"{' + '.join(scope.get('previous_phases', phases))} from {scope['previous_start']} through {scope['previous_end']}"
        if scope.get("previous_start")
        else "the previous period"
    )
    clauses = []
    for key, label in (
        ("pts", "scoring"),
        ("ast", "assists"),
        ("ts_pct", "true shooting"),
    ):
        m = by_key.get(key, {})
        if not complete(m) or m.get("change") is None:
            continue
        if changed(m):
            unit = "percentage points" if key == "ts_pct" else "per game"
            clauses.append(
                f"{label} {'rose' if m['change'] > 0 else 'fell'} {abs(m['change']):.1f} {unit}"
            )
        else:
            clauses.append(f"{label} stayed broadly similar")
    second = (
        f"Compared with {baseline}, " + "; ".join(clauses) + "."
        if clauses
        else f"A reliable scoring, playmaking or efficiency comparison with {baseline} is unavailable."
    )
    minutes = by_key.get("min", {})
    if changed(minutes):
        second += f" Minutes per game {'rose' if minutes['change'] > 0 else 'fell'} {abs(minutes['change']):.1f}, providing context for the production changes."
    if clauses:
        second += " These differences describe performance, not what caused it."

    facts = insights or {}
    if facts.get("median_points") is not None:
        third = (
            f"Game-level scoring ranged from {facts['min_points']:.0f} to {facts['max_points']:.0f} points, "
            f"with a median of {facts['median_points']:.1f}. "
            f"{name} reached {facts['threshold']} points in {facts['threshold_games']} of {facts['observed_games']} appearances. "
            f"{'One of the highest-scoring games came' if facts['peak_ties'] > 1 else 'The highest-scoring game came'} on {facts['peak_date']}. "
            "The median and game counts add context that the average alone does not show."
        )
    else:
        third = "Complete game-level scoring is unavailable, so scoring consistency and standout-game claims are withheld."
    missing = [
        m["label"]
        for m in metrics
        if m.get("missing_component_games")
        and m["key"] in ("pts", "ast", "reb", "stl", "blk", "ts_pct")
    ]
    if missing:
        third += f" Partial data for {', '.join(missing)}: missing values are not zero."
    if appearances < 5:
        third += " This small sample should be interpreted cautiously."
    return [first, second, third]
