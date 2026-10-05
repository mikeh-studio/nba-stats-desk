#!/usr/bin/env python3
"""Reparse invalid report sources and capture final participation into a new bundle.

Public read-only requests; raw downloads and repair audit stay in a private run
folder. No warehouse writes. Publication is create-only and fails on any error.
"""

import argparse
import hashlib
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "dags")]
from app.availability import prepare_availability  # noqa: E402
from app.research_snapshots import append_snapshot, digest, read_snapshot  # noqa: E402


def participation_rows(payload, schedule, source_url):
    game = payload["game"]
    if game["gameId"] != schedule["game_id"] or game["gameStatus"] != 3:
        raise ValueError("Box score is not the requested final game")
    home, away = game["homeTeam"], game["awayTeam"]
    if {home["teamTricode"], away["teamTricode"]} != {
        schedule["team_abbr"],
        schedule["opponent_abbr"],
    }:
        raise ValueError("Box score teams disagree with schedule")
    if game["gameTimeUTC"].replace("Z", "+00:00") != schedule[
        "scheduled_start_utc"
    ].replace("Z", "+00:00"):
        raise ValueError("Box score date/time disagrees with schedule")
    result = []
    for team in (home, away):
        for player in team["players"]:
            raw = player.get("statistics", {}).get("minutes")
            match = re.fullmatch(r"PT(\d+)M(\d+(?:\.\d+)?)S", raw or "")
            if not match or player.get("played") not in ("0", "1"):
                raise ValueError("Unknown final player participation")
            minutes = int(match[1]) + float(match[2]) / 60
            if (minutes > 0) != (player["played"] == "1"):
                raise ValueError("Minutes and participation disagree")
            result.append(
                dict(
                    season=schedule["season"],
                    game_id=game["gameId"],
                    player_id=player["personId"],
                    team_abbr=team["teamTricode"],
                    minutes=minutes,
                    source_url=source_url,
                )
            )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--player-id", type=int, required=True)
    parser.add_argument("--teammate-id", type=int, required=True)
    parser.add_argument("--reports-only", action="store_true")
    parser.add_argument("--cached-reports", type=Path)
    args = parser.parse_args()
    args.run_dir.mkdir(parents=True, exist_ok=False)
    import requests

    import nba_pipeline as pipeline

    doc = read_snapshot(args.input)
    original_hash = doc["sha256"]
    invalid = [
        r for r in doc["reports"] if r["team_abbr"] not in r["matchup"].split("@")
    ]
    (args.run_dir / "invalid-original-rows.json").write_text(
        json.dumps(invalid, indent=2)
    )
    urls = sorted({r["source_url"] for r in invalid})
    if len(urls) > 200:
        raise ValueError("Repair exceeds 200-source bound")
    lookup = {
        pipeline.normalize_player_name_key(r["player_name"]): r["player_id"]
        for r in doc["stats"]["rows"]
    }
    errors = []

    def reparse(url):
        try:
            parsed = urlparse(url)
            if (
                parsed.scheme != "https"
                or parsed.hostname != "ak-static.cms.nba.com"
                or not parsed.path.startswith("/referee/injury/")
            ):
                raise ValueError("Unexpected report source")
            cached = (
                args.cached_reports / Path(parsed.path).name
                if args.cached_reports
                else None
            )
            if cached and cached.exists():
                content = cached.read_bytes()
            else:
                raw = requests.get(
                    url, timeout=25, headers=pipeline.OFFICIAL_INJURY_REPORT_HEADERS
                )
                raw.raise_for_status()
                content = raw.content
            (args.run_dir / Path(parsed.path).name).write_bytes(content)
            old = next(r for r in invalid if r["source_url"] == url)
            text = pipeline.extract_text_from_injury_report_pdf(content)
            header = re.search(
                r"Injury\s+Report:\s+(\d{2}/\d{2}/\d{2,4})\s+(\d{1,2}):(\d{2})\s*([AP]M)",
                text,
            )
            if not header:
                raise ValueError("Missing report timestamp")
            date = datetime.strptime(
                header[1], "%m/%d/%y" if len(header[1]) == 8 else "%m/%d/%Y"
            ).date()
            frame = pipeline.parse_injury_report_text(
                text,
                report_date=date,
                report_time_et=f"{header[2]}_{header[3]}{header[4]}",
                source_url=url,
                season=old["season"],
                player_lookup=lookup,
            )
            rows = json.loads(frame.to_json(orient="records", date_format="iso"))
            normalized = []
            for row in rows:
                row = {k.lower(): v for k, v in row.items()}
                if row.get("player_id") is None:
                    continue
                row["player_id"] = int(row["player_id"])
                row["game_date"] = row["game_date"][:10]
                normalized.append(row)
            if not normalized:
                raise ValueError("Empty report parse")
            return url, normalized, hashlib.sha256(content).hexdigest()
        except Exception as exc:
            return url, None, str(exc)

    replacements, hashes = {}, {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        for url, rows, detail in pool.map(reparse, urls):
            if rows is None:
                errors.append(dict(source=url, error=detail))
            else:
                replacements[url], hashes[url] = rows, detail
    (args.run_dir / "report-repair-audit.json").write_text(
        json.dumps(
            dict(
                source_snapshot=original_hash,
                invalid_rows=len(invalid),
                repaired_sources=len(replacements),
                errors=errors,
                raw_pdf_hashes=hashes,
            ),
            indent=2,
        )
    )
    if errors:
        raise ValueError(f"{len(errors)} report repairs failed; no bundle published")
    doc["reports"] = [r for r in doc["reports"] if r["source_url"] not in replacements]
    doc["reports"].extend(r for rows in replacements.values() for r in rows)
    index = {
        (r["season"], r["game_id"], r["player_id"]): r for r in doc["stats"]["rows"]
    }
    targets = [
        r
        for r in doc["stats"]["rows"]
        if r["player_id"] == args.player_id
        and (r["season"], r["game_id"], args.teammate_id) not in index
    ]
    participation = {
        (r["season"], r["game_id"], r["player_id"]): r
        for r in doc.get("participation", [])
    }
    if not args.reports_only and len(targets) > 200:
        raise ValueError("Participation capture exceeds 200-game bound")
    for target in [] if args.reports_only else targets:
        gid = target["game_id"]
        if not re.fullmatch(r"\d{10}", gid):
            raise ValueError("Invalid NBA game ID")
        url = f"https://cdn.nba.com/static/json/liveData/boxscore/boxscore_{gid}.json"
        try:
            response = requests.get(url, timeout=25, headers=pipeline.NBA_CDN_HEADERS)
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            (args.run_dir / "participation-failure.json").write_text(
                json.dumps(dict(source=url, error=str(exc), published=False), indent=2)
            )
            raise
        (args.run_dir / f"boxscore_{gid}.json").write_text(json.dumps(payload))
        schedule = next(
            g
            for g in doc["games"]
            if g["game_id"] == gid and g["team_abbr"] == target["team_abbr"]
        )
        for record in participation_rows(payload, schedule, url):
            participation[
                (record["season"], record["game_id"], record["player_id"])
            ] = record
    doc["participation"] = list(participation.values())
    doc["participation_capture"] = (
        "not_requested" if args.reports_only else "complete_for_requested_pair"
    )
    doc["captured_at"] = doc["as_of_ts"] = datetime.now(timezone.utc).isoformat()
    doc["input_hashes"] = dict(
        previous=original_hash,
        reports=digest(doc["reports"]),
        participation=digest(doc["participation"]),
    )
    doc.pop("sha256", None)
    doc["sha256"] = digest(doc)
    prepare_availability(doc)
    append_snapshot(args.run_dir / "availability.json", doc)
    print(
        json.dumps(
            dict(
                repaired_sources=len(replacements),
                participation_rows=len(participation),
                output=str(args.run_dir / "availability.json"),
            )
        )
    )


if __name__ == "__main__":
    main()
