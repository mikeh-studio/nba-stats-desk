"""Short, server-authored takeaways from governed samples, never an impact score."""

from app.agent.overview_observations import changed as metric_changed

GROUPS = {
    "Scoring": ("pts", "fg3m", "ts_pct", "fg3_pct"),
    "Creation": ("ast", "tov"),
    "Rebounding": ("reb",),
    "Defensive activity": ("stl", "blk"),
    "Exposure": ("min", "fga", "fg3a"),
}
CORES = {
    "Scoring": ("pts",),
    "Creation": ("ast", "tov"),
    "Rebounding": ("reb",),
    "Defensive activity": ("stl", "blk"),
    "Exposure": ("min",),
}


def takeaways(metrics, profile, appearances, minimum, insights=None):
    if not appearances:
        return []
    lookup = {m["key"]: m for m in metrics}

    def supported(key):
        return lookup.get(key, {}).get("sample", {}).get("status") == "descriptive"

    def changed(key):
        m = lookup.get(key, {})
        return supported(key) and metric_changed(m)

    def note(area, title, text, role="context"):
        return {
            "area": area,
            "title": title,
            "text": text,
            "role": role,
            "icon": "shield"
            if area == "Defensive activity"
            else "chart-line"
            if area == "Trajectory"
            else "chart-bar",
        }

    def snapshot(area):
        counts, ratios = [], []
        for key in GROUPS[area]:
            if not supported(key):
                continue
            m = lookup[key]
            if m["unit"] == "percent":
                ratios.append(f"{m['display_label']} {m['value']:.1f}%")
            elif key != "min":
                counts.append(f"{m['value']:.1f} {m['display_label']}")
        text = " and ".join(counts) + " per game" if counts else ""
        if ratios:
            text += ("; " if text else "") + ", ".join(ratios)
        if area in (
            "Creation",
            "Rebounding",
            "Defensive activity",
            "Exposure",
        ) and supported("min"):
            text += (
                " across " if text else ""
            ) + f"{lookup['min']['value']:.1f} MIN per game"
        text += "." if text else ""
        title = {
            "Scoring": "Scoring and efficiency",
            "Creation": "Assists with ball security",
            "Rebounding": "Recorded rebounding",
            "Defensive activity": "Recorded defensive activity",
            "Exposure": "Minutes and opportunities",
        }[area]
        return note(area, title, text.strip())

    if profile in ("general", "contribution"):
        areas = (
            ["Scoring", "Creation", "Defensive activity", "Rebounding"]
            if profile == "general"
            else ["Creation", "Rebounding", "Defensive activity"]
        )
        notes = []
        for area in areas:
            if not any(supported(k) for k in CORES[area]):
                continue
            item = snapshot(area)
            if area == "Creation":
                keys = [k for k in CORES[area] if changed(k)]
                if keys:
                    item["role"] = "change"
                    item["title"] = "Playmaking changed vs comparison"
                    if changed("ast") and changed("tov"):
                        ast, tov = (lookup[k]["display_change"] for k in ("ast", "tov"))
                        if ast > 0 and tov < 0:
                            item["title"] = "More assists, fewer turnovers"
                        elif ast < 0 and tov > 0:
                            item["title"] = "Fewer assists, more turnovers"
                    item["text"] += (
                        " "
                        + "; ".join(
                            f"{lookup[k]['display_label']} {lookup[k]['display_change']:+.1f} per game"
                            for k in keys
                        )
                        + " vs comparison."
                    )
                    if changed("min"):
                        item["text"] += (
                            f" MIN {lookup['min']['display_change']:+.1f} per game."
                        )
            notes.append(item)
            if len(notes) == 3:
                return notes
        if notes:
            return notes

    main_area = {
        "general": "Scoring",
        "scoring": "Scoring",
        "playmaking": "Creation",
        "rebounding": "Rebounding",
        "defense": "Defensive activity",
        "contribution": "Creation",
    }[profile]
    if not all(supported(k) for k in CORES[main_area]):
        detail = "; ".join(
            f"{lookup[k]['display_label']}: {lookup[k]['valid_games']}/{lookup[k]['observed_games']} valid appearances"
            for k in CORES[main_area]
        )
        return [
            note(
                main_area,
                "Limited evidence for this focus",
                detail
                + ". No standout claim; limited or incomplete comparisons are withheld.",
                "main",
            )
        ]

    notes = [{**snapshot(main_area), "role": "main"}]
    comparison_area = (
        "Creation"
        if profile == "general"
        and all(supported(k) for k in CORES["Creation"])
        and any(changed(k) for k in CORES["Creation"])
        else main_area
    )
    changed_keys = [k for k in GROUPS[comparison_area] if changed(k)][:2]
    if changed_keys:
        title = {
            "Scoring": "Scoring changed vs comparison",
            "Creation": "Playmaking changed vs comparison",
            "Rebounding": "Rebounds changed vs comparison",
            "Defensive activity": "Activity changed vs comparison",
        }[comparison_area]
        if comparison_area == "Creation" and changed("ast") and changed("tov"):
            ast, tov = (lookup[k]["display_change"] for k in ("ast", "tov"))
            if ast > 0 and tov < 0:
                title = "More assists, fewer turnovers"
            elif ast < 0 and tov > 0:
                title = "Fewer assists, more turnovers"
        elif comparison_area == "Scoring" and changed("pts"):
            title = (
                "Scoring rose vs comparison"
                if lookup["pts"]["display_change"] > 0
                else "Scoring eased vs comparison"
            )
        clauses = [
            f"{lookup[k]['display_label']} {lookup[k]['display_change']:+.1f} {'pp' if lookup[k]['unit'] == 'percent' else 'per game'}"
            for k in changed_keys
        ]
        text = "; ".join(clauses) + " vs comparison."
        if changed("min"):
            text += f" MIN {lookup['min']['display_change']:+.1f} per game; changes are descriptive."
        notes.append(note(comparison_area, title, text, "change"))
    elif profile in ("general", "scoring"):
        facts = insights or {}
        if (
            facts.get("valid_games", 0) >= minimum
            and facts.get("median_points") is not None
            and facts["max_points"] - facts["min_points"]
            >= max(5, lookup["pts"]["value"] * 0.3)
        ):
            notes.append(
                note(
                    "Trajectory",
                    "The average hides a range",
                    f"PTS ranged from {facts['min_points']:.0f} to {facts['max_points']:.0f} across {facts['observed_games']} games; median {facts['median_points']:.1f}. These are observed game scores.",
                    "variation",
                )
            )

    context_areas = (
        ["Exposure"]
        if profile == "scoring"
        else ["Rebounding", "Exposure"]
        if profile == "defense"
        else ["Defensive activity", "Rebounding", "Creation", "Exposure"]
    )
    used = {n["area"] for n in notes}
    for area in context_areas:
        if len(notes) >= 3:
            break
        if area in used or not all(supported(k) for k in CORES[area]):
            continue
        notes.append(snapshot(area))
        used.add(area)
    return notes
