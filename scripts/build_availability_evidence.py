#!/usr/bin/env python3
"""Publish a bounded league-wide availability snapshot; no player-pair registry."""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.agent.semantic_source import load_snapshot  # noqa: E402
from app.agent.teammate_readiness import schedule_games  # noqa: E402
from app.availability import load_availability, prepare_availability  # noqa: E402
from app.research_snapshots import append_snapshot, digest  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("snapshot", "schedule", "injuries", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    stats, evidence = load_snapshot(args.snapshot)
    schedule = json.loads(args.schedule.read_text())
    injuries = json.loads(args.injuries.read_text())
    teams = sorted({(r["season"], r["team_abbr"]) for r in evidence.rows})
    games = [
        g
        for season, team in teams
        for g in schedule_games(schedule, season, team, "Both")
    ]
    stamp = datetime.now(timezone.utc).isoformat()
    doc = dict(
        version=1,
        artifact_type="availability_evidence/v1",
        kind="reconstructed",
        as_of_ts=stamp,
        captured_at=stamp,
        rows=[],
        stats=stats,
        games=games,
        reports=injuries["rows"],
        input_hashes=dict(
            stats=stats["sha256"], schedule=digest(schedule), injuries=digest(injuries)
        ),
    )
    doc["sha256"] = digest(doc)
    prepare_availability(doc)
    append_snapshot(args.output, doc)
    load_availability(str(args.output))
    print(
        f"Validated {len(evidence.rows)} appearances and {len(games)} team games; {len(doc['reports'])} reports"
    )


if __name__ == "__main__":
    main()
