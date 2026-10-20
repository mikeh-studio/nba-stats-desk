#!/usr/bin/env python3
"""Capture and evaluate historical injury ordering with an independent SQL oracle."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent.semantic_source import snapshot_digest  # noqa: E402
from app.agent.semantics import pregame_evidence  # noqa: E402
from app.seasons import ARCHIVE_SEASONS  # noqa: E402


def capture(client: Any, project: str) -> dict[str, Any]:
    from google.cloud import bigquery

    if not re.fullmatch(r"[a-z][a-z0-9-]{4,62}", project):
        raise ValueError("Invalid project")
    now = datetime.now(timezone.utc).isoformat()
    sources = [
        f"{project}.nba_silver_{suffix}.stg_player_injury_reports_clean"
        for suffix in (season.replace("-", "_") for season in ARCHIVE_SEASONS)
    ]
    columns = "season, player_id, player_name_source, team_abbr, game_date, game_time_et, matchup, report_timestamp_utc, injury_status, reason, source_url"
    sql = (
        "WITH reports AS ("
        + " UNION ALL ".join(
            f"SELECT {columns} FROM `{source}` FOR SYSTEM_TIME AS OF @snapshot_at"
            for source in sources
        )
        + ") SELECT *, COUNT(*) OVER() AS total FROM reports LIMIT 50001"
    )
    config = bigquery.QueryJobConfig(
        maximum_bytes_billed=100_000_000,
        query_parameters=[
            bigquery.ScalarQueryParameter("snapshot_at", "TIMESTAMP", now)
        ],
        use_query_cache=False,
    )
    job = client.query(sql, job_config=config, timeout=30)
    rows = [dict(r) for r in job.result(timeout=120)]
    if not rows or len(rows) > 50_000 or any(r.pop("total") != len(rows) for r in rows):
        raise ValueError("Empty or truncated injury source")
    for row in rows:
        row["game_date"] = str(row["game_date"])
        row["report_timestamp_utc"] = str(row["report_timestamp_utc"])
    snapshot = {
        "sources": sources,
        "source_timestamp": now,
        "rows": rows,
        "query_id": job.job_id,
        "bytes_processed": job.total_bytes_processed,
        "bytes_billed": job.total_bytes_billed,
    }
    snapshot["sha256"] = snapshot_digest(snapshot)
    return snapshot


def evaluate(snapshot: dict[str, Any]) -> dict[str, Any]:
    if snapshot["sha256"] != snapshot_digest(snapshot):
        raise ValueError("Snapshot checksum mismatch")
    rows = snapshot["rows"]
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE reports (season, player_id, game_date, matchup, published_at, status, url)"
    )
    conn.executemany(
        "INSERT INTO reports VALUES (?,?,?,?,?,?,?)",
        [
            (
                r["season"],
                r["player_id"],
                r["game_date"],
                r["matchup"],
                r["report_timestamp_utc"],
                r["injury_status"],
                r["source_url"],
            )
            for r in rows
        ],
    )
    sql = "SELECT status, url, published_at FROM reports WHERE season=? AND player_id=? AND game_date=? AND matchup=? AND julianday(published_at)<julianday(?) ORDER BY julianday(published_at) DESC LIMIT 1"
    cases = []
    for season in ("2023-24", "2024-25"):
        candidates = [
            r
            for r in rows
            if r["season"] == season
            and r["player_id"] is not None
            and r["game_time_et"]
            and r["source_url"]
        ]
        selected = None
        for row in sorted(candidates, key=lambda r: (r["game_date"], r["player_id"])):
            # NBA injury report schedule times are ET. Missing or ambiguous times
            # stay unverified; do not invent a UTC offset or use report date alone.
            text = re.sub(
                r"\s*\(?ET\)?\s*$", "", str(row["game_time_et"]), flags=re.I
            ).strip()
            try:
                clock = datetime.strptime(text, "%I:%M%p").time()
                cutoff = datetime.combine(
                    datetime.fromisoformat(row["game_date"]).date(),
                    clock,
                    ZoneInfo("America/New_York"),
                ).isoformat()
            except ValueError:
                continue
            params = (
                season,
                row["player_id"],
                row["game_date"],
                row["matchup"],
                cutoff,
            )
            expected = conn.execute(sql, params).fetchone()
            if expected:
                selected = (row, cutoff, dict(expected), params)
                break
        if selected is None:
            if not candidates:
                cases.append(
                    {"id": season, "passed": False, "reason": "no_historical_reports"}
                )
                continue
            row = sorted(candidates, key=lambda r: (r["game_date"], r["player_id"]))[0]
            actual = pregame_evidence(
                rows,
                season=season,
                player_id=row["player_id"],
                game_date=row["game_date"],
                matchup=row["matchup"],
                tipoff=None,
            )
            cases.append(
                {
                    "id": season + "/ambiguous-schedule-time",
                    "passed": actual["status"] == "unverified",
                    "source_row": row,
                    "expected": {
                        "status": "unverified",
                        "reason": "tipoff_unavailable",
                    },
                    "actual": actual,
                    "reason": "source_time_has_no_AM_PM; do not infer tipoff",
                }
            )
            continue
        row, cutoff, expected, params = selected
        args = {
            "season": season,
            "player_id": row["player_id"],
            "game_date": row["game_date"],
            "matchup": row["matchup"],
            "tipoff": cutoff,
        }
        actual = pregame_evidence(rows, **args)
        passed = (
            actual["status"] == "ok"
            and actual["report"]["injury_status"] == expected["status"]
            and actual["report"]["source_url"] == expected["url"]
            and actual["report"]["report_timestamp_utc"] == expected["published_at"]
        )
        cases.append(
            {
                "id": season,
                "passed": passed,
                "reference_sql": sql,
                "parameters": params,
                "expected": expected,
                "actual": actual,
                "cutoff_basis": "scheduled_time_on_official_injury_report_not_verified_actual_tipoff",
            }
        )
        unknown = pregame_evidence(rows, **(args | {"tipoff": None}))
        cases.append(
            {
                "id": season + "/unverified-tipoff",
                "passed": unknown["status"] == "unverified",
                "actual": unknown,
            }
        )
    conn.close()
    return {
        "evidence_kind": "historical_injury",
        "snapshot_sha256": snapshot["sha256"],
        "passed": sum(c["passed"] for c in cases),
        "total": len(cases),
        "human_reviewed": False,
        "release_ready": False,
        "rows": len(rows),
        "verified_actual_tipoff_cases": 0,
        "query_id": snapshot["query_id"],
        "limitations": [
            "Explicit schedule times can be tested as cutoffs; missing AM/PM is unverified and actual tipoff is not established.",
            "Sampled daily archives cannot establish every intraday change.",
        ],
        "cases": cases,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project")
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.project:
        from google.cloud import bigquery

        if args.snapshot.exists():
            parser.error("Snapshot already exists")
        snapshot = capture(bigquery.Client(project=args.project), args.project)
        args.snapshot.parent.mkdir(parents=True, exist_ok=True)
        with args.snapshot.open("x") as stream:
            stream.write(json.dumps(snapshot, indent=2) + "\n")
    else:
        snapshot = json.loads(args.snapshot.read_text())
    report = evaluate(snapshot)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "cases"}, indent=2))
    if report["passed"] != report["total"]:
        print(json.dumps(report["cases"], indent=2))
    return 0 if report["passed"] == report["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
