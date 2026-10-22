#!/usr/bin/env python3
"""Manual, local-only ESPN PBP experiment. No warehouse or scheduler writes."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import espn_sources as source  # noqa: E402
from scripts.fetch_gamebooks import write_json  # noqa: E402
from scripts.gamebook_sources import (  # noqa: E402
    SourceUnavailable,
    capture_json,
    season_bounds,
)
from scripts.gamebook_store import require  # noqa: E402


def revision(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def normalize(payload, game):
    # Deliberately independent of NBA identity/catalog and PDF availability.
    result = source.normalize_summary(payload, game, [])
    result["schema_version"] = "espn-pbp-experiment/1"
    result["nba_game_id"] = None
    result["validation"] = {
        "espn_internal": "reconciled",
        "pdf": "not_attempted",
        "cross_provider": "not_validated",
        "nba_identity": "not_attempted",
    }
    # Keep the full captured response separately; expose stable PBP fields here.
    result["plays"] = [
        {
            "source_event_id": p["id"],
            "period": p["period"]["number"],
            "clock_seconds": source.clock(
                p["clock"]["displayValue"], p["period"]["number"]
            ),
            "text": p["text"],
            "type": p.get("type"),
            "source_team_id": p.get("team", {}).get("id"),
            "participant_source_ids": sorted(
                {x["athlete"]["id"] for x in p.get("participants", [])}
            ),
            "scoring_play": p["scoringPlay"],
            "score_value": p.get("scoreValue"),
            "home_score": p.get("homeScore"),
            "away_score": p.get("awayScore"),
        }
        for p in payload["plays"]
    ]
    return result


def pairs(candidate):
    names = {p["source_player_id"]: p["name"] for p in candidate["players"]}
    grouped = {}
    for edge in candidate["assists"]:
        pair = (edge["team"], edge["assister_source_id"], edge["scorer_source_id"])
        row = grouped.setdefault(
            pair,
            dict(
                team=pair[0],
                assister_source_id=pair[1],
                assister=names[pair[1]],
                scorer_source_id=pair[2],
                scorer=names[pair[2]],
                assists=0,
                assisted_points=0,
                source_event_ids=[],
            ),
        )
        row["assists"] += 1
        row["assisted_points"] += edge["points"]
        row["source_event_ids"].append(edge["source_event_id"])
    return sorted(
        grouped.values(),
        key=lambda r: (
            -r["assists"],
            r["team"],
            r["assister_source_id"],
            r["scorer_source_id"],
        ),
    )


def run(season, start, end, output, session):
    first, last = season_bounds(season)
    require(first <= start <= end <= last, "Dates outside season")
    require((end - start).days < 7, "At most seven league-local dates")
    require(
        output.resolve().is_relative_to((ROOT / "reports").resolve())
        and not output.exists(),
        "Use a new directory inside reports/",
    )
    output.mkdir(parents=True)
    report = {"mode": "manual_local_only", "season": season, "days": [], "games": []}
    seen = set()
    for offset in range((end - start).days + 1):
        day = start + timedelta(days=offset)
        day_result = {"date": str(day), "status": "failed"}
        report["days"].append(day_result)
        try:
            payload = capture_json(
                source.BASE + "/scoreboard?dates=" + day.strftime("%Y%m%d"),
                output / "sources" / str(day),
                session,
            )
            games = source.scoreboard(payload, season, day)
            require(
                not (seen & {g["source_game_id"] for g in games}),
                "Duplicate game across dates",
            )
            seen.update(g["source_game_id"] for g in games)
            day_result.update(
                status="captured",
                observed=len(games),
                final=sum(g["final"] for g in games),
            )
        except (SourceUnavailable, ValueError, KeyError, TypeError) as exc:
            day_result["error"] = str(exc)
            continue
        for game in games:
            if not game["final"]:
                continue
            gid = game["source_game_id"]
            directory = output / "games" / gid
            row = {"game": game, "status": "failed"}
            report["games"].append(row)
            try:
                payload = capture_json(
                    source.BASE + "/summary?event=" + gid, directory / "source", session
                )
                candidate = normalize(payload, game)
                write_json(directory / "candidate.json", candidate)
                write_json(directory / "pairs.json", pairs(candidate))
                totals = Counter(e["team"] for e in candidate["assists"])
                row.update(
                    status="espn_reconciled_only",
                    revision=revision(candidate),
                    plays=candidate["play_count"],
                    assists=len(candidate["assists"]),
                    team_assists=dict(totals),
                    active_players=sum(p["played"] for p in candidate["players"]),
                    source_url=source.BASE + "/summary?event=" + gid,
                    play_by_play_url="https://www.espn.com/nba/playbyplay/_/gameId/"
                    + gid,
                )
            except (SourceUnavailable, ValueError, KeyError, TypeError) as exc:
                row["error"] = str(exc)
            write_json(directory / "result.json", row)
            print(gid, row["status"], flush=True)
    report["failed"] = sum(
        r["status"] == "failed" for r in report["days"] + report["games"]
    )
    report["observed_final_games"] = len(report["games"])
    report["reconciled_games"] = sum(
        r["status"] == "espn_reconciled_only" for r in report["games"]
    )
    write_json(output / "result.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--season", required=True)
    parser.add_argument("--start-date", type=date.fromisoformat, required=True)
    parser.add_argument("--end-date", type=date.fromisoformat, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with requests.Session() as session:
        result = run(args.season, args.start_date, args.end_date, args.output, session)
    return 2 if result["failed"] or not result["observed_final_games"] else 0


if __name__ == "__main__":
    sys.exit(main())
