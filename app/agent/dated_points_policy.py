"""Bounded, deterministic interpretation of an omitted dated-points aggregation."""

from __future__ import annotations

import re
from typing import Any, Mapping

from app.agent.overview_calendar import MONTH_PATTERN
from app.agent.semantics import SemanticError, load_contract


def dated_points_default(
    question: str,
    plan: dict[str, Any],
    scope: Mapping[str, Any],
    replacements: Mapping[str, str],
    references: Mapping[str, str],
    selected_season: str,
) -> dict[str, Any] | None:
    """Consume a complete supported request before applying the approved default.

    This runs after decoding, before authoritative literal scope replacement.
    Neither prior conversational aggregation nor model preference defines policy.
    """
    if plan["status"] != "query":
        return None
    text = " ".join(question.casefold().split())
    if not re.search(r"\b(?:points|pts)\b", text):
        return None
    handles = set(replacements.values())
    if len(handles) != 1:
        return None  # No implicit identity inheritance under this policy.
    handle = next(iter(handles))
    for name in sorted(replacements, key=len, reverse=True):
        text = re.sub(
            r"(?<!\w)" + re.escape(" ".join(name.casefold().split())) + r"(?!\w)",
            " ",
            text,
        )
    date_expressions = re.findall(
        r"\bfrom\s+\d{4}-\d{2}-\d{2}\s+(?:through|to)\s+\d{4}-\d{2}-\d{2}\b"
        r"|\bweek (?:of|starting|beginning)\s+\d{4}-\d{2}-\d{2}\b"
        r"|\b(?:this|last|previous|past) (?:month|week)\b|" + MONTH_PATTERN,
        text,
    )
    if scope.get("window") != "date_range" and len(date_expressions) < 2:
        return None
    home = bool(re.search(r"\bat home\b|\bhome games\b", text))
    away = bool(re.search(r"\bon the road\b|\baway games\b", text))
    regular = bool(re.search(r"\bregular[ -]season\b", text))
    playoffs = bool(re.search(r"\b(?:playoffs?|postseason)\b", text))
    if (
        len(date_expressions) != 1
        or (home and away)
        or (regular and playoffs and scope.get("season_type") != "Both")
    ):
        raise SemanticError(
            "clarification_required",
            "Please provide one date period and a nonconflicting phase and venue for the points summary.",
        )
    if re.search(r"\b(?:predict\w*|rolling|cumulative)\b", text):
        raise SemanticError(
            "clarification_required",
            "A dated points summary cannot answer predictions, rolling or cumulative calculations.",
        )
    # Other tasks keep their established routes; never normalize them to summaries.
    if re.search(
        r"\b(?:average|averaging|mean|ppg|per[ -]game|game[ -]logs?|game[ -]by[ -]game|"
        r"rank\w*|leaders?|top|percentile|compare|versus|vs|"
        r"rebounds?|assists?|steals?|blocks?|minutes?|turnovers?|"
        r"games played|reb|ast|stl|blk|tov|ratio)\b",
        text,
    ):
        return None
    explicit_total = bool(re.search(r"\b(?:total|totals|sum)\b", text))
    # Dates must not masquerade as season declarations.
    text = re.sub(
        r"\bfrom\s+\d{4}-\d{2}-\d{2}\s+(?:through|to)\s+\d{4}-\d{2}-\d{2}\b", " ", text
    )
    text = re.sub(
        r"\b(?:week (?:of|starting|beginning)|as[ -]of)\s+\d{4}-\d{2}-\d{2}\b",
        " ",
        text,
    )
    seasons = {
        a + "-" + b[-2:] for a, b in re.findall(r"\b(20\d{2})-(\d{4}|\d{2})\b", text)
    }
    text = re.sub(r"\b20\d{2}-(?:\d{4}|\d{2})\b", " ", text)
    text = re.sub(MONTH_PATTERN + r"\s+20\d{2}\b", " ", text)
    text = re.sub(r"\bweek (?:of|starting|beginning)\s+\d{4}-\d{2}-\d{2}\b", " ", text)
    text = re.sub(r"\b(?:this|last|previous|past) (?:month|week)\b", " ", text)
    text = re.sub(r"\bas[ -]of\s+\d{4}-\d{2}-\d{2}\b", " ", text)
    phase = (
        "Both"
        if scope.get("season_type") == "Both"
        else (
            "Playoffs"
            if re.search(r"\b(?:playoffs?|postseason)\b", text)
            else load_contract().default_phase
        )
    )
    text = re.sub(
        r"\b(?:regular[ -]season|playoffs?|postseason|at home|home games|on the road|away games)\b",
        " ",
        text,
    )
    text = re.sub(r"['’]s\b", " ", text)
    text = re.sub(
        r"\b(?:what|are|is|were|was|show|me|please|give|tell|summarize|summary|of|for|the|in|during|points|pts|both|combined|combine|including|and|total|totals|sum)\b",
        " ",
        text,
    )
    if re.sub(r"[\s,?.!:;]", "", text):
        raise SemanticError(
            "clarification_required",
            "Please state the player, date period and points summary without additional unsupported conditions.",
        )
    query = plan["queries"][0]
    expected = {
        **scope,
        "season": next(iter(seasons)) if len(seasons) == 1 else selected_season,
        "season_type": phase,
        "player_name": references[handle],
        "metric": "pts",
    }
    if (
        len(seasons) > 1
        or any(
            query.get(
                k,
                load_contract().default_phase
                if k == "season_type"
                else scope.get(k)
                if k in {"window", "start_date", "as_of", "n"}
                else None,
            )
            != v
            for k, v in expected.items()
        )
        or query.get("operation", "summary") != "summary"
        or any(
            query.get(k) is not None
            for k in ("team_abbr", "opponent_abbr", "min_games", "min_attempts")
        )
        or query.get("home_away") != scope.get("home_away")
    ):
        raise SemanticError(
            "clarification_required",
            "The planned points summary does not match the requested player, dates or scope. Please restate the request.",
        )
    original = query.get("aggregation")
    if explicit_total:
        if original != "total":
            raise SemanticError(
                "clarification_required",
                "The planned aggregation does not match the requested points total.",
            )
        return None
    if original not in {"total", "average"}:
        raise SemanticError(
            "clarification_required", "The planned points aggregation is unsupported."
        )
    query["aggregation"] = "average"
    return {
        "policy": "dated_points_per_game_v1",
        "source": "deterministic_product_policy",
        "reason": "aggregation_omitted",
        "original_aggregation": original,
        "chosen_aggregation": "average",
        "unit": "points per game",
        "request_scope": dict(expected),
        "derived_scope_fields": sorted(
            k
            for k in expected
            if k not in query
            and k in {"window", "start_date", "as_of", "n", "season_type"}
        ),
    }
