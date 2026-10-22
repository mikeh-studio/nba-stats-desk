"""Bounded automated discovery and identity capture; never infer missing IDs."""

from __future__ import annotations

import json
import re
import time
from datetime import date, datetime
from urllib.parse import urlencode

import requests
from scripts.fetch_gamebook_identities import validate
from scripts.fetch_gamebooks import digest, now, write_json

SCHEDULE_URLS = (
    "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2_1.json",
    "https://stats.nba.com/stats/scheduleleaguev2?",
)
MAX_JSON_BYTES = 10 * 1024 * 1024


class SourceUnavailable(RuntimeError):
    """Retryable source failure, with evidence saved separately."""


def capture_json(url, directory, session):
    directory.mkdir(parents=True, exist_ok=False)
    record = {"url": url, "started_at": now(), "status": "failed"}
    body = bytearray()
    started = time.monotonic()
    try:
        with session.get(
            url, timeout=(10, 20), stream=True, allow_redirects=False
        ) as r:
            record["http_status"] = r.status_code
            for chunk in r.iter_content(65536):
                if len(body) + len(chunk) > MAX_JSON_BYTES:
                    raise ValueError("Response exceeds 10 MiB")
                body.extend(chunk)
                if time.monotonic() - started > 40:
                    raise ValueError("Response duration limit")
            if r.status_code != 200:
                raise ValueError(f"HTTP {r.status_code}")
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise ValueError("Expected JSON object")
            record["status"] = "captured"
            return payload
    except (requests.RequestException, ValueError) as exc:
        record["error_type"] = type(exc).__name__
        raise SourceUnavailable(
            f"Source unavailable; see {directory / 'attempt.json'}"
        ) from exc
    finally:
        record.update(finished_at=now(), sha256=digest(body), bytes=len(body))
        (directory / "response.body").write_bytes(body)
        write_json(directory / "attempt.json", record)


def season_bounds(season):
    if not re.fullmatch(r"\d{4}-\d{2}", season):
        raise ValueError("Invalid season")
    year = int(season[:4])
    if season[-2:] != str(year + 1)[-2:]:
        raise ValueError("Invalid season suffix")
    return date(year, 7, 1), date(year + 1, 6, 30)


def schedule_games(payload, season, as_of):
    """Use NBA's league-local schedule date, never the UTC timestamp's date."""
    start, end = season_bounds(season)
    league = payload["leagueSchedule"]
    if league["seasonYear"] != season or not league["gameDates"]:
        raise ValueError("Empty schedule or unexpected season")
    games, seen = [], set()
    for day in league["gameDates"]:
        day_text = day["gameDate"]
        try:
            local_day = datetime.strptime(day_text.split()[0], "%m/%d/%Y").date()
        except ValueError:
            local_day = date.fromisoformat(day_text[:10])
        if not start <= local_day <= end:
            raise ValueError("Schedule date outside season")
        for row in day["games"]:
            gid = row["gameId"]
            if (
                not isinstance(gid, str)
                or not re.fullmatch(r"\d{10}", gid)
                or gid in seen
            ):
                raise ValueError("Invalid or duplicate schedule game ID")
            seen.add(gid)
            if not gid.startswith("002"):
                continue  # v1 parser/storage contract: regular season only.
            if gid[3:5] != season[2:4]:
                raise ValueError("Game ID season differs")
            if str(row["gameStatus"]) != "3" or local_day > as_of:
                continue
            game = {"game_id": gid, "date": local_day.isoformat(), "season": season}
            for side in ("home", "away"):
                team = row[f"{side}Team"]
                code = team["teamTricode"]
                name = f"{team['teamCity']} {team['teamName']}".strip()
                if not re.fullmatch(r"[A-Z]{3}", code) or not name:
                    raise ValueError("Invalid schedule team")
                game[side], game[f"{side}_name"] = code, name
            if game["home"] == game["away"]:
                raise ValueError("Schedule teams must differ")
            games.append(game)
    if not seen:
        raise ValueError("Schedule has no games")
    if len(games) > 1500:
        raise ValueError("Completed regular-season schedule exceeds bound")
    return sorted(games, key=lambda g: (g["date"], g["game_id"]))


