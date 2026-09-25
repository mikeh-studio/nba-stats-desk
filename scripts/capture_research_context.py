#!/usr/bin/env python3
"""Capture schedule and injury input versions independently of game-log refreshes."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "dags")]

from app.research_snapshots import (  # noqa: E402
    append_snapshot,
    build_context_snapshot,
    digest,
    read_snapshot,
)
from app.research_studies import PAIRS  # noqa: E402


def capture(output: Path, memberships_path: Path, season="2026-27"):
    import pandas as pd

    from nba_pipeline import (
        build_injury_report_candidates,
        get_all_official_injury_reports,
        get_upcoming_schedule,
    )
    from nba_source_contracts import SourceContractError, validate_source_contract

    memberships = json.loads(memberships_path.read_text())
    statuses, games, reports = {}, [], []
    local_now = datetime.now(ZoneInfo("America/New_York"))
    latest = local_now.replace(
        minute=(local_now.minute // 30) * 30, second=0, microsecond=0
    )
    candidates = []
    for stamp in (latest, latest - timedelta(minutes=30)):
        candidates.extend(
            build_injury_report_candidates(
                start_date=stamp.date(),
                end_date=stamp.date(),
                report_times_et=[stamp.strftime("%I:%M%p")],
                max_reports=1,
            )
        )
    # Parsed source extracts and validated rows are separate evidence; failed extracts
    # are recorded rather than replaced by an apparent successful empty result.
    inputs = {}
    for domain in ("schedule", "injury_reports"):
        try:
            frame = (
                get_upcoming_schedule(
                    season=season, horizon_days=7, retries=2, timeout=15
                )
                if domain == "schedule"
                else get_all_official_injury_reports(
                    candidates,
                    season=season,
                    retries=2,
                    timeout=15,
                )
            )
            inputs[domain] = json.loads(
                frame.to_json(orient="records", date_format="iso")
            )
            if frame.empty:
                statuses[domain] = {
                    "status": "unavailable",
                    "reason": "empty_source_response",
                }
                continue
            validated = validate_source_contract(domain, frame, season=season)
            rows = json.loads(
                validated.frame.to_json(orient="records", date_format="iso")
            )
            statuses[domain] = {
                "status": "complete",
                "contract": validated.result,
                "quarantine": json.loads(
                    validated.quarantine_frame.to_json(
                        orient="records", date_format="iso"
                    )
                ),
            }
            if domain == "schedule":
                for r in rows:
                    games.append(
                        {
                            "season": r["SEASON"],
                            "game_id": r["GAME_ID"],
                            "game_date": r["SCHEDULE_DATE"][:10],
                            "team_abbr": r["TEAM_ABBR"],
                            "opponent_abbr": r["OPPONENT_ABBR"],
                            "home_away": r["HOME_AWAY"].lower(),
                            "scheduled_start_utc": r.get("GAME_TIME_UTC"),
                            "ingested_at_utc": r["INGESTED_AT_UTC"],
                            "source_updated_at_utc": r["SOURCE_UPDATED_AT_UTC"],
                            "source_time_semantics": "observed_capture_time",
                            "final": str(r["GAME_STATUS"]).lower().startswith("final"),
                            "postponed": str(r["GAME_STATUS"]).lower()
                            in ("postponed", "ppd"),
                        }
                    )
            else:
                observed_urls = {r.get("SOURCE_URL") for r in rows}
                statuses[domain]["attempts"] = [
                    {
                        "source_url": c["source_url"],
                        "status": "parsed"
                        if c["source_url"] in observed_urls
                        else "unavailable",
                    }
                    for c in candidates
                ]
                if any(c["source_url"] not in observed_urls for c in candidates):
                    statuses[domain]["status"] = "partial_failure"
                for r in rows:
                    item = {k.lower(): v for k, v in r.items()}
                    item["game_date"] = item["game_date"][:10]
                    if pd.notna(item.get("player_id")):
                        item["player_id"] = int(item["player_id"])
                    reports.append(item)
        except SourceContractError as exc:
            statuses[domain] = {
                "status": "failed",
                "error_type": type(exc).__name__,
                "contract": exc.result,
                "quarantine": json.loads(
                    exc.quarantine_frame.to_json(orient="records", date_format="iso")
                ),
            }
        except Exception as exc:
            statuses[domain] = {"status": "failed", "error_type": type(exc).__name__}
    # Retain earlier valid versions on optional-source failure without pretending
    # they were refreshed. The original acquisition times still govern cutoffs.
    for path in sorted((output / season).glob("*.json"), reverse=True)[:96]:
        if games and reports:
            break
        prior = read_snapshot(path)
        for domain, key in (("schedule", "games"), ("injury_reports", "reports")):
            values = games if key == "games" else reports
            if (
                not values
                and prior.get("source_status", {}).get(domain, {}).get("status")
                == "complete"
            ):
                values.extend(prior.get("inputs", {}).get(key, []))
                if values:
                    statuses[domain]["retained_snapshot_hash"] = prior["sha256"]
                    statuses[domain]["stale"] = True
    stamp = datetime.now(timezone.utc).isoformat()
    context = build_context_snapshot(
        games=games,
        reports=reports,
        memberships=memberships,
        pairs=[
            {"player_id": p["player_id"], "teammate_id": p["teammate_id"]}
            for p in PAIRS
        ],
        as_of_ts=stamp,
        captured_at=stamp,
        kind="captured",
        source_status=statuses,
    )
    # Retain the exact parsed versions used for replay; no public responses expose
    # operational paths or raw diagnostics.
    context["inputs"] = {
        "raw_extracts": inputs,
        "games": games,
        "reports": reports,
        "memberships": memberships,
    }
    context["sha256"] = digest({k: v for k, v in context.items() if k != "sha256"})
    filename = stamp.replace(":", "-") + ".json"
    append_snapshot(output / season / filename, context)
    if any(s["status"] != "complete" for s in statuses.values()):
        raise RuntimeError(
            "Context capture retained evidence of an unavailable or failed source"
        )
    return filename


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--memberships", type=Path, required=True)
    parser.add_argument("--season", choices=("2025-26", "2026-27"), default="2026-27")
    args = parser.parse_args()
    print(capture(args.output, args.memberships, args.season))


if __name__ == "__main__":
    main()
