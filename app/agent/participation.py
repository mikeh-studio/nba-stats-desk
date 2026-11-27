"""Sourced playoff participation facts. Absent records never prove absence."""

import json
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import date
from pathlib import Path
from typing import Any

from app.agent.alias_builder import normalize_player_text
from app.agent.payload import answer_payload
from app.agent.player_suggestions import resolve_player_name
from app.agent.scope_policy import (
    normalize_playoff_round_year,
    playoff_round_planning_question,
    requested_playoff_round,
)
from app.agent.semantic_serving import requested_seasons, source_players
from app.agent.semantics import (
    PLAYOFF_ROUND_LABELS,
    PLAYOFF_ROUNDS,
    Evidence,
    SemanticError,
    _date,
    row_playoff_round,
)

_REQUEST_WORDS = (
    r"nba|finals?|in|the|this|season|did|does|play|participate|reach|make|it|to|"
    r"how|many|what|were|was|his|points?|pts|rebounds?|assists?|stats|statistics|"
    r"per|game|score|scored|average"
)
_PROGRAMMATIC_WORDS = (
    _REQUEST_WORDS + r"|playoffs|of|during|total|totals|games|log|perform|performance|"
    r"appear|appeared|played|record|put up|any|minutes|steals|blocks|turnovers|"
    r"(?:last|previous|prior|current) season"
)


def load_participation_catalog() -> list[dict[str, Any]]:
    data = json.loads(
        Path(__file__).with_name("participation_catalog.json").read_text()
    )
    if data.get("version") != 1:
        raise ValueError("Unsupported participation catalog")
    seen = set()
    for r in data["records"]:
        key = (r["season"], r["event"], r["player_id"])
        if (
            key in seen
            or r["event"] != "NBA Finals"
            or r["participated"] is not False
            or type(r["player_id"]) is not int
            or not r["aliases"]
            or not r["source_url"].startswith("https://www.nba.com/")
        ):
            raise ValueError("Invalid participation record")
        seen.add(key)
    return data["records"]


def _completed_round_teams(
    games: Mapping[str, list[Mapping[str, Any]]], expected_series: int
) -> set[str]:
    series: dict[tuple[str, str], list[tuple[date, str, str]]] = defaultdict(list)
    for game_id, rows in games.items():
        teams = set()
        observed_teams = set()
        dates = set()
        winners = set()
        for row in rows:
            team, opponent = row.get("team_abbr"), row.get("opponent_abbr")
            if not team or not opponent or team == opponent:
                return set()
            teams.add(tuple(sorted((team, opponent))))
            observed_teams.add(team)
            dates.add(_date(row["game_date"]))
            results = {row.get("game_result"), row.get("wl")} - {None}
            if not results <= {"W", "L"} or len(results) > 1:
                return set()
            if results:
                winners.add(team if "W" in results else opponent)
        if (
            len(teams) != 1
            or len(observed_teams) != 2
            or len(winners) != 1
            or len(dates) != 1
        ):
            return set()
        series[teams.pop()].append((dates.pop(), game_id, winners.pop()))

    teams_in_round = {team for pair in series for team in pair}
    if len(series) != expected_series or len(teams_in_round) != 2 * expected_series:
        return set()
    for series_games in series.values():
        _, _, final_winner = max(series_games)
        wins = sum(winner == final_winner for _, _, winner in series_games)
        if not 4 <= len(series_games) <= 7 or wins != 4:
            return set()
    return teams_in_round


