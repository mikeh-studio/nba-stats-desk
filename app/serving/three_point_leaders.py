"""One season-to-date Ask capability over a verified weekly publication."""

from dataclasses import replace

import yaml

from app.agent.payload import answer_payload
from app.agent.reference_planner import LEADER_GAME_SHARE, conversation_leader_plan
from app.agent.semantic_answer import leader_statement
from app.agent.semantics import Query, run_query
from app.seasons import SEASON_FILE


def asks_for_three_point_leaders(question, season, context=None):
    plan = conversation_leader_plan(question, context or {}, season)
    if not plan or plan.get("leader_game_share") != LEADER_GAME_SHARE:
        return False
    query = plan["queries"][0]
    return (
        query["season"] == season
        and not query.get("team_abbr")
        and query["metric"] == "fg3m"
        and query["season_type"] == "Regular Season"
    )


def validate_leader_source(scope, accepted, models):
    """Compare every materialized make and game membership to accepted box scores."""
    opening = yaml.safe_load(SEASON_FILE.read_text())["regular_season_starts"][
        scope["season"]
    ]
    if scope["first"] != str(opening) or scope["phase"] != "Regular Season":
        raise ValueError("Three-point rankings require coverage from opening night")
    games = {
        g["game_id"]: {g["home_team_abbr"], g["away_team_abbr"]}
        for g in accepted["games"]
    }
    if (
        len(games) != len(accepted["games"])
        or any(len(teams) != 2 for teams in games.values())
        or len(set().union(*games.values())) != 30
    ):
        raise ValueError("Three-point rankings require the full league")
    expected = {}
    teams_by_game = {}
    for row in accepted["appearances"]:
        game, player, team = row["game_id"], row["player_id"], row["team_abbr"]
        makes, attempts = row["fg3m"], row["fg3a"]
        if (
            (game, player) in expected
            or team not in games.get(game, ())
            or not (
                type(makes) is int and type(attempts) is int and 0 <= makes <= attempts
            )
        ):
            raise ValueError("Invalid three-point appearance")
        expected[game, player] = (team, makes)
        teams_by_game.setdefault(game, set()).add(team)
    facts = models["fct_player_game_stats"]
    if (
        teams_by_game != games
        or len(facts) != len(expected)
        or any(
            type(row["fg3m"]) is not int
            or row["season"] != scope["season"]
            or row["season_type"] != scope["phase"]
            or not scope["first"] <= str(row["game_date"]) <= scope["last"]
            for row in facts
        )
        or {(r["game_id"], r["player_id"]): (r["team_abbr"], r["fg3m"]) for r in facts}
        != expected
    ):
        raise ValueError(
            "Three-point leaderboard facts differ from accepted appearances"
        )


def leader_answer(evidence, manifest):
    scope = manifest["scope"]
    query = Query(
        "fg3m",
        scope["season"],
        "average",
        season_type=scope["phase"],
        operation="rank",
        as_of=scope["last"],
    )
    section = run_query(replace(evidence, cohort_complete=True), query)
    rows = section["rows"]
    if not rows:
        return answer_payload(
            "No players met the minimum games requirement.",
            status="unsupported_coverage",
            publication_id=manifest["publication_id"],
        )
    names = {r["player_id"]: r["player_name"] for r in evidence.rows}
    text = " ".join(
        leader_statement(section, section["metric"], section["scope"], names)
    )
    pid = manifest["publication_id"]
    return answer_payload(
        text,
        status="ok",
        publication_id=pid,
        conversation_context={
            "scope": section["scope"],
            "players": [],
            "metrics": [section["metric"]["key"]],
            "answer_summary": text,
        },
        assumptions=[
            f"{scope['season']} regular season through {scope['last']} · Publication {pid}.",
            "Minimum 65% of team games. Per-game rates are ranked before rounding.",
        ],
        tables=[
            {
                "title": "Three-pointers made per game",
                "columns": [
                    {"key": k, "label": label}
                    for k, label in (
                        ("rank", "Rank"),
                        ("player", "Player"),
                        ("per_game", "3PM/G"),
                        ("total", "3PM"),
                        ("games", "GP"),
                    )
                ],
                "rows": [
                    [
                        r["rank"],
                        names[r["player_id"]],
                        f"{r['display_value']:.1f}",
                        int(r["numerator"]),
                        r["valid_games"],
                    ]
                    for r in rows
                ],
            }
        ],
    )
