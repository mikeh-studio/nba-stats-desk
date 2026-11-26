"""Exact-date player analysis, using earlier appearances and verified game assets."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, TypedDict

from app.agent.clock import today_in
from app.agent.overview_calendar import MONTHS
from app.agent.semantics import Evidence, SemanticError, aggregate, load_contract

_EXACT_DAY = r"\d{4}-\d{2}-\d{2}|[a-z]+\.? \d{1,2},? 20\d{2}"
_DAY = rf"yesterday|last night|today|{_EXACT_DAY}"
_MATCHUP = r"(?:against|versus|vs\.?) (?:the )?"
_QUESTION = re.compile(
    r"how (?:well )?did (?P<player>.+?) (?:play(?:ed)?|perform(?:ed)?|do) "
    rf"(?:(?:{_MATCHUP}(?P<opponent_before>.+?) )?(?:on )?(?P<day>{_DAY})"
    rf"(?: {_MATCHUP}(?P<opponent_after>.+?))?)"
    r"(?: (?:\(|in (?:the )?)?(?P<phase>regular season|playoffs)\)?)?[?.!]?",
    re.I,
)
_REBOUNDING_QUESTION = re.compile(
    r"(?:(?:what (?:stands|stood) out about) (?P<player>.+?)['’]s rebounding"
    r"|how unusual was (?P<other_player>.+?)['’]s (?P<stated>\d{1,3})[- ]rebound game) "
    rf"{_MATCHUP}(?P<opponent_before>.+?) on (?P<day>{_EXACT_DAY})"
    r"(?: \((?P<phase>regular season|playoffs)\))?[?.!]?",
    re.I,
)
_METRICS = ("pts", "reb", "ast", "stl", "blk", "tov", "min", "oreb", "dreb", "fg_pct")
_ROSTER_METRICS = tuple(key for key in _METRICS if key != "fg_pct")
_CONTRIBUTION = ("pts", "reb", "ast", "oreb", "dreb", "stl", "blk")
_LABELS = {
    "pts": "Points",
    "reb": "Rebounds",
    "ast": "Assists",
    "stl": "Steals",
    "blk": "Blocks",
    "tov": "Turnovers",
    "min": "Minutes",
    "oreb": "Offensive rebounds",
    "dreb": "Defensive rebounds",
    "fg_pct": "Field goal percentage",
}
_MEANINGFUL_CHANGE = {"pts": 5, "reb": 3, "ast": 3, "stl": 3, "blk": 3}
_STANDOUT_SCALE = {"pts": 10, "reb": 4, "ast": 4, "stl": 3, "blk": 3}
_RATE = "Player-credited OREB / (player-credited OREB + opponent player-credited DREB)"


@dataclass(frozen=True)
class GameQuestion:
    player: str
    day: str
    phase: str
    opponent: str | None
    relative: bool
    focus: str | None
    stated_rebounds: int | None

    @property
    def dates(self) -> dict[str, str]:
        return {"window": "date_range", "start_date": self.day, "as_of": self.day}


def _match(question: str) -> re.Match[str] | None:
    wording = " ".join(question.partition("\nSelected player: ")[0].split())
    return _QUESTION.fullmatch(wording) or _REBOUNDING_QUESTION.fullmatch(wording)


def wants_game_performance(question: str) -> bool:
    return _match(question) is not None


def parse_game_question(question: str) -> GameQuestion | None:
    if not (match := _match(question)):
        return None
    clock = today_in(load_contract().timezone)
    wording = match["day"].casefold()
    relative = wording in {"today", "yesterday", "last night"}
    try:
        if relative:
            day = clock - timedelta(days=wording != "today")
        elif "-" in wording:
            day = date.fromisoformat(wording)
        else:
            month, number, year = wording.replace(",", "").split()
            day = date(int(year), MONTHS[month.rstrip(".")], int(number))
    except (KeyError, ValueError) as exc:
        raise SemanticError(
            "clarification_required", "Please give a valid game date."
        ) from exc
    if day > clock:
        raise SemanticError(
            "unsupported_coverage",
            f"{day} has not occurred on the NBA Eastern calendar.",
        )
    fields = match.groupdict()
    before, after = fields.get("opponent_before"), fields.get("opponent_after")
    if before and after:
        raise SemanticError(
            "clarification_required", "Please specify one opponent for the game."
        )
    phase = (
        "Playoffs"
        if (match["phase"] or "").casefold() == "playoffs"
        else "Regular Season"
    )
    return GameQuestion(
        fields.get("player") or fields["other_player"],
        day.isoformat(),
        phase,
        before or after,
        relative,
        "reb" if match.re is _REBOUNDING_QUESTION else None,
        int(stated) if (stated := fields.get("stated")) is not None else None,
    )


def resolve_opponent_abbr(opponent: str) -> str:
    # The packaged directory resolves aliases. The source facts verify the game.
    from nba_api.stats.static.teams import get_teams

    name = opponent.casefold()
    teams = [
        team["abbreviation"]
        for team in get_teams()
        if name
        in {
            team[key].casefold()
            for key in ("abbreviation", "city", "full_name", "nickname")
        }
    ]
    if len(teams) != 1:
        raise SemanticError(
            "clarification_required",
            "Please name the opponent with its full NBA team name or abbreviation.",
        )
    return teams[0]


def plan_game_performance(question, season, selected_player=None):
    from app.agent.scoring_report import game_log_plan

    request = parse_game_question(question)
    if request is None:
        return None
    scope = dict(request.dates, season=season, season_type=request.phase)
    if request.opponent:
        scope["opponent_abbr"] = resolve_opponent_abbr(request.opponent)
    plan = game_log_plan(
        selected_player or request.player, request.focus or "pts", scope
    )
    plan["include_game_performance"] = True
    plan["message"] = (
        "Single-game rebounding" if request.focus else "Single-game player performance"
    )
    if request.stated_rebounds is not None:
        plan["stated_rebounds"] = request.stated_rebounds
    return plan


class GamePerformance(TypedDict):
    kind: str
    version: str
    game: dict[str, Any]
    player: dict[str, Any]
    focus_metric: str | None
    metrics: list[dict[str, Any]]
    baseline: dict[str, Any] | None
    contribution: list[dict[str, Any]]
    team_rebounding: dict[str, Any]
    rosters: dict[str, Any]
    warnings: list[str]
    rebounding_context: dict[str, Any] | None


def _official_result(
    evidence: Evidence, player: Mapping[str, Any]
) -> Mapping[str, Any]:
    games = [
        row
        for row in evidence.game_results
        if row["game_id"] == player["game_id"]
        and row["season"] == player["season"]
        and str(row["game_date"]) == str(player["game_date"])
        and row["season_type"] == player["season_type"]
        and {row["home_team_abbr"], row["away_team_abbr"]}
        == {player["team_abbr"], player["opponent_abbr"]}
        and row["home_team_abbr"] != row["away_team_abbr"]
        and all(
            type(row.get(f"{side}_score")) is int and row[f"{side}_score"] >= 0
            for side in ("home", "away")
        )
        and row["home_score"] != row["away_score"]
    ]
    return games[0] if len(games) == 1 else {}


def _complete_rosters(evidence: Evidence, game, team, opponent):
    if not game:
        return None
    appearances = [row for row in evidence.rows if row["game_id"] == game["game_id"]]
    participants = [
        row
        for row in evidence.game_participants
        if row["game_id"] == game["game_id"] and row["played"] is True
    ]
    found = {(row["player_id"], row["team_abbr"]) for row in appearances}
    expected = {(row["player_id"], row["team_abbr"]) for row in participants}
    if (
        {abbr for _, abbr in found} != {team, opponent}
        or found != expected
        or len(found) != len(appearances)
        or len(expected) != len(participants)
    ):
        return None
    sides = {
        abbr: [row for row in appearances if row["team_abbr"] == abbr]
        for abbr in (team, opponent)
    }
    for side in ("home", "away"):
        roster = sides[game[f"{side}_team_abbr"]]
        if not roster or _count(roster, "pts") != game[f"{side}_score"]:
            return None
    return sides


def _count(rows, key):
    values = [row.get(key) for row in rows]
    return (
        sum(values)
        if values and all(type(value) is int and value >= 0 for value in values)
        else None
    )


def _credited_rebounding(sides, team, opponent):
    def side_rate(own, other):
        oreb, allowed = _count(sides[own], "oreb"), _count(sides[other], "dreb")
        if oreb is None or allowed is None or not oreb + allowed:
            return None
        return {
            "team_abbr": own,
            "oreb": oreb,
            "opponent_dreb": allowed,
            "rate": oreb / (oreb + allowed),
        }

    first, second = (
        (side_rate(team, opponent), side_rate(opponent, team))
        if sides
        else (None, None)
    )
    return {
        "status": "ok" if first is not None and second is not None else "unavailable",
        "formula": _RATE,
        "team": first if second is not None else None,
        "opponent": second if first is not None else None,
    }


def build_game_performance(
    evidence: Evidence,
    section: dict[str, Any],
    *,
    focus: str | None,
    stated_rebounds: int | None = None,
) -> GamePerformance | None:
    appearances = section["rows"]
    if len(appearances) != 1:
        return None
    selected = appearances[0]
    row = next(
        entry
        for entry in evidence.rows
        if entry["game_id"] == selected["game_id"]
        and entry["player_id"] == selected["player_id"]
        and entry["season"] == section["scope"]["season"]
    )
    before = [
        entry
        for entry in evidence.rows
        if entry["player_id"] == row["player_id"]
        and entry["season"] == row["season"]
        and entry["season_type"] == row["season_type"]
        and str(entry["game_date"]) < str(row["game_date"])
    ]
    contract = load_contract()
    keys = (
        (
            "reb",
            "oreb",
            "dreb",
            *[key for key in _METRICS if key not in {"reb", "oreb", "dreb"}],
        )
        if focus == "reb"
        else _METRICS
    )
    metrics = []
    for key in keys:
        metric = contract.metric(key)
        ratio = bool(metric.denominator)
        raw_value = aggregate([row], metric, "ratio" if ratio else "total")["value"]
        prior = aggregate(before, metric, "ratio" if ratio else "average")
        scale = 100 if ratio else 1
        value = raw_value * scale if raw_value is not None else None
        baseline = (
            prior["value"] * scale
            if prior["value"] is not None and not prior["missing_component_games"]
            else None
        )
        metrics.append(
            {
                "key": key,
                "label": _LABELS[key],
                "value": value,
                "baseline": baseline,
                "change": value - baseline
                if value is not None and baseline is not None
                else None,
                "unit": "%" if ratio else metric.unit,
                "change_unit": "percentage points" if ratio else metric.unit,
                "baseline_games": prior["valid_games"],
            }
        )

    team, opponent = row["team_abbr"], row["opponent_abbr"]
    official = _official_result(evidence, row)
    from nba_api.stats.static.teams import get_teams

    labels = {
        entry["abbreviation"]: entry["full_name"]
        for entry in get_teams()
        if entry["abbreviation"] in {team, opponent}
    }
    game = {
        "game_id": row["game_id"],
        "game_date": str(row["game_date"]),
        "season": row["season"],
        "phase": row["season_type"],
        "team_abbr": team,
        "opponent_abbr": opponent,
        "team_name": labels.get(team),
        "opponent_name": labels.get(opponent),
        **{
            key: official.get(key)
            for key in ("home_team_abbr", "away_team_abbr", "home_score", "away_score")
        },
    }
    sides = _complete_rosters(evidence, official, team, opponent)
    contribution = []
    if sides:
        for key in _CONTRIBUTION:
            total = _count(sides[team], key)
            value = row.get(key)
            if total and value is not None:
                contribution.append(
                    {
                        "key": key,
                        "label": _LABELS[key],
                        "value": value,
                        "team_total": total,
                        "share": value / total,
                        "unit": contract.metric(key).unit,
                    }
                )
    rates = _credited_rebounding(sides, team, opponent)
    warnings = []
    if not before:
        warnings.append(
            "No earlier recorded appearances in the same season and phase in this source."
        )
    if not official:
        warnings.append(
            "An independently verified final score is unavailable for this game."
        )
    if not sides:
        warnings.append(
            "Both played rosters could not be verified against the final score. Team shares and roster detail are unavailable."
        )
    if rates["status"] != "ok":
        warnings.append(
            "The player-credited team offensive rebound rate comparison is unavailable."
        )
    if rates["status"] == "ok":
        warnings.append(
            "The offensive rebound rates use player-credited rebounds. Team and dead-ball rebounds are excluded."
        )

    def roster(abbr):
        return (
            [
                {
                    key: entry.get(key)
                    for key in (
                        "player_id",
                        "player_name",
                        "team_abbr",
                        *_ROSTER_METRICS,
                    )
                }
                for entry in sorted(
                    sides[abbr],
                    key=lambda value: (
                        -value["pts"],
                        value["player_name"],
                        value["player_id"],
                    ),
                )
            ]
            if sides
            else []
        )

    baseline = (
        {
            "first": min(str(entry["game_date"]) for entry in before),
            "last": max(str(entry["game_date"]) for entry in before),
            "games": len(before),
        }
        if before
        else None
    )
    rebounding: dict[str, Any] | None = None
    if focus == "reb":
        total, offensive, defensive = (row.get(key) for key in ("reb", "oreb", "dreb"))
        rebound_metric = next(entry for entry in metrics if entry["key"] == "reb")
        split = (
            {"offensive": int(offensive), "defensive": int(defensive)}
            if total is not None
            and offensive is not None
            and defensive is not None
            and all(
                value >= 0 and int(value) == value
                for value in (total, offensive, defensive)
            )
            and total == offensive + defensive
            else None
        )
        rebounding = {
            "split": split,
            "previous": (
                {
                    "average": rebound_metric["baseline"],
                    "appearances": baseline["games"],
                    "first": baseline["first"],
                    "last": baseline["last"],
                    "at_least_current": (
                        sum(float(entry["reb"]) >= float(total) for entry in before)
                        if total is not None
                        else None
                    ),
                }
                if baseline and rebound_metric["baseline"] is not None
                else None
            ),
        }
        if stated_rebounds is not None:
            rebounding["premise_differs"] = (
                total is not None and total != stated_rebounds
            )

    return {
        "kind": "game_performance",
        "version": "game_performance/1",
        "game": game,
        "player": {key: row[key] for key in ("player_id", "player_name", "team_abbr")},
        "focus_metric": focus,
        "metrics": metrics,
        "baseline": baseline,
        "rebounding_context": rebounding,
        "contribution": contribution,
        "team_rebounding": rates,
        "rosters": {
            "status": "ok" if sides else "unavailable",
            "team": roster(team),
            "opponent": roster(opponent),
        },
        "warnings": warnings,
    }


def _standout(section):
    metrics = {row["key"]: row for row in section["metrics"]}
    core = [
        metrics[key]
        for key in ("pts", "reb", "ast")
        if metrics[key]["value"] is not None
    ]
    defensive = [
        metrics[key]
        for key in ("stl", "blk")
        if metrics[key]["value"] is not None
        and metrics[key]["value"] >= 5
        and (
            metrics[key]["value"] >= 7
            or (metrics[key]["change"] or 0) >= _MEANINGFUL_CHANGE[key]
        )
    ]
    positive = [
        row
        for row in core
        if row["change"] is not None and row["change"] >= _MEANINGFUL_CHANGE[row["key"]]
    ]
    if (
        not positive
        and not defensive
        and metrics["pts"]["value"] is not None
        and metrics["pts"]["value"] >= 40
    ):
        return metrics["pts"]

    def priority(row):
        key = row["key"]
        scale = _STANDOUT_SCALE[key]
        if key in {"stl", "blk"}:
            return max(row["change"] or 0, row["value"] - 4) / scale
        if row["change"] is None:
            return 0
        return abs(row["change"]) / max(row["baseline"], scale)

    return max(positive + defensive or core, key=priority, default=None)


def _other_stats(section, key):
    stats = []
    for metric in section["metrics"]:
        if (
            metric["key"] not in {"pts", "reb", "ast"} - {key}
            or metric["value"] is None
        ):
            continue
        text = f"{metric['value']:g} {metric['label'].lower()}"
        change = metric["change"]
        if change is not None and change <= -_MEANINGFUL_CHANGE[metric["key"]]:
            text += f" ({abs(change):.1f} below his pregame average)"
        stats.append(text)
    if len(stats) > 2:
        return ", ".join(stats[:-1]) + f", and {stats[-1]}"
    return " and ".join(stats)


def _rebounding_summary(section, name):
    if context := section.get("rebounding_context"):
        game = section["rows"][0]
        if game["display_value"] is not None:
            total = game["display_value"]
            answer = f"{name} had {total:g} rebounds against {game['opponent_abbr']} on {game['game_date']}."
            if context.get("premise_differs"):
                answer = "The box score records a different total. " + answer
            if split := context["split"]:
                answer += f" The split was {split['offensive']} offensive and {split['defensive']} defensive."
            if previous := context["previous"]:
                gap = total - previous["average"]
                games = previous["appearances"]
                phase = (
                    "playoff" if game["season_type"] == "Playoffs" else "regular-season"
                )
                answer += (
                    f" He averaged {previous['average']:.1f} rebounds across his "
                    f"{games} earlier {phase} {'appearance' if games == 1 else 'appearances'} in this dataset."
                )
                if abs(gap) >= 0.05:
                    answer += f" This game was {abs(gap):.1f} rebounds {'above' if gap > 0 else 'below'} that average."
                if games >= 5:
                    answer += (
                        f" He had at least {total:g} rebounds in {previous['at_least_current']} "
                        f"of those {games} previous appearances."
                    )
            return answer


def render_game_performance(section):
    from app.agent.payload import answer_payload
    from app.agent.performance_overview import scoped_identity_profile

    game, player = section["game"], section["player"]
    name, team, opponent = (
        player["player_name"],
        game["team_abbr"],
        game["opponent_abbr"],
    )
    matchup = f" against {opponent}" if opponent else ""
    standout = next(
        (
            row
            for row in section["metrics"]
            if row["key"] == section["focus_metric"] and row["value"] is not None
        ),
        None,
    ) or _standout(section)
    section["lead_metric"] = section["focus_metric"] or (
        standout["key"] if standout else None
    )
    summary = _rebounding_summary(section, name)
    paragraphs = [summary] if summary else []
    if not summary:
        if standout:
            lead = f"{name} recorded {standout['value']:g} {standout['label'].lower()}{matchup} on {game['game_date']}."
            if standout["change"] is not None:
                baseline = section["baseline"]
                gap = standout["change"]
                comparison = (
                    f"{abs(gap):.1f} {'above' if gap > 0 else 'below'}"
                    if abs(gap) >= 0.05
                    else "in line with"
                )
                lead += (
                    f" That was {comparison} the {standout['baseline']:.1f} average across "
                    f"{baseline['games']} earlier {'appearance' if baseline['games'] == 1 else 'appearances'} "
                    "in the same season and phase in this source."
                )
            other = _other_stats(section, standout["key"])
            paragraphs.append(lead + (f" He also had {other}." if other else ""))
        else:
            paragraphs.append(
                f"{name} has a recorded appearance{matchup} on {game['game_date']}."
            )

    if game["home_score"] is not None:
        home = team == game["home_team_abbr"]
        first, second = (
            (game["home_score"], game["away_score"])
            if home
            else (game["away_score"], game["home_score"])
        )
        result = f"{team} {'beat' if first > second else 'lost to'} {opponent} {first}–{second}."
        contribution = next(
            (
                row
                for row in section["contribution"]
                if row["key"] == section["lead_metric"]
            ),
            None,
        )
        if contribution:
            result += f" {name} accounted for {contribution['share']:.1%} of {team}’s {contribution['label'].lower()} ({contribution['value']:g} of {contribution['team_total']:g})."
        paragraphs.append(result)
    rates = section["team_rebounding"]
    if rates["status"] == "ok":
        paragraphs.append(
            f"The player-credited offensive rebound rate was {rates['team']['rate']:.1%} for "
            f"{team} and {rates['opponent']['rate']:.1%} for {opponent}."
        )
    scope = section["scope"]
    profile = scoped_identity_profile(
        player,
        section["rows"],
        {
            "season": game["season"],
            "start": game["game_date"],
            "end": game["game_date"],
            "phase": game["phase"],
        },
    )
    return answer_payload(
        "\n\n".join(paragraphs),
        status="ok",
        player_profile=profile,
        semantic_evidence=section,
        assumptions=[
            f"{game['season']} {game['phase']} · {game['game_date']} · Game {game['game_id']}.",
            *section["warnings"],
        ],
        followups=[
            f"Show his {next(row['label'].lower() for row in section['metrics'] if row['key'] == (section['lead_metric'] or 'pts'))} game by game."
        ],
        conversation_context={
            "analysis_type": "game_performance",
            "players": [player],
            "scope": scope,
            "metrics": [section["lead_metric"] or "pts"],
        },
    )
