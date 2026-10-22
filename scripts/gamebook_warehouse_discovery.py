"""Read-only completed-game discovery from the existing NBA API game-log landing."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from nba_api.stats.static.teams import get_teams
from scripts.fetch_gamebooks import write_json
from scripts.gamebook_sources import season_bounds
from scripts.gamebook_store import require

MAX_GROUPS = 6000
TEAM_NAMES = {t["abbreviation"]: t["full_name"] for t in get_teams()}


def table_ref(project, dataset, table):
    require(
        bool(re.fullmatch(r"[a-z][a-z0-9-]{4,62}", project)), "Invalid source project"
    )
    require(
        bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,1023}", dataset)),
        "Invalid source dataset",
    )
    require(table in ("raw_game_logs", "pipeline_run_log"), "Unapproved source table")
    return f"{project}.{dataset}.{table}"


def discovery_window(season, as_of, mode, start_date=None, end_date=None):
    start, end = season_bounds(season)
    require(mode in ("daily", "backfill"), "Invalid discovery mode")
    if mode == "daily":
        require(
            start_date is None and end_date is None,
            "Daily discovery uses the whole observed season",
        )
        require(
            start <= as_of.date() <= end,
            "Daily discovery must use the active calendar season",
        )
        return start, as_of.date()
    require(
        start_date is not None and end_date is not None,
        "Backfill needs explicit start and end dates",
    )
    first, last = date.fromisoformat(start_date), date.fromisoformat(end_date)
    require(
        start <= first <= last <= min(end, as_of.date()),
        "Backfill dates outside observed season",
    )
    require((last - first).days < 31, "Backfill discovery is limited to 31 days")
    return first, last


def check_freshness(runs, as_of, max_age_hours=36):
    require(
        len(runs) == 1,
        "No upstream NBA API run for requested season; not a no-game day",
    )
    row = runs[0]
    stamp = row["finished_at_utc"]
    finished = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    require(
        finished.tzinfo is not None, "Upstream run timestamp must be timezone aware"
    )
    require(
        timedelta(0) <= as_of - finished <= timedelta(hours=max_age_hours),
        "Upstream NBA API ingestion is stale or future-dated",
    )
    require(row["status"] == "success", "Latest upstream NBA API run did not succeed")
    details = json.loads(row.get("details") or "{}")
    require(isinstance(details, dict), "Malformed upstream run details")
    require(
        details.get("stats_status", "success") == "success",
        "Upstream stats publication failed",
    )
    return {
        "dag_run_id": row["dag_run_id"],
        "finished_at_utc": finished.isoformat(),
        "watermark_after": str(row.get("watermark_after")),
        "max_age_hours": max_age_hours,
    }


def completed_games(rows, season, start, end):
    """One consistent home/away pair with opposing final W/L outcomes per game."""
    require(
        len(rows) <= MAX_GROUPS, "Source grouping exceeded limit; refusing truncation"
    )
    by_game = defaultdict(list)
    errors = []
    for row in rows:
        by_game[row.get("game_id")].append(row)
    games = []
    for gid, records in by_game.items():
        try:
            require(
                isinstance(gid, str) and bool(re.fullmatch(r"002\d{7}", gid)),
                "Invalid regular-season game ID",
            )
            require(gid[3:5] == season[2:4], "Game ID season differs")
            days = {str(r["game_date"]) for r in records}
            require(len(days) == 1, "Conflicting game dates")
            day = date.fromisoformat(next(iter(days)))
            require(start <= day <= end, "Game date outside discovery scope")
            # GROUP BY removes player-row repetition, but not conflicting matchups/outcomes.
            contexts = {(r["matchup"], r["wl"]) for r in records}
            require(len(contexts) == 2, "Expected two consistent team outcomes")
            home, away = None, None
            outcomes = set()
            for matchup, wl in contexts:
                require(wl in ("W", "L"), "Missing final W/L result")
                match = re.fullmatch(r"([A-Z]{3}) (vs\.|@) ([A-Z]{3})", matchup or "")
                require(match is not None, "Invalid matchup")
                team, venue, opponent = match.groups()
                require(
                    team in TEAM_NAMES and opponent in TEAM_NAMES and team != opponent,
                    "Unknown or identical matchup teams",
                )
                outcomes.add(wl)
                if venue == "vs.":
                    require(home is None, "Multiple home teams")
                    home = (team, opponent)
                else:
                    require(away is None, "Multiple away teams")
                    away = (team, opponent)
            require(outcomes == {"W", "L"}, "Team results are not opposing W/L")
            require(
                home is not None and away == (home[1], home[0]),
                "Non-reciprocal home/away records",
            )
            games.append(
                {
                    "game_id": gid,
                    "date": str(day),
                    "season": season,
                    "home": home[0],
                    "away": away[0],
                    "home_name": TEAM_NAMES[home[0]],
                    "away_name": TEAM_NAMES[away[0]],
                }
            )
        except (ValueError, TypeError, KeyError) as exc:
            errors.append({"game_id": gid, "error": str(exc), "records": records})
    require(len(games) <= 1500, "Too many completed regular-season games")
    return sorted(games, key=lambda g: (g["date"], g["game_id"])), errors


def capture(
    store,
    season,
    as_of,
    directory,
    *,
    bronze_dataset,
    metadata_dataset,
    mode="daily",
    start_date=None,
    end_date=None,
):
    directory.mkdir(parents=True, exist_ok=False)
    start, end = discovery_window(season, as_of, mode, start_date, end_date)
    raw = table_ref(store.project, bronze_dataset, "raw_game_logs")
    metadata = table_ref(store.project, metadata_dataset, "pipeline_run_log")
    provenance = {
        "source_kind": "nba_api_warehouse",
        "source_table": raw,
        "metadata_table": metadata,
        "season": season,
        "phase": "Regular Season",
        "mode": mode,
        "scope_start": str(start),
        "scope_end": str(end),
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "as_of": as_of.isoformat(),
        "coverage_scope": "completed_games_observed_in_upstream_ingestion; not independent league-wide completeness",
        "freshness": "not_verified"
        if mode == "daily"
        else "not_required_for_explicit_bounded_backfill",
    }
    write_json(directory / "request.json", provenance)
    first_job = len(store.jobs)
    try:
        if mode == "daily":
            runs = store.query(
                f"""SELECT dag_run_id, status, finished_at_utc, watermark_after, details
