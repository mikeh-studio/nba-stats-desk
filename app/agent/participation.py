"""Sourced playoff participation facts. Absent records never prove absence."""

import json
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.agent.alias_builder import normalize_player_text
from app.agent.catalog import load_semantic_catalog
from app.agent.payload import answer_payload
from app.agent.player_suggestions import resolve_player_name
from app.agent.scope_policy import (
    METRICS,
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
from app.agent.source_policy import get_source_policy

_REQUEST_WORDS = (
    r"nba|finals?|in|the|this|season|did|does|play|participate|reach|make|it|to|"
    r"how|many|what|were|was|his|points?|pts|rebounds?|assists?|stats|statistics|"
    r"per|game|score|scored|averages?|averaged|averaging"
)
_PROGRAMMATIC_WORDS = (
    _REQUEST_WORDS + r"|playoffs|of|during|total|totals|games|log|perform|performance|"
    r"appear|appeared|played|record|put up|any|minutes|steals|blocks|turnovers|"
    r"(?:last|previous|prior|current) season"
)
_MODIFIER_RE = re.compile(
    r"\b(?:east|west|if|would|could|versus|vs|compare|before|after)\b",
    re.I,
)
_PLAYOFF_SERIES_GAME_RE = re.compile(r"004[0-9]{2}00[1-4][0-9]([1-7])")
_VOCABULARY_METRIC_TOKENS = (
    "ppg",
    "rpg",
    "apg",
    "mpg",
    "spg",
    "bpg",
    "rbs",
    "blcks",
    "tos",
    "fg",
    "ft",
    "3p",
    "3pt",
    "pct",
    "ts",
    "efg",
    "bsi",
    "per36",
    "oreb",
    "dreb",
    "pf",
    "stl",
    "blk",
    "tov",
    "pts",
    "reb",
    "ast",
    "min",
    "fgm",
    "fga",
    "fg3m",
    "fg3a",
    "ftm",
    "fta",
    "gp",
)


@lru_cache(maxsize=1)
def _metric_alias_patterns() -> tuple[re.Pattern[str], ...]:
    literals: set[str] = set(_VOCABULARY_METRIC_TOKENS)
    for metric in load_semantic_catalog().list_metrics():
        for raw in (metric["key"], metric["label"], *metric.get("aliases", ())):
            text = str(raw).strip().lower()
            if not text:
                continue
            literals.add(text)
            literals.add(text.replace("_", " "))
            normalized = normalize_player_text(text)
            if normalized:
                literals.add(normalized)
    ordered_literals = sorted(literals, key=lambda item: (-len(item), item))
    return tuple(
        re.compile(r"(?<!\w)(?:" + pattern + r")(?!\w)", re.I)
        for pattern in (
            *METRICS.values(),
            "|".join(map(re.escape, ordered_literals)),
        )
        if pattern
    )


def _strip_metric_aliases(text: str) -> str:
    cleaned = text
    for pattern in _metric_alias_patterns():
        cleaned = pattern.sub(" ", cleaned)
    return cleaned


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


def _series_game_number(game_id: str, rows: Sequence[Mapping[str, Any]]) -> int | None:
    explicit: set[int] = set()
    for row in rows:
        for field in ("playoff_series_game_number", "series_game_number"):
            if field in row and row[field] is not None:
                value = row[field]
                if type(value) is not int or not 1 <= value <= 7:
                    return None
                explicit.add(value)
    if explicit:
        return next(iter(explicit)) if len(explicit) == 1 else None
    match = _PLAYOFF_SERIES_GAME_RE.fullmatch(game_id)
    return int(match[1]) if match else None


def _evidence_participants(evidence: Evidence) -> Sequence[Mapping[str, Any]]:
    participants = getattr(evidence, "game_participants", ())
    if participants:
        return participants
    return getattr(evidence, "participants", ())


def _completed_round_teams(
    games: Mapping[str, list[Mapping[str, Any]]],
    expected_series: int,
    *,
    season: str | None = None,
    game_participants: Sequence[Mapping[str, Any]] = (),
) -> set[str]:
    participants_by_game: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for item in game_participants:
        item_season = item.get("season")
        if item_season is not None and season is not None and item_season != season:
            continue
        gid = item.get("game_id")
        if isinstance(gid, str):
            participants_by_game[gid].append(item)

    series: dict[tuple[str, str], list[tuple[date, int, str, str]]] = defaultdict(list)
    for game_id, rows in games.items():
        teams = set()
        observed_teams = set()
        observed_by_team: dict[str, set[int]] = defaultdict(set)
        observed_pairs: set[tuple[int, str]] = set()
        dates = set()
        winners = set()
        for row in rows:
            team, opponent = row.get("team_abbr"), row.get("opponent_abbr")
            if not team or not opponent or team == opponent:
                return set()
            teams.add(tuple(sorted((team, opponent))))
            observed_teams.add(team)
            observed_by_team[team].add(row["player_id"])
            observed_pairs.add((row["player_id"], team))
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
        game_number = _series_game_number(game_id, rows)
        if game_number is None:
            return set()
        manifest = participants_by_game.get(game_id)
        if manifest:
            expected_played: set[tuple[int, str]] = set()
            expected_not_played: set[tuple[int, str]] = set()
            for entry in manifest:
                pid = entry.get("player_id")
                team = entry.get("team_abbr")
                played = entry.get("played")
                if played is True:
                    if type(pid) is not int or team not in observed_teams:
                        return set()
                    expected_played.add((pid, team))
                elif played is False and type(pid) is int and isinstance(team, str):
                    expected_not_played.add((pid, team))
            if (
                {team for _, team in expected_played} != observed_teams
                or not expected_played <= observed_pairs
                or bool(expected_not_played & observed_pairs)
            ):
                return set()
        elif any(len(observed_by_team[team]) < 5 for team in observed_teams):
            return set()
        series[teams.pop()].append((dates.pop(), game_number, game_id, winners.pop()))

    teams_in_round = {team for pair in series for team in pair}
    if len(series) != expected_series or len(teams_in_round) != 2 * expected_series:
        return set()
    for series_games in series.values():
        _, _, _, final_winner = max(series_games, key=lambda g: (g[0], g[2]))
        wins = sum(winner == final_winner for _, _, _, winner in series_games)
        game_numbers = [num for _, num, _, _ in series_games]
        if (
            not 4 <= len(series_games) <= 7
            or wins != 4
            or len(set(game_numbers)) != len(series_games)
            or set(game_numbers) != set(range(1, len(series_games) + 1))
        ):
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
    observed_player_games: set[tuple[str, str, int]] = set()
    game_seasons: dict[str, set[str]] = defaultdict(set)
    for row in evidence.rows:
        observed_player_games.add((row["season"], row["game_id"], row["player_id"]))
        game_seasons[row["game_id"]].add(row["season"])
        if row["season_type"] == "Regular Season":
            regular_seasons.add(row["season"])
        if row["season_type"] in ("Regular Season", "Playoffs"):
            season_players[row["season"], row["player_id"]].append(row)
        if row["season_type"] == "Playoffs":
            playoff_games[row["season"], row["game_id"]].append(row)

    for gr in getattr(evidence, "game_results", ()):
        if isinstance(gr.get("game_id"), str) and isinstance(gr.get("season"), str):
            game_seasons[gr["game_id"]].add(gr["season"])

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

    for gr in getattr(evidence, "game_results", ()):
        if gr.get("season_type") == "Playoffs":
            gr_season = gr.get("season")
            gr_label = row_playoff_round(gr)
            if (
                isinstance(gr_season, str)
                and gr_label is not None
                and (gr_season, gr.get("game_id")) not in playoff_games
            ):
                conflicting_rounds.add((gr_season, gr_label))

    participants = _evidence_participants(evidence)
    observed_seasons = {s for s, _ in season_players}
    single_season = next(iter(observed_seasons)) if len(observed_seasons) == 1 else None
    missing_box_players: dict[str, set[int]] = defaultdict(set)
    manifest_teams: dict[tuple[str, int], set[str]] = defaultdict(set)
    manifest_round_games: dict[tuple[str, int], set[str]] = defaultdict(set)
    for item in participants:
        if item.get("played") is not True:
            continue
        pid = item.get("player_id")
        gid = item.get("game_id")
        if type(pid) is not int or not isinstance(gid, str):
            continue
        p_season = item.get("season")
        if p_season is None:
            seasons_for_game = game_seasons.get(gid, set())
            p_season = (
                next(iter(seasons_for_game))
                if len(seasons_for_game) == 1
                else single_season
            )
        if not isinstance(p_season, str):
            continue
        if (p_season, gid, pid) not in observed_player_games:
            missing_box_players[p_season].add(pid)
        team = item.get("team_abbr")
        if isinstance(team, str) and team.strip():
            manifest_teams[p_season, pid].add(team)
        manifest_round_games[p_season, pid].add(gid)

    records = []
    for (season, label), games in sorted(rounds.items()):
        if season not in regular_seasons or (season, label) in conflicting_rounds:
            continue
        number = PLAYOFF_ROUNDS.index(label) + 1
        round_teams = _completed_round_teams(
            games,
            2 ** (4 - number),
            season=season,
            game_participants=participants,
        )
        if not round_teams:
            continue
        event = PLAYOFF_ROUND_LABELS[label]
        for (player_season, player_id), rows in sorted(season_players.items()):
            if player_season != season:
                continue
            if player_id in missing_box_players[season]:
                continue
            if manifest_teams[season, player_id] & round_teams:
                continue
            if manifest_round_games[season, player_id] & set(games):
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
    remainder = _strip_metric_aliases(remainder)
    if _MODIFIER_RE.search(remainder):
        return None
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
    players: Iterable[Mapping[str, Any]] | None = None,
    programmatic_records: Iterable[Mapping[str, Any]] | None = None,
    include_curated: bool = True,
    source_policy: Any = None,
) -> dict[str, Any] | None:
    """Answer a single-player, single-round request using verified facts."""
    question = normalize_playoff_round_year(question)
    round_key = requested_playoff_round(question)
    if not round_key:
        return None
    wording = playoff_round_planning_question(question)
    if _MODIFIER_RE.search(_strip_metric_aliases(wording)):
        return None
    try:
        seasons = requested_seasons(question, season)
    except SemanticError:
        return None
    if len(seasons) != 1:
        return None
    authorized_players = list(players) if players is not None else None
    policy = get_source_policy(source_policy)
    allowed_ids = (
        {p["player_id"] for p in authorized_players}
        if authorized_players is not None
        else policy.allowed_player_ids
        if policy is not None
        else None
    )
    catalog = (
        [
            r
            for r in load_participation_catalog()
            if r["season"] == seasons[0]
            and (allowed_ids is None or r["player_id"] in allowed_ids)
        ]
        if include_curated
        else []
    )
    if round_key == "nba_finals":
        curated_text = wording.lower()
        for record in catalog:
            if _requested_mention(
                curated_text,
                (a.lower() for a in record["aliases"]),
                _PROGRAMMATIC_WORDS,
            ):
                return _answer(record, conversation_id, curated=True)

    event = PLAYOFF_ROUND_LABELS[round_key]

    if evidence is not None:
        if not evidence.complete or evidence.cohort_complete is not True:
            return None
        roster = (
            authorized_players
            if authorized_players is not None
            else source_players(evidence, source_policy=policy)
        )
    elif programmatic_records is not None:
        programmatic_records = list(programmatic_records)
        # A fact table is not an identity cohort. Require a full name here.
        if authorized_players is not None:
            roster = authorized_players
        else:
            roster = list(
                {
                    r["player_id"]: {
                        "player_id": r["player_id"],
                        "player_name": r["player_name"],
                    }
                    for r in programmatic_records
                    if r.get("season") == seasons[0]
                    and (allowed_ids is None or r.get("player_id") in allowed_ids)
                }.values()
            )
    else:
        return None
    if not roster:
        return None

    cleaned_wording = re.sub(r"20\d{2}[-–/]\d{2,4}|[’']s\b", " ", wording)
    # Strip symbolic metric aliases (e.g. TS%, +/-) before text normalization drops punctuation.
    cleaned_wording = _strip_metric_aliases(cleaned_wording)
    mention = _requested_mention(
        normalize_player_text(cleaned_wording),
        (
            normalize_player_text(name)
            for player in roster
            for name in (player["player_name"], *player.get("aliases", ()))
        ),
        _PROGRAMMATIC_WORDS,
    )
    if mention is None:
        return None
    match = resolve_player_name(mention, roster)
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
