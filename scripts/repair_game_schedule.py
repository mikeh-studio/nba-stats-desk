"""Reconcile a statistics repair with official designated home/away assignments."""

from collections import defaultdict
from datetime import date, timedelta

from app.agent.semantic_source import snapshot_digest, snapshot_evidence


def reconcile_schedule(snapshot, document):
    season = {row["season"] for row in snapshot["rows"]}
    if len(season) != 1 or document["leagueSchedule"]["seasonYear"] not in season:
        raise ValueError("Schedule season mismatch")
    totals = defaultdict(int)
    dates = {}
    for row in snapshot["rows"]:
        totals[(row["game_id"], row["team_abbr"])] += row["pts"]
        dates[(row["game_id"], row["team_abbr"])] = row["game_date"]
    mapping = {}
    for day in document["leagueSchedule"]["gameDates"]:
        for game in day["games"]:
            gid = game["gameId"]
            for side, other, venue in [
                ("homeTeam", "awayTeam", "HOME"),
                ("awayTeam", "homeTeam", "AWAY"),
            ]:
                own, opp = game[side], game[other]
                team, opponent = own["teamTricode"], opp["teamTricode"]
                key = (gid, team)
                if key not in totals:
                    continue
                game_date = game["gameDateEst"][:10]
                if (
                    key in mapping
                    or game["gameStatus"] != 3
                    or game_date != dates[key]
                    or int(own["score"]) != totals[key]
                    or (gid, opponent) not in totals
                ):
                    raise ValueError("Schedule/statistics game mismatch")
                mapping[key] = dict(
                    game_id=gid,
                    team_abbr=team,
                    opponent_abbr=opponent,
                    home_away=venue,
                    schedule_date=game_date,
                    season=next(iter(season)),
                    game_status="final",
                    source_updated_at_utc=snapshot["source_timestamp"],
                    ingested_at_utc=snapshot["source_timestamp"],
                    is_neutral=game.get("isNeutral"),
                )
    if set(mapping) != set(totals):
        raise ValueError("Schedule does not cover every repaired team-game")
    previous = {}
    schedule = []
    for row in sorted(
        mapping.values(),
        key=lambda row: (row["schedule_date"], row["game_id"], row["team_abbr"]),
    ):
        day = date.fromisoformat(row["schedule_date"])
        team = row["team_abbr"]
        row["is_back_to_back"] = previous.get(team) == day - timedelta(days=1)
        previous[team] = day
        schedule.append({k: v for k, v in row.items() if k != "is_neutral"})
    changes = []
    rows = []
    for row in snapshot["rows"]:
        game = mapping[(row["game_id"], row["team_abbr"])]
        if row["home_away"] != game["home_away"]:
            changes.append(
                dict(
                    game_id=row["game_id"],
                    player_id=row["player_id"],
                    before=row["home_away"],
                    after=game["home_away"],
                )
            )
        rows.append(
            dict(
                row,
                home_away=game["home_away"],
                source_matchup=row.get("source_matchup", row.get("matchup")),
                matchup=f"{row['team_abbr']} {'vs.' if game['home_away'] == 'HOME' else '@'} {row['opponent_abbr']}",
            )
        )
    repaired = dict(
        snapshot,
        rows=rows,
        capture=dict(
            snapshot["capture"],
            schedule_sha256=snapshot_digest(document),
            venue_basis="official_schedule_designation_including_neutral_sites",
        ),
    )
    repaired["sha256"] = snapshot_digest(repaired)
    snapshot_evidence(repaired)
    return (
        repaired,
        schedule,
        dict(
            changed_assignments=changes,
            team_games=len(schedule),
            neutral_team_games=sum(
                row["is_neutral"] is True for row in mapping.values()
            ),
            schedule_sha256=snapshot_digest(document),
        ),
    )
