"""Governed rebounding observations for a named player and exact matchup."""

import re
from dataclasses import dataclass
from datetime import date

from app.agent.clock import today
from app.agent.overview_calendar import MONTH_PATTERN, MONTHS
from app.agent.semantics import SemanticError, aggregate

_REQUEST = re.compile(
    r"(?:(?:what (?:stands|stood) out about) (?P<player>.+?)['’]s rebounding"
    r"|how unusual was (?P<other_player>.+?)['’]s (?P<stated>\d{1,3})[- ]rebound game) "
    r"against (?:the )?(?P<opponent>.+?) on "
    rf"(?:(?P<iso>\d{{4}}-\d{{2}}-\d{{2}})|(?P<month>{MONTH_PATTERN}) "
    r"(?P<day>\d{1,2}),? (?P<year>20\d{2}))"
    r"(?: \((?P<phase>regular season|playoffs)\))?[?.!]?",
    re.I,
)


@dataclass(frozen=True)
class ReboundingGame:
    player: str
    opponent: str
    day: str
    phase: str
    stated: int | None

    @property
    def dates(self):
        return dict(window="date_range", start_date=self.day, as_of=self.day)

    def opponent_abbr(self):
        return resolve_opponent_abbr(self.opponent)


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


def _match(question: str):
    wording = " ".join(question.partition("\nSelected player: ")[0].split())
    return _REQUEST.fullmatch(wording)


def is_rebounding_game(question: str) -> bool:
    return _match(question) is not None


def parse_rebounding_game(question: str) -> ReboundingGame | None:
    if not (match := _match(question)):
        return None
    try:
        day = (
            date.fromisoformat(match["iso"])
            if match["iso"]
            else date(
                int(match["year"]),
                MONTHS[match["month"].lower().rstrip(".")],
                int(match["day"]),
            )
        )
    except ValueError as exc:
        raise SemanticError(
            "clarification_required", "Please give a valid game date."
        ) from exc
    if day > today():
        raise SemanticError(
            "unsupported_coverage", "Please give a game date no later than today."
        )
    phase = (
        "Playoffs"
        if (match["phase"] or "").casefold() == "playoffs"
        else "Regular Season"
    )
    return ReboundingGame(
        match["player"] or match["other_player"],
        match["opponent"],
        day.isoformat(),
        phase,
        int(match["stated"]) if match["stated"] else None,
    )


def rebounding_context(row, rows, metric):
    counts = [row.get(key) for key in ("reb", "oreb", "dreb")]
    split = None
    if all(
        value is not None and value >= 0 and int(value) == value for value in counts
    ):
        total, offensive, defensive = map(int, counts)
        if total == offensive + defensive:
            split = {"offensive": offensive, "defensive": defensive}
    earlier = [
        game
        for game in rows
        if game["player_id"] == row["player_id"]
        and game["season"] == row["season"]
        and game["season_type"] == row["season_type"]
        and str(game["game_date"]) < str(row["game_date"])
    ]
    baseline = aggregate(earlier, metric, "average")
    return {
        "split": split,
        "previous": (
            {
                "average": baseline["value"],
                "appearances": len(earlier),
                "first": min(str(game["game_date"]) for game in earlier),
                "last": max(str(game["game_date"]) for game in earlier),
                "at_least_current": sum(
                    float(game["reb"]) >= float(row["reb"]) for game in earlier
                )
                if row.get("reb") is not None
                else None,
            }
            if baseline["value"] is not None and not baseline["missing_component_games"]
            else None
        ),
    }
