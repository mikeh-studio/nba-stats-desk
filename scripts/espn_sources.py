"""Strict, provider-labelled ESPN dev normalization and PDF comparison."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import date, datetime
from zoneinfo import ZoneInfo

from nba_api.stats.static.teams import get_teams
from scripts.gamebook_sources import season_bounds
from scripts.gamebook_store import require
from scripts.parse_gamebooks import key

VERSION = "espn-dev/1"
BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
TEAM_NAMES = {t["abbreviation"]: t["full_name"] for t in get_teams()}
ALIASES = {
    "NY": "NYK",
    "GS": "GSW",
    "SA": "SAS",
    "NO": "NOP",
    "WSH": "WAS",
    "UTAH": "UTA",
}


def team_code(value):
    code = ALIASES.get(value, value)
    require(code in TEAM_NAMES, f"Unknown team: {value}")
    return code


def identifier(value):
    require(
        isinstance(value, str) and bool(re.fullmatch(r"[1-9]\d{0,15}", value)),
        "Invalid ESPN ID",
    )
    return value


def integer(value):
    require(
        not isinstance(value, bool) and bool(re.fullmatch(r"\d+", str(value))),
        "Invalid nonnegative integer",
    )
    return int(value)


def event_game(event, season, day):
    require(
        event["season"]["year"] == int(season[:4]) + 1 and event["season"]["type"] == 2,
        "Wrong ESPN season/phase",
    )
    eid = identifier(event["id"])
    require(len(event["competitions"]) == 1, "Ambiguous competition")
    comp = event["competitions"][0]
    require(comp["id"] == eid, "Competition ID differs")
    timestamp = datetime.fromisoformat(comp["date"].replace("Z", "+00:00"))
    require(timestamp.tzinfo is not None, "Game timestamp needs timezone")
    local_date = timestamp.astimezone(ZoneInfo("America/New_York")).date()
    require(local_date == day, "Event outside requested league date")
    status = comp["status"]["type"]
    require(isinstance(status["completed"], bool), "Invalid completion flag")
    final = (
        status["completed"]
        and status["state"] == "post"
        and status["name"] == "STATUS_FINAL"
    )
    require(not status["completed"] or final, "Conflicting final status")
    teams = comp["competitors"]
    require(
        len(teams) == 2 and {t["homeAway"] for t in teams} == {"home", "away"},
        "Invalid competitors",
    )
    result = {
        "provider": "espn",
        "source_game_id": eid,
        "season": season,
        "phase": "Regular Season",
        "date": str(day),
        "final": final,
    }
    for t in teams:
        side = t["homeAway"]
        code = team_code(t["team"]["abbreviation"])
        result[side] = code
        result[side + "_source_team_id"] = identifier(t["team"]["id"])
        result[side + "_score"] = integer(t["score"]) if final else None
    require(result["home"] != result["away"], "Same team on both sides")
    if final:
        require(result["home_score"] != result["away_score"], "Tied final score")
    return result


def scoreboard(payload, season, day):
    first, last = season_bounds(season)
    require(first <= day <= last, "Date outside season")
    require(
        payload["leagues"][0]["season"]["displayName"] == season,
        "Unexpected scoreboard season",
    )
    events = payload["events"]
    require(isinstance(events, list) and len(events) <= 30, "Scoreboard bound exceeded")
    games = []
    for event in events:
        if event["season"]["type"] != 2:
            continue
        games.append(event_game(event, season, day))
    require(
        len({g["source_game_id"] for g in games}) == len(games), "Duplicate ESPN games"
    )
    return games


def game_mapping(game, records):
    """Join scoped date/home/away, confirm score; never invent an NBA ID."""
    matches = [
        r
        for r in records
        if r.get("Date") == game["date"]
        and r.get("HomeTeamAbbreviation") == game["home"]
        and r.get("AwayTeamAbbreviation") == game["away"]
    ]
    require(len(matches) <= 1, "Ambiguous NBA game mapping")
    if not matches:
        return None
    row = matches[0]
    gid = row["GameId"]
    require(
        isinstance(gid, str)
        and bool(re.fullmatch(r"002" + game["season"][2:4] + r"\d{5}", gid)),
        "Wrong NBA game season/phase",
    )
    require(
        row["HomePoints"] == game["home_score"]
        and row["AwayPoints"] == game["away_score"],
        "Mapping score conflict",
    )
    require(
        sum(r.get("GameId") == gid for r in records) == 1, "Duplicate NBA mapping ID"
    )
    return gid


def player_index(reference):
    result = defaultdict(set)
    for row in reference:
        result[key(row["full_name"])].add(str(row["id"]))
    return result


def clock(value, period):
    # ESPN expresses the final subminute clock as e.g. "0.0".
    parts = value.split(":")
    seconds = float(parts[-1]) + (60 * int(parts[0] or "0") if len(parts) == 2 else 0)
    require(
        len(parts) in (1, 2)
        and 0 <= float(parts[-1]) < 60
        and 0 <= seconds <= (720 if period <= 4 else 300),
        "Invalid play clock",
    )
    return round(seconds, 2)


def normalize_box(payload, game, reference):
    """Validate a completed result and player box without requiring PBP."""
    header = payload["header"]
    require(header["id"] == game["source_game_id"], "Summary game differs")
    confirmed = event_game(header, game["season"], date.fromisoformat(game["date"]))
    require(
        confirmed == game and game["final"],
        "Summary scope/status/score differs from discovery",
    )
    index = player_index(reference)
    players = []
    seen = set()
    sides = set()
    stat_keys = ["PTS", "REB", "AST", "TO", "STL", "BLK", "OREB", "DREB", "PF"]
    for team in payload["boxscore"]["players"]:
        code = team_code(team["team"]["abbreviation"])
        side = "home" if code == game["home"] else "away"
        require(
            code == game[side] and code not in sides, "Unexpected/duplicate box team"
        )
        require(
            team["team"]["id"] == game[side + "_source_team_id"], "Box team ID differs"
        )
        sides.add(code)
        require(len(team["statistics"]) == 1, "Unexpected stat groups")
        group = team["statistics"][0]
        labels = group["labels"]
        require(len(set(labels)) == len(labels), "Duplicate stat labels")
        require(
            set(stat_keys + ["MIN", "FG", "3PT", "FT", "+/-"]) <= set(labels),
            "Missing required box fields",
        )
        for item in group["athletes"]:
            athlete = item["athlete"]
            pid = identifier(athlete["id"])
            name = athlete["displayName"]
            require(pid not in seen and bool(key(name)), "Duplicate/empty player")
            seen.add(pid)
            ids = index.get(key(name), set())
            nba_id = next(iter(ids)) if len(ids) == 1 else None
            row = {
                "source_player_id": pid,
                "nba_player_id": nba_id,
                "name": name,
                "team": code,
                "played": not item["didNotPlay"],
                "stats": None,
            }
            if row["played"]:
                require(len(item["stats"]) == len(labels), "Missing player stat cells")
                raw = dict(zip(labels, item["stats"]))
                stats = {k: integer(raw[k]) for k in stat_keys}
                stats["MIN"] = raw["MIN"]
                stats["PLUS_MINUS"] = int(raw["+/-"])
                for label, made, attempted in [
                    ("FG", "FGM", "FGA"),
                    ("3PT", "FG3M", "FG3A"),
                    ("FT", "FTM", "FTA"),
                ]:
                    values = raw[label].split("-")
                    require(len(values) == 2, "Invalid shooting cell")
                    stats[made], stats[attempted] = map(integer, values)
                    require(stats[made] <= stats[attempted], "Made exceeds attempts")
                require(
                    stats["FG3M"] <= stats["FGM"] and stats["FG3A"] <= stats["FGA"],
                    "Invalid three point subset",
                )
                require(
                    stats["PTS"] == 2 * stats["FGM"] + stats["FG3M"] + stats["FTM"],
                    "Player scoring arithmetic mismatch",
                )
                require(
                    stats["REB"] == stats["OREB"] + stats["DREB"],
                    "Player rebound mismatch",
                )
                row["stats"] = stats
            players.append(row)
    require(sides == {game["home"], game["away"]}, "Missing box team")
    active = [p for p in players if p["played"]]
    require(len(active) > 0, "Empty player box score")
    mapped = [p["nba_player_id"] for p in active if p["nba_player_id"]]
    require(len(set(mapped)) == len(mapped), "Duplicate NBA player mapping")
    for side in ("home", "away"):
        require(
            sum(p["stats"]["PTS"] for p in active if p["team"] == game[side])
            == game[side + "_score"],
            "Player/team points mismatch",
        )
    return sorted(players, key=lambda p: p["source_player_id"])


def normalize_summary(payload, game, reference):
    players = normalize_box(payload, game, reference)
    active = [p for p in players if p["played"]]
    plays = payload["plays"]
    require(0 < len(plays) <= 3000, "Invalid play count")
    seen_plays = set()
    edges = []
    points = Counter()
    assists = Counter()
    periods = set()
    for play in plays:
        event_id = identifier(play["id"])
        require(event_id not in seen_plays, "Duplicate play ID")
        seen_plays.add(event_id)
        period = integer(play["period"]["number"])
        require(1 <= period <= 10, "Invalid period")
        periods.add(period)
        seconds = clock(play["clock"]["displayValue"], period)
        if play["scoringPlay"]:
            code = next(
                (
                    game[s]
                    for s in ("home", "away")
                    if game[s + "_source_team_id"] == play.get("team", {}).get("id")
                ),
                None,
            )
            require(code is not None, "Unknown scoring team")
            value = integer(play["scoreValue"])
            require(value in (1, 2, 3), "Invalid scoring points")
            points[code] += value
        if "assist" not in play["text"].lower():
            continue
        match = re.fullmatch(r"(.+?) makes .+ \((.+?) assists\)", play["text"])
        require(
            match is not None and play["scoringPlay"] and play["scoreValue"] in (2, 3),
            "Unsupported assisted play",
        )
        resolved = []
        for name in match.groups():
            found = [
                p for p in active if key(p["name"]) == key(name) and p["team"] == code
            ]
            require(len(found) == 1, "Unresolved assisted-play participant")
            resolved.append(found[0])
        scorer, assister = resolved
        require(
            scorer["source_player_id"] != assister["source_player_id"], "Self assist"
        )
        participants = {p["athlete"]["id"] for p in play.get("participants", [])}
        require(
            {scorer["source_player_id"], assister["source_player_id"]} <= participants,
            "Text/participant IDs differ",
        )
        assists[assister["source_player_id"]] += 1
        edges.append(
            {
                "source_event_id": event_id,
                "period": period,
                "clock_seconds": seconds,
                "team": code,
                "points": play["scoreValue"],
                "scorer_source_id": scorer["source_player_id"],
                "assister_source_id": assister["source_player_id"],
                "scorer_nba_id": scorer["nba_player_id"],
                "assister_nba_id": assister["nba_player_id"],
                "text": play["text"],
            }
        )
    require(
        periods == set(range(1, max(periods) + 1)) and max(periods) >= 4,
        "Incomplete periods",
    )
    require(
        plays[-1]["homeScore"] == game["home_score"]
        and plays[-1]["awayScore"] == game["away_score"],
        "Final PBP score differs",
    )
    require(
        all(points[game[s]] == game[s + "_score"] for s in ("home", "away")),
        "PBP scoring total differs",
    )
    require(
        all(assists[p["source_player_id"]] == p["stats"]["AST"] for p in active),
        "Player assist/PBP mismatch",
    )
    return {
        "schema_version": VERSION,
        "game": game,
        "players": sorted(players, key=lambda p: p["source_player_id"]),
        "assists": sorted(edges, key=lambda e: e["source_event_id"]),
        "play_count": len(plays),
    }


def pdf_game(candidate):
    g = candidate["game"]
    require(candidate.get("nba_game_id"), "No NBA game mapping")
    return {
        "game_id": candidate["nba_game_id"],
        "season": g["season"],
        "date": g["date"],
        "home": g["home"],
        "away": g["away"],
        "home_name": TEAM_NAMES[g["home"]],
        "away_name": TEAM_NAMES[g["away"]],
    }


def pdf_identity(candidate):
    g = candidate["game"]
    reference = {
        "reference_kind": "identity_only_unique_normalized_name_match_nba_api_static",
        "season": g["season"],
        "date": g["date"],
        "season_type": g["phase"],
        "home_team_abbreviation": g["home"],
        "away_team_abbreviation": g["away"],
        "stats": {"Home": {"FullGame": []}, "Away": {"FullGame": []}},
    }
    for p in candidate["players"]:
        if not p["played"]:
            continue
        require(p["nba_player_id"], "Unmapped active NBA player")
        side = "Home" if p["team"] == g["home"] else "Away"
        reference["stats"][side]["FullGame"].append(
            {"Name": p["name"], "EntityId": p["nba_player_id"]}
        )
    return reference


def compare_pdf(candidate, parsed):
    require(
        parsed["status"] == "reconciled" and not parsed["errors"],
        "PDF internal reconciliation failed",
    )
    require(parsed["game"] == pdf_game(candidate), "PDF scope differs")
    pdf_players = {p["player_id"]: p for p in parsed["player_validation"]}
    active = [p for p in candidate["players"] if p["played"]]
    require(
        set(pdf_players) == {p["nba_player_id"] for p in active},
        "PDF/ESPN participant coverage differs",
    )
    for p in active:
        q = pdf_players[p["nba_player_id"]]
        require(p["team"] == q["team"], "PDF player team differs")
        for pdf_key, espn_key in [
            ("PTS", "PTS"),
            ("FG", "FGM"),
            ("3P", "FG3M"),
            ("FT", "FTM"),
            ("A", "AST"),
        ]:
            require(
                q["stats"][pdf_key] == p["stats"][espn_key],
                f"PDF/ESPN player {pdf_key} differs",
            )

    def pdf_key(e):
        return (
            e["period"],
            clock(e["clock"], e["period"]),
            e["team"],
            e["points"],
            e["scorer_id"],
            e["assister_id"],
        )

    def espn_key(e):
        return (
            e["period"],
            e["clock_seconds"],
            e["team"],
            e["points"],
            e["scorer_nba_id"],
            e["assister_nba_id"],
        )

    require(
        Counter(map(pdf_key, parsed["assists"]))
        == Counter(map(espn_key, candidate["assists"])),
        "PDF/ESPN assist event mismatch",
    )
    lookup = defaultdict(list)
    for e in parsed["assists"]:
        lookup[pdf_key(e)].append(e)
    matches = []
    for e in candidate["assists"]:
        p = lookup[espn_key(e)].pop(0)
        matches.append(
            {
                "source_event_id": e["source_event_id"],
                "pdf_event_id": p["event_id"],
                "page": p["page"],
                "url": parsed["source_url"] + "#page=" + str(p["page"]),
            }
        )
    return matches
