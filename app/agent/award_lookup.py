"""Shared award resolution: reviewed reference identities, governed performance.

New awards and seasons are catalog records, not branches in the request handler.
There is no network retrieval or model-memory fallback on the serving path.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from app.agent.semantics import SemanticError


def load_catalog(path=Path(__file__).with_name("award_catalog.json")):
    data = json.loads(path.read_text())
    if data.get("version") != 1:
        raise ValueError("Unsupported award catalog version")
    catalog = {}
    aliases = set()
    for entry in data["awards"]:
        key = entry["key"]
        if key in catalog or not entry["aliases"] or not entry["records"]:
            raise ValueError("Duplicate or empty award definition")
        records = {}
        for record in entry["records"]:
            season = record["season"]
            if (
                season in records
                or not re.fullmatch(r"20\d{2}-\d{2}", season)
                or int(season[-2:]) != (int(season[:4]) + 1) % 100
                or not isinstance(record["player_id"], int)
                or record["player_id"] <= 0
                or not record["player_name"]
                or not record["source_url"].startswith("https://www.nba.com/news/")
                or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", record["verified_on"])
            ):
                raise ValueError("Invalid or duplicate award record")
            records[season] = record
        for alias in entry["aliases"]:
            if alias.lower() in aliases:
                raise ValueError("Duplicate award alias")
            aliases.add(alias.lower())
        catalog[key] = dict(entry, records=records)
    return catalog


CATALOG = load_catalog()
# Longest aliases first prevent Finals MVP from being resolved as regular MVP.
ALIASES = {alias: key for key, a in CATALOG.items() for alias in a["aliases"]}
AWARD = re.compile(
    r"\b(?:"
    + "|".join(re.escape(a) for a in sorted(ALIASES, key=len, reverse=True))
    + r")\b",
    re.I,
)
AWARD_LANGUAGE = re.compile(
    r"\b(?:awards?|troph(?:y|ies)|(?:coach|executive|teammate) of the year|"
    r"coty|eoty|all[ -](?:nba|rookie|defensive)|sportsmanship)\b",
    re.I,
)


def wants_award(question: str) -> bool:
    return bool(AWARD.search(question) or AWARD_LANGUAGE.search(question))


def resolve_award_question(question: str, selected_season: str):
    if not wants_award(question):
        return None
    text = question.lower()
    matches = list(AWARD.finditer(text))
    keys = {ALIASES[m.group().lower()] for m in matches}
    supported = ", ".join(a["label"].removeprefix("NBA ") for a in CATALOG.values())
    if not keys:
        raise SemanticError(
            "unsupported_coverage",
            "That award is not in the connected winner catalog. "
            f"Available awards: {supported}. Specify an award and season; a player name is not required.",
        )
    if len(keys) > 1:
        raise SemanticError(
            "clarification_required",
            "Which award should I look up first? Please choose one award.",
        )
    entry = CATALOG[next(iter(keys))]
    # Qualified MVP variants must never silently become the regular-season MVP.
    if re.search(
        r"\b(?:all[ -]star|conference|eastern|western|east|west|cup|in[ -]season tournament|wnba|g[ -]league)\b",
        text,
    ):
        raise SemanticError(
            "unsupported_coverage",
            "That competition or award variant is not in the connected NBA winner catalog. Regular-season MVP and NBA Finals MVP are separate supported awards.",
        )
    text = AWARD.sub(" ", text)
    seasons = []
    for match in re.finditer(r"\b(20\d{2})[-–/](20\d{2}|\d{2})\b", text):
        start, end = int(match[1]), int(match[2])
        if end != ((start + 1) if len(match[2]) == 4 else (start + 1) % 100):
            raise SemanticError(
                "clarification_required",
                "Use a consecutive NBA season, such as 2024-25.",
            )
        seasons.append(f"{start}-{end % 100:02}")
    text = re.sub(r"\b20\d{2}[-–/](?:20)?\d{2}\b", " ", text)
    for year in re.findall(r"\b20\d{2}\b", text):
        seasons.append(f"{int(year) - 1}-{int(year) % 100:02}")
    text = re.sub(r"\b20\d{2}\b", " ", text)
    relatives = re.findall(r"\b(?:this|current|last|previous|latest)\b", text)
    for relative in relatives:
        year = int(selected_season[:4]) - (relative in ("last", "previous"))
        seasons.append(
            max(entry["records"])
            if relative == "latest"
            else f"{year}-{(year + 1) % 100:02}"
        )
    if len(set(seasons)) > 1:
        raise SemanticError(
            "clarification_required",
            "Which award season and performance period do you mean? Specify one season for this lookup.",
        )
    season = next(iter(seasons), selected_season)
    explicit_regular = bool(re.search(r"\bregular[ -]season\b", text))
    explicit_playoffs = bool(re.search(r"\b(?:playoffs?|postseason)\b", text))
    if explicit_regular and explicit_playoffs:
        raise SemanticError(
            "clarification_required",
            "Choose regular-season or playoff performance for the award winner.",
        )
    phase = (
        "Regular Season"
        if explicit_regular
        else "Playoffs"
        if explicit_playoffs
        else entry["default_performance_phase"]
    )
    text = re.sub(r"\bregular[ -]season\b", " ", text)
    text = re.sub(
        r"\b(?:who|which|player|won|winner|win|is|was|the|a|an|nba|kia|award|in|for|of|and|how|did|does|he|his|him|they|their|them|play|played|perform|performed|performance|stats|statistics|show|me|tell|about|please|season|year|this|current|last|previous|latest|playoff|playoffs|postseason)\b",
        " ",
        text,
    )
    if re.sub(r"[\s?.!,;:'’–-]+", "", text):
        raise SemanticError(
            "unsupported_scope",
            f"I can look up the {entry['label']} winner for one season and summarize recorded performance. Predictions, voting explanations and additional filters need separate supported analysis; no condition was dropped.",
        )
    if season not in entry["records"]:
        raise SemanticError(
            "unsupported_coverage",
            f"No verified {entry['label']} record is connected for {season}. Available seasons: {', '.join(sorted(entry['records']))}.",
        )
    record = entry["records"][season]
    return dict(
        record,
        award_key=entry["key"],
        award=entry["label"],
        season_basis="latest_reviewed_record"
        if "latest" in relatives
        else "explicit"
        if seasons
        else "selected_season",
        performance_phase=phase,
        performance_requested=bool(
            re.search(
                r"\b(?:how|play|played|perform|performed|performance|stats|statistics)\b",
                question,
                re.I,
            )
        ),
        performance_question=f"{record['player_name']} performance {season} {phase}",
    )


def award_intro(award):
    return f"{award['player_name']} won the {award['season']} {award['award']} award. [NBA award record]({award['source_url']})."