def derive_nonparticipation_records(evidence: Evidence) -> list[dict[str, Any]]:
    """Verify round closure and reconcile appearances with every observed team."""
    if not evidence.complete or evidence.cohort_complete is not True:
        return []
    evidence.validate()
    regular_seasons = set()
    season_players: dict[tuple[str, int], list[Mapping[str, Any]]] = defaultdict(list)
    playoff_games: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in evidence.rows:
        if row["season_type"] == "Regular Season":
            regular_seasons.add(row["season"])
        if row["season_type"] in ("Regular Season", "Playoffs"):
            season_players[row["season"], row["player_id"]].append(row)
        if row["season_type"] == "Playoffs":
            playoff_games[row["season"], row["game_id"]].append(row)

    rounds: dict[tuple[str, str], dict[str, list[Mapping[str, Any]]]] = defaultdict(
        dict
    )
    conflicting_rounds: set[tuple[str, str | None]] = set()
    for (season, game_id), rows in playoff_games.items():
        labels = {row_playoff_round(row) for row in rows}
        if len(labels) != 1:
            conflicting_rounds.update((season, label) for label in labels)
        elif (label := labels.pop()) is not None:
            rounds[season, label][game_id] = rows

    records = []
    for (season, label), games in sorted(rounds.items()):
        if season not in regular_seasons or (season, label) in conflicting_rounds:
            continue
        number = PLAYOFF_ROUNDS.index(label) + 1
        round_teams = _completed_round_teams(games, 2 ** (4 - number))
        if not round_teams:
            continue
        event = PLAYOFF_ROUND_LABELS[label]
        for (player_season, player_id), rows in sorted(season_players.items()):
            if player_season != season:
                continue
            teams = {r.get("team_abbr") for r in rows}
            season_teams = sorted(
                team for team in teams if isinstance(team, str) and team.strip()
            )
            if teams & round_teams or len(season_teams) != len(teams):
                continue
            rounds_played = {
                row_playoff_round(r) for r in rows if r["season_type"] == "Playoffs"
            }
            if None in rounds_played or any(r["game_id"] in games for r in rows):
                continue
            prior = [
                r
                for r in rows
                if r["season_type"] == "Regular Season"
                or row_playoff_round(r) in PLAYOFF_ROUNDS[: number - 1]
            ]
            if not prior:
                continue
            latest = max(prior, key=lambda r: (r["game_date"], r["game_id"]))
            team_list = ", ".join(season_teams)
            records.append(
                {
                    "season": season,
                    "event": event,
                    "player_id": player_id,
                    "player_name": latest["player_name"],
                    "season_teams": season_teams,
                    "participated": False,
                    "is_verified_complete_cohort": True,
                    "reason": f"Recorded season teams ({team_list}) did not reach the {event}.",
                    "source": evidence.source,
                    "snapshot_id": evidence.snapshot_id,
                    "data_through": evidence.data_through[season, "Playoffs"],
                    "round_game_ids": sorted(games),
                }
            )
    return records


def _requested_mention(
    text: str, aliases: Iterable[str], request_words: str
) -> str | None:
    names = sorted(set(aliases) - {""}, key=lambda name: (-len(name), name))
    if not names:
        return None
    match = re.compile(r"\b(?:" + "|".join(map(re.escape, names)) + r")\b")
    found = list(match.finditer(text))
    if len(found) != 1:
        return None
    remainder = match.sub(" ", text)
    remainder = re.sub(r"20\d{2}[-–/]\d{2,4}", " ", remainder)
    remainder = re.sub(r"[’']s\b", " ", remainder)
    remainder = re.sub(r"\b(?:" + request_words + r")\b", " ", remainder)
    return found[0][0] if not remainder.strip(" ?.!,") else None


def _answer(
    record: Mapping[str, Any], conversation_id: str | None, *, curated: bool
) -> dict[str, Any]:
    answer = (
        f"{record['player_name']} did not participate in the "
        f"{record['season']} {record['event']}. {record['reason']} "
    )
    if curated:
        answer += (
            "Finals statistics are not applicable, rather than missing or zero. "
            f"[Verified participation source]({record['source_url']})."
        )
    else:
        answer += "Statistics for this round are not applicable."
    return answer_payload(
        answer,
        status="not_applicable",
        conversation_id=conversation_id,
        participation_evidence={
            **record,
            "contract_version": (
                "event_participation/1" if curated else "event_participation/2"
            ),
        },
        agent_plan={
            "route": "event_participation",
            "confidence": 1.0,
            "needs_clarification": False,
        },
        assumptions=[
            "This is a sourced participation fact, not an inference from missing box scores. Other players and seasons require their own verified records."
            if curated
            else "The source covers the full cohort and records a decided series for every matchup in the requested round."
        ],
    )