FROM `{metadata}` WHERE source_system='nba_api' AND season=@season
ORDER BY finished_at_utc DESC, dag_run_id DESC LIMIT 1""",
                [store.parameter("season", season)],
            )
            write_json(
                directory / "upstream_run.json",
                json.loads(json.dumps(runs, default=str)),
            )
            provenance["freshness"] = check_freshness(runs, as_of)
        rows = store.query(
            f"""SELECT game_id, game_date, matchup, wl, COUNT(*) AS player_rows,
MIN(ingested_at_utc) AS first_ingested_at, MAX(ingested_at_utc) AS last_ingested_at
FROM `{raw}` WHERE season=@season AND season_type='Regular Season'
AND game_date BETWEEN @start AND @end
GROUP BY game_id, game_date, matchup, wl
ORDER BY game_date, game_id, matchup, wl LIMIT {MAX_GROUPS + 1}""",
            [
                store.parameter("season", season),
                store.parameter("start", str(start), "DATE"),
                store.parameter("end", str(end), "DATE"),
            ],
        )
        write_json(
            directory / "game_contexts.json", json.loads(json.dumps(rows, default=str))
        )
        games, errors = completed_games(rows, season, start, end)
        write_json(
            directory / "rejected_contexts.json",
            json.loads(json.dumps(errors, default=str)),
        )
        require(
            not errors,
            "Conflicting or incomplete upstream game contexts; see rejected_contexts.json",
        )
        write_json(directory / "manifest.json", games)
        provenance.update(
            status="available", discovered_games=len(games), context_groups=len(rows)
        )
        return games
    except Exception as exc:
        provenance.update(
            status="failed", error_type=type(exc).__name__, error=str(exc)
        )
        raise
    finally:
        provenance["query_job_ids"] = [j["job_id"] for j in store.jobs[first_job:]]
        write_json(directory / "selection.json", provenance)
