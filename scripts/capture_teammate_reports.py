#!/usr/bin/env python3
"""Capture bounded NBA report samples for one study; cache raw bytes and provenance."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "dags")]
import requests  # noqa: E402
from app.agent.teammate_readiness import schedule_games, utc  # noqa: E402
from scripts.historical_sources import report_time_from_header  # noqa: E402

import nba_pipeline as pipeline  # noqa: E402


def report_sample(start):
    local = utc(start).astimezone(ZoneInfo("America/New_York"))
    # Legacy filenames can have a header thirty minutes later than the name.
    if (local.hour, local.minute) > (17, 30):
        return local.date().isoformat(), ("05PM", "05_00PM")
    if (local.hour, local.minute) > (14, 30):
        return local.date().isoformat(), ("02_30PM", "02PM", "02_00PM")
    if (local.hour, local.minute) > (13, 30):
        return local.date().isoformat(), ("01PM", "01_00PM")
    return (local - timedelta(days=1)).date().isoformat(), ("05PM", "05_00PM")


def capture_player_lookup(stats):
    # Season appearances cannot identify rostered players who never appeared.
    reference = pipeline.players.get_players()
    aliases = [
        {**r, "full_name": r["full_name"].replace("Đ", "Dj").replace("đ", "dj")}
        for r in reference
    ]
    lookup = pipeline.build_player_id_lookup(reference + aliases)
    lookup.update(
        pipeline.build_player_id_lookup(
            [{"id": r["player_id"], "full_name": r["player_name"]} for r in stats]
        )
    )
    return lookup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("spec", "schedule", "snapshot", "cache-dir", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--capture", action="store_true")
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("Output already exists")
    spec = json.loads(args.spec.read_text())
    schedule = json.loads(args.schedule.read_text())
    stats = json.loads(args.snapshot.read_text())
    games = [
        g
        for g in schedule_games(schedule, spec["season"], spec["team_abbr"])
        if spec["start"] <= g["game_date"] <= spec["end"]
    ]
    if not games or len(games) > 82:
        raise ValueError("Capture requires 1 to 82 scheduled games")
    lookup = capture_player_lookup(stats["rows"])
    args.cache_dir.mkdir(parents=True, exist_ok=True)
    rows, checks = [], []
    for game in games:
        if not game["scheduled_start_utc"] or game["postponed"]:
            checks.append(
                {"game_id": game["game_id"], "error": "No usable scheduled start"}
            )
            continue
        day, suffixes = report_sample(game["scheduled_start_utc"])
        for suffix in suffixes:
            filename = f"Injury-Report_{day}_{suffix}.pdf"
            url = f"{pipeline.OFFICIAL_INJURY_REPORT_BASE_URL}/{filename}"
            pdf = args.cache_dir / filename
            metadata = pdf.with_suffix(".json")
            check = {"game_id": game["game_id"], "url": url}
            checks.append(check)
            if not pdf.exists():
                if not args.capture:
                    check["error"] = "Not cached; --capture required"
                    continue
                try:
                    response = requests.get(
                        url, headers=pipeline.OFFICIAL_INJURY_REPORT_HEADERS, timeout=20
                    )
                    check["http_status"] = response.status_code
                    if response.status_code in (403, 404):
                        continue
                    response.raise_for_status()
                    content = response.content
                    # Validate before saving so error pages cannot poison the cache.
                    pipeline.extract_text_from_injury_report_pdf(content)
                    pdf.write_bytes(content)
                    metadata.write_text(
                        json.dumps(
                            {
                                "source_url": url,
                                "captured_at": datetime.now(timezone.utc).isoformat(),
                                "sha256": hashlib.sha256(content).hexdigest(),
                            },
                            indent=2,
                        )
                    )
                except (requests.RequestException, ValueError) as exc:
                    check["error"] = type(exc).__name__
                    continue
            evidence = json.loads(metadata.read_text())
            if (
                evidence["source_url"] != url
                or evidence["sha256"] != hashlib.sha256(pdf.read_bytes()).hexdigest()
            ):
                raise ValueError("Raw report provenance mismatch")
            text = pipeline.extract_text_from_injury_report_pdf(pdf.read_bytes())
            report_time = report_time_from_header(text, day)
            frame = pipeline.parse_injury_report_text(
                text,
                report_date=day,
                report_time_et=report_time,
                source_url=url,
                season=spec["season"],
                player_lookup=lookup,
                ingested_at_utc=evidence["captured_at"],
            )
            frame.columns = [c.lower() for c in frame.columns]
            parsed = json.loads(frame.to_json(orient="records", date_format="iso"))
            for row in parsed:
                for field in ("game_date", "report_date"):
                    row[field] = row[field][:10]
            rows.extend(parsed)
            check.update(evidence, parsed_rows=len(parsed), report_time_et=report_time)
            break
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as handle:
        json.dump(
            {
                "rows": rows,
                "source_checks": checks,
                "player_identity_lookup": lookup,
                "limitations": [
                    "One sampled report per game, not exhaustive intraday coverage. Raw PDF header supplies report time."
                ],
                "model_tokens": 0,
            },
            handle,
            indent=2,
        )
    print(
        json.dumps(
            {
                "rows": len(rows),
                "reports": sum("parsed_rows" in c for c in checks),
                "checks": len(checks),
                "output": str(args.output),
            }
        )
    )


if __name__ == "__main__":
    main()