def discover(season, as_of, directory, session):
    errors = []
    for i, base in enumerate(SCHEDULE_URLS):
        url = (
            base + urlencode({"LeagueID": "00", "Season": season})
            if base.endswith("?")
            else base
        )
        try:
            payload = capture_json(url, directory / str(i), session)
            games = schedule_games(payload, season, as_of)
            write_json(directory / "manifest.json", games)
            write_json(
                directory / "selection.json",
                {
                    "season": season,
                    "as_of": str(as_of),
                    "url": url,
                    "games": len(games),
                    "prior_failures": errors,
                },
            )
            return games
        except (SourceUnavailable, ValueError, KeyError, TypeError) as exc:
            errors.append(
                {"source": i, "error_type": type(exc).__name__, "detail": str(exc)}
            )
    write_json(directory / "failure.json", errors)
    raise SourceUnavailable(
        "All schedule sources failed; this is not an empty game day"
    )


def normalize_identity(reference, game):
    """Only names/IDs affect revisions; provider statistics/timestamps do not."""
    validate(reference, game)
    result = {
        "season": game["season"],
        "date": game["date"],
        "season_type": "Regular Season",
        "stats": {},
    }
    seen = set()
    for side in ("home", "away"):
        result[f"{side}_team_abbreviation"] = game[side]
        entries = []
        for p in reference["stats"][side.title()]["FullGame"]:
            pid = str(p["EntityId"])
            # PBP Stats includes a non-player aggregate row in each team's list.
            # Ignore that explicit sentinel only; a player with a missing ID fails.
            if pid == "0" and p["Name"].strip().lower() == "team":
                continue
            if (
                not pid.isdigit()
                or int(pid) <= 0
                or pid in seen
                or not p["Name"].strip()
            ):
                raise ValueError("Invalid or duplicate player identity")
            seen.add(pid)
            entries.append({"EntityId": pid, "Name": p["Name"].strip()})
        if not entries:
            raise ValueError("No canonical players in team reference")
        result["stats"][side.title()] = {
            "FullGame": sorted(entries, key=lambda p: p["EntityId"])
        }
    return result


def boxscore_identity(payload, game):
    source = payload["game"]
    if (
        source["gameId"] != game["game_id"]
        or str(source["gameStatus"]) != "3"
        or source["gameTimeLocal"][:10] != game["date"]
    ):
        raise ValueError("Boxscore game/status/date differs")
    reference = {
        "season": game["season"],
        "date": game["date"],
        "season_type": "Regular Season",
        "stats": {},
    }
    for side in ("home", "away"):
        team = source[f"{side}Team"]
        reference[f"{side}_team_abbreviation"] = team["teamTricode"]
        reference["stats"][side.title()] = {
            "FullGame": [
                {"EntityId": str(p["personId"]), "Name": p["name"]}
                for p in team["players"]
            ]
        }
    return normalize_identity(reference, game)


def identities(game, directory, session):
    urls = (
        f"https://cdn.nba.com/static/json/liveData/boxscore/boxscore_{game['game_id']}.json",
        "https://api.pbpstats.com/get-game-stats?"
        + urlencode({"GameId": game["game_id"], "Type": "Player"}),
    )
    errors = []
    for i, url in enumerate(urls):
        try:
            payload = capture_json(url, directory / str(i), session)
            reference = (
                boxscore_identity(payload, game)
                if i == 0
                else normalize_identity(payload, game)
            )
            body = (
                json.dumps(reference, sort_keys=True, separators=(",", ":")) + "\n"
            ).encode()
            (directory / "identity.json").write_bytes(body)
            write_json(
                directory / "selection.json",
                {
                    "url": url,
                    "sha256": digest(body),
                    "prior_failures": errors,
                    "usage": "identity only",
                },
            )
            return body
        except (SourceUnavailable, ValueError, KeyError, TypeError) as exc:
            errors.append(
                {"source": i, "error_type": type(exc).__name__, "detail": str(exc)}
            )
    write_json(directory / "failure.json", errors)
    raise SourceUnavailable("No validated game-specific identities")
