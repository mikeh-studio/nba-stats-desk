"""Compact descriptive context; unknown availability is never an absence."""

from statistics import mean


def summarize_context(context_rows, case):
    own = [
        r
        for r in context_rows
        if r["player_id"] == case["player_id"]
        and r["season"] in case["seasons"]
        and r["season_type"] == case["phase"]
    ]
    periods = {}
    for name, start, end in (
        ("previous", case["previous_start"], case["previous_end"]),
        ("current", case["start"], case["end"]),
    ):
        sample = [r for r in own if start <= r["game_date"] <= end]
        valid = [r for r in sample if r["opponent_prior_win_pct"] is not None]
        shooting = [
            r for r in sample if r["opponent_prior_efg_allowed_pct"] is not None
        ]
        periods[name] = {
            "games": len(sample),
            "valid_opponent_games": len(valid),
            "valid_opponent_shooting_games": len(shooting),
            "mean_opponent_prior_win_pct": mean(
                r["opponent_prior_win_pct"] for r in valid
            )
            if valid
            else None,
            "mean_opponent_prior_efg_allowed_pct": mean(
                r["opponent_prior_efg_allowed_pct"] for r in shooting
            )
            if shooting
            else None,
        }
    return {
        "id": "S_opponent",
        "periods": periods,
        "claim_level": "descriptive",
        "definition": "Equal-game mean of opponents prior same-season/phase win% and eFG% allowed. At least 5 complete prior games per opponent; missing games excluded.",
        "limitations": "Not possession-based defensive rating; not causal adjustment. Historical statistics may be corrected later.",
    }


def teammate_comparison(rows, reports, case, teammate_id):
    """Only game-specific co-team appearances or dated team reports support a split.

    No inferred roster intervals. An unobserved player can be traded, rested,
    unreported, or injured. Those games are retained as unknown.
    """
    lookup = {(r["season"], r["game_id"], r["player_id"]): r for r in rows}
    status = {
        (r["season"], r["game_id"], r["player_id"], r["team_abbr"]): r for r in reports
    }
    results = {}
    for name, start, end in (
        ("previous", case["previous_start"], case["previous_end"]),
        ("current", case["start"], case["end"]),
    ):
        groups = {
            "participated": [],
            "reported_out_no_appearance": [],
            "unknown": [],
            "conflicting": [],
        }
        details = []
        for row in rows:
            if (
                row["player_id"] != case["player_id"]
                or row["season"] not in case["seasons"]
                or row["season_type"] != case["phase"]
                or not start <= row["game_date"] <= end
            ):
                continue
            other = lookup.get((row["season"], row["game_id"], teammate_id))
            report = status.get(
                (row["season"], row["game_id"], teammate_id, row["team_abbr"])
            )
            played = (
                other is not None
                and other["team_abbr"] == row["team_abbr"]
                and (other.get("min") or 0) > 0
            )
            out = report is not None and report["injury_status"] == "Out"
            conflict = report is not None and report["conflicting_reports"]
            group = (
                "conflicting"
                if conflict or (played and out)
                else "participated"
                if played
                else "reported_out_no_appearance"
                if out and other is None
                else "unknown"
            )
            groups[group].append(row)
            details.append(
                {
                    "game_id": row["game_id"],
                    "group": group,
                    "source_url": report["source_url"] if report else None,
                }
            )
        summary = {}
        for group, sample in groups.items():
            summary[group] = {"games": len(sample)}
            if group in ("participated", "reported_out_no_appearance"):
                for field in ("ast", "pts", "min"):
                    values = [r[field] for r in sample if r.get(field) is not None]
                    summary[group][field + "_per_game"] = (
                        mean(values) if values and len(values) == len(sample) else None
                    )
        results[name] = {"groups": summary, "game_evidence": details}
    return {
        "id": "S_teammate",
        "teammate_id": teammate_id,
        "teammate_name": next(
            (r.get("player_name") for r in rows if r["player_id"] == teammate_id),
            None,
        ),
        "periods": results,
        "claim_level": "descriptive",
        "limitations": "Selected observed games only; historical roster coverage incomplete. Out uses a conservative before-game-date UTC report, not final tipoff availability. Co-participation is not shared court time. No causal effect or adjusted estimate.",
    }