def answer_known_nonparticipation(
    question: str,
    season: str,
    conversation_id: str | None = None,
    *,
    evidence: Evidence | None = None,
    programmatic_records: Iterable[Mapping[str, Any]] | None = None,
    include_curated: bool = True,
) -> dict[str, Any] | None:
    """Answer a single-player, single-round request using verified facts."""
    question = normalize_playoff_round_year(question)
    text = question.lower()
    if re.search(
        r"\b(?:east|west|if|would|could|versus|vs|compare|before|after)\b", text
    ):
        return None
    try:
        seasons = requested_seasons(question, season)
    except SemanticError:
        return None
    if len(seasons) != 1:
        return None
    catalog = (
        [r for r in load_participation_catalog() if r["season"] == seasons[0]]
        if include_curated
        else []
    )
    if re.search(r"\bfinals?\b", text) and not re.search(r"\bconference\b", text):
        for record in catalog:
            if _requested_mention(
                text, (a.lower() for a in record["aliases"]), _REQUEST_WORDS
            ):
                return _answer(record, conversation_id, curated=True)

    round_key = requested_playoff_round(question)
    if not round_key:
        return None
    event = PLAYOFF_ROUND_LABELS[round_key]

    if evidence is not None:
        if not evidence.complete or evidence.cohort_complete is not True:
            return None
        players = source_players(evidence)
    elif programmatic_records is not None:
        programmatic_records = list(programmatic_records)
        # A fact table is not an identity cohort. Require a full name here.
        players = list(
            {
                r["player_id"]: {
                    "player_id": r["player_id"],
                    "player_name": r["player_name"],
                }
                for r in programmatic_records
                if r.get("season") == seasons[0]
            }.values()
        )
    else:
        return None

    wording = playoff_round_planning_question(question)
    wording = re.sub(r"20\d{2}[-–/]\d{2,4}|[’']s\b", " ", wording)
    mention = _requested_mention(
        normalize_player_text(wording),
        (
            normalize_player_text(name)
            for player in players
            for name in (player["player_name"], *player.get("aliases", ()))
        ),
        _PROGRAMMATIC_WORDS,
    )
    if mention is None:
        return None
    match = resolve_player_name(mention, players)
    if match["status"] != "ok":
        return None
    player_id = match["matches"][0]["player_id"]
    records = (
        derive_nonparticipation_records(evidence)
        if evidence is not None
        else programmatic_records or ()
    )
    matching = [
        r
        for r in records
        if (r.get("season"), r.get("event", r.get("event_name")), r.get("player_id"))
        == (seasons[0], event, player_id)
    ]
    if len(matching) == 1 and (
        matching[0].get("participated") is False
        and matching[0].get("is_verified_complete_cohort") is True
        and matching[0].get("reason")
    ):
        curated = next(
            (r for r in catalog if r["event"] == event and r["player_id"] == player_id),
            None,
        )
        if curated is not None:
            return _answer(curated, conversation_id, curated=True)
        candidate = matching[0]
        game_ids = candidate.get("round_game_ids")
        if (
            isinstance(game_ids, (list, tuple))
            and game_ids
            and all(
                isinstance(value, str) and value.strip()
                for value in (
                    candidate.get("source"),
                    candidate.get("snapshot_id"),
                    *game_ids,
                )
            )
        ):
            return _answer(
                {**candidate, "event": event}, conversation_id, curated=False
            )
    return None
