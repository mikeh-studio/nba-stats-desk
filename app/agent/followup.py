"""Bounded, source-derived conversation state; never a substitute for evidence."""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from app.seasons import SEASONS, season_bounds


def analysis_context(question: str, payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("status") != "ok":
        return {}
    evidence = payload.get("semantic_evidence") or {}
    profiles = payload.get("player_profiles") or [
        payload.get("player_profile") or payload.get("reference_player")
    ]
    players = [
        {key: profile["player"][key] for key in ("player_id", "player_name")}
        for profile in profiles
        if profile
        and all(
            key in profile.get("player", {}) for key in ("player_id", "player_name")
        )
    ]
    return {
        "question": question,
        "answer_summary": str(payload.get("answer", ""))[:1500],
        "players": players,
        "scope": evidence.get("scope") or {},
        "metrics": (
            [
                m["key"]
                for m in evidence.get("metrics", [])
                if isinstance(m, dict) and "key" in m
            ]
            or (
                [evidence["metric"]["key"]]
                if evidence.get("metric", {}).get("key")
                else []
            )
        ),
    }


def resolve_followup(
    question: str, context: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    """Inherit identity and scope only when the new question does not replace them."""
    if not context:
        return question, {}
    players = context.get("players", [])
    resolved = question
    for player in players:
        name = player["player_name"]
        surname = name.split()[-1]
        if (
            sum(
                p["player_name"].split()[-1].casefold() == surname.casefold()
                for p in players
            )
            == 1
        ):
            # Do not rewrite an already explicit full name.
            if name.casefold() not in resolved.casefold():
                resolved = re.sub(
                    r"(^|\b(?:besides?|except|excluding|other than|about|for|does|is|did|compare)\s+)"
                    + re.escape(surname)
                    + r"(?!\w)",
                    lambda match: match[1] + name,
                    resolved,
                    flags=re.I,
                )
    if len(players) == 1:
        resolved = re.sub(
            r"\b(his|him|he)\b",
            lambda m: (
                players[0]["player_name"] + ("’s" if m[0].lower() == "his" else "")
            ),
            resolved,
            flags=re.I,
        )
    scope = context.get("scope", {})
    inherited = {}
    changes_dates = re.search(
        r"\b(?:20\d{2}|last|past|previous|prior|this (?:season|month|week|year)|current season|today|yesterday|since|before|after|from|as.of|January|February|March|April|May|June|July|August|September|October|November|December)\b",
        question,
        re.I,
    )
    if not changes_dates:
        start = scope.get("start") or scope.get("start_date")
        end = scope.get("end") or scope.get("as_of")
        if start and end:
            resolved += f" from {start} through {end}"
            inherited["seasons"] = scope.get("seasons") or [scope.get("season")]
            inherited["seasons"] = [
                season
                for season in inherited["seasons"]
                if season
                and season_bounds(season)[0] <= date.fromisoformat(end)
                and season_bounds(season)[1] >= date.fromisoformat(start)
            ]
    if not re.search(r"\b(?:playoffs?|postseason|regular season)\b", question, re.I):
        phases = scope.get("phases") or (
            [scope["season_type"]] if scope.get("season_type") else []
        )
        if phases:
            resolved += (
                " ("
                + (
                    "both regular season and playoffs"
                    if len(phases) > 1 or phases == ["Both"]
                    else phases[0]
                )
                + ")"
            )
    dates = re.search(
        r"\bfrom (\d{4}-\d{2}-\d{2}) (?:through|to) (\d{4}-\d{2}-\d{2})\b",
        resolved,
        re.I,
    )
    if dates:
        try:
            start, end = (date.fromisoformat(value) for value in dates.groups())
            inherited["seasons"] = sorted(
                s
                for s in SEASONS
                if season_bounds(s)[0] <= end and season_bounds(s)[1] >= start
            )
        except ValueError:
            pass  # The normal scope validator supplies the user-facing error.
    excluded = []
    for player in players:
        if re.search(
            r"\b(?:besides?|except|excluding|other than)\s+"
            + re.escape(player["player_name"])
            + r"\b",
            resolved,
            re.I,
        ):
            excluded.append(player["player_id"])
    if excluded:
        inherited["excluded_player_ids"] = excluded
    if (
        re.search(r"\bplaymaking\b", question, re.I)
        and "ast" in context.get("metrics", [])
        and not re.search(r"\b(?:turnovers?|ratio|total|potential)\b", question, re.I)
    ):
        resolved += ". Use assists per game as the playmaking measure."
        inherited["assumption"] = (
            "Playmaking is measured by assists per game, following the prior overview."
        )
    return resolved, inherited
