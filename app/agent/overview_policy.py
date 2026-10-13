"""Explicit display choices and sample limits; never an individual impact score."""

import re

from app.agent.overview_takeaways import takeaways

ABBREVIATIONS = {
    "pts": "PTS",
    "reb": "REB",
    "ast": "AST",
    "stl": "STL",
    "blk": "BLK",
    "fg3m": "3PM",
    "fg3a": "3PA",
    "fg3_pct": "3P%",
    "min": "MIN",
    "tov": "TOV",
    "fga": "FGA",
    "fta": "FTA",
    "fg_pct": "FG%",
    "ft_pct": "FT%",
    "efg_pct": "eFG%",
    "ts_pct": "TS%",
    "pts_per36": "PTS/36",
    "ast_per36": "AST/36",
}

GENERAL = ("pts", "ts_pct", "fg3m", "fg3_pct", "ast", "tov", "reb", "stl", "blk", "min")
PROFILES = {
    "general": GENERAL,
    "scoring": ("pts", "ts_pct", "fg3m", "fg3_pct", "fga", "fg3a", "min"),
    "playmaking": ("ast", "tov", "pts", "ts_pct", "reb", "min"),
    "rebounding": ("reb", "min", "ast", "tov", "stl", "blk"),
    "defense": ("stl", "blk", "min", "reb", "ast", "tov"),
    "contribution": ("ast", "tov", "reb", "stl", "blk", "min"),
}
FOCUS_WORDS = {
    "contribution": r"contribution beyond scoring|non[ -]scoring contribution",
    "scoring": r"scoring|shooting",
    "playmaking": r"playmaking",
    "rebounding": r"rebounding",
    "defense": r"defen[cs]e|defensive activity",
}
LIMITS = {
    "general": "Box scores describe recorded production. Screening, defensive positioning and individual defensive impact are not measured here.",
    "scoring": "Shooting accuracy is descriptive; attempts and valid games accompany percentages. No shooting percentile is inferred.",
    "playmaking": "Assists and turnovers describe recorded creation and ball security; they do not measure passing quality or creation load completely.",
    "rebounding": "Only total rebounds are connected. Offensive/defensive splits, rebound opportunities and box-outs are unavailable; supporting stats do not measure rebounding skill.",
    "defense": "Steals and blocks describe recorded defensive activity. Contests, assignments, fouls and rim protection are unavailable; this is not an overall defensive grade.",
    "contribution": "These are recorded contributions beyond scoring. Screens, potential assists, secondary assists and loose-ball recoveries are unavailable; no combined impact score is calculated.",
}


def focus(question):
    text = question.split("\nClarification:", 1)[0]
    for name, pattern in FOCUS_WORDS.items():
        if re.search(r"\b(?:" + pattern + r")\b", text, re.I):
            return name
    return "general"


def strip_focus(question):
    for pattern in FOCUS_WORDS.values():
        question = re.sub(r"\b(?:" + pattern + r")\b", "", question, flags=re.I)
    return question


def sample(metric, minimum):
    valid, observed = metric["valid_games"], metric["observed_games"]
    missing = metric["missing_component_games"]
    status = (
        "unavailable"
        if metric["value"] is None
        else "limited"
        if missing or valid < minimum
        else "descriptive"
    )
    text = f"{valid}/{observed} valid appearances"
    if missing:
        text += f"; {missing} missing components"
    if metric["key"] in ("fg_pct", "fg3_pct", "ft_pct"):
        text += (
            f"; {metric['numerator']:g} makes / {metric['denominator']:g} attempts"
            if valid
            else "; attempts unavailable"
        )
    elif metric["key"] == "ts_pct":
        c = metric["components"]
        text += (
            f"; {c.get('fga', 0):g} FGA, {c.get('fta', 0):g} FTA"
            if valid
            else "; attempts unavailable"
        )
    if status == "unavailable":
        text += "; value unavailable (missing data or zero denominator)"
    elif status == "limited":
        text += "; limited sample — comparison withheld"
    elif metric["unit"] == "percent":
        text += "; descriptive ratio — no ranking threshold established"
    return {
        "status": status,
        "text": text,
        "comparison_eligible": bool(
            status == "descriptive"
            and metric.get("previous", {}).get("valid_games", 0) >= minimum
            and not metric.get("previous", {}).get("missing_component_games", 0)
        ),
    }


def presentation(metrics, question, appearances, minimum, *, insights=None):
    profile = focus(question)
    for metric in metrics:
        metric["display_label"] = ABBREVIATIONS.get(metric["key"], metric["label"])
        metric.setdefault("unit", "per game")
        metric["sample"] = sample(metric, minimum)
        metric["display_change"] = (
            metric.get("change") if metric["sample"]["comparison_eligible"] else None
        )
    lookup = {m["key"]: m for m in metrics}
    selected = [lookup[key] for key in PROFILES[profile]]
    notes = takeaways(metrics, profile, appearances, minimum, insights)
    return {
        "version": "ask_metric_presentation/2",
        "profile": profile,
        "primary_keys": [m["key"] for m in selected],
        "scope_note": f"{appearances} recorded appearances. Samples vary by metric; missing values are not zero. The {minimum}-game comparison floor is a project policy, not a guarantee of statistical precision.",
        "limitation": LIMITS[profile],
        "takeaways": notes,
        "preferred_metric": {
            "scoring": "pts",
            "playmaking": "ast",
            "rebounding": "reb",
            "defense": "stl",
            "contribution": "ast",
        }.get(profile, "pts"),
    }


def metric_table(metrics, keys, title):
    by_key = {m["key"]: m for m in metrics}
    chosen = [by_key[k] for k in keys]

    def display(value):
        return "Unavailable" if value is None else f"{value:.1f}"

    return {
        "title": title,
        "columns": [
            {"key": k, "label": v}
            for k, v in [
                ("metric", "Stat"),
                ("value", "Value"),
                ("unit", "Unit"),
                ("percentile", "League percentile"),
                ("change", "Change vs comparison"),
                ("sample", "Sample / opportunity"),
            ]
        ],
        "rows": [
            [
                m.get("display_label", m["label"]),
                display(m["value"]),
                m["unit"],
                display(m.get("percentile")),
                display(m.get("display_change")),
                m["sample"]["text"],
            ]
            for m in chosen
        ],
    }
