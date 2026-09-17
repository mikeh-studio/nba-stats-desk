#!/usr/bin/env python3
"""Locally supplement selected teammate cases with archived prior-day reports.

One 5pm ET sample per needed date, with explicit legacy/current URL variants.
This is not full daily report coverage and does not establish final tipoff status.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "dags")]
import requests  # noqa: E402
from scripts.historical_sources import report_time_from_header  # noqa: E402

import nba_pipeline as pipeline  # noqa: E402


def needed_dates(rows, cases):
    return sorted(
        {
            (date.fromisoformat(row["game_date"]) - timedelta(days=1)).isoformat()
            for case in cases
            if case.get("teammate_id")
            for row in rows
            if row["player_id"] == case["player_id"]
            and row["season"] in case["seasons"]
            and row["season_type"] == case["phase"]
            and (
                case["start"] <= row["game_date"] <= case["end"]
                or case["previous_start"] <= row["game_date"] <= case["previous_end"]
            )
        }
    )


def capture(stats_path, cases_path, original_path, output_dir):
    stats = json.loads(stats_path.read_text())
    cases = json.loads(cases_path.read_text())
    original = json.loads(original_path.read_text())
    output_dir.mkdir(parents=True, exist_ok=False)
    lookup = pipeline.build_player_id_lookup()
    # Supplement the static reference using verified season appearance names.
    lookup.update(
        pipeline.build_player_id_lookup(
            [
                {"id": r["player_id"], "full_name": r["player_name"]}
                for r in stats["rows"]
            ]
        )
    )
    captured_at = datetime.now(timezone.utc).isoformat()

    def fetch(day):
        checks = []
        for stamp in ["05PM", "05_00PM"]:
            url = f"{pipeline.OFFICIAL_INJURY_REPORT_BASE_URL}/Injury-Report_{day}_{stamp}.pdf"
            try:
                response = requests.get(
                    url, headers=pipeline.OFFICIAL_INJURY_REPORT_HEADERS, timeout=20
                )
                check = {"date": day, "url": url, "http_status": response.status_code}
                checks.append(check)
                if response.status_code in (403, 404):
                    continue
                response.raise_for_status()
                path = output_dir / f"{day}.pdf"
                path.write_bytes(response.content)
                check["sha256"] = hashlib.sha256(response.content).hexdigest()
                text = pipeline.extract_text_from_injury_report_pdf(response.content)
                report_time = report_time_from_header(text, day)
                frame = pipeline.parse_injury_report_text(
                    text,
                    report_date=day,
                    report_time_et=report_time,
                    source_url=url,
                    player_lookup=lookup,
                    ingested_at_utc=captured_at,
                )
                frame.columns = [c.lower() for c in frame.columns]
                rows = json.loads(frame.to_json(orient="records", date_format="iso"))
                for row in rows:
                    for field in ["game_date", "report_date"]:
                        row[field] = row[field][:10]
                check.update(
                    parsed_rows=len(rows),
                    report_time_et=report_time,
                    unresolved_player_ids=sum(r["player_id"] is None for r in rows),
                )
                if not rows:
                    check["error"] = "No parsed rows; coverage unverified"
                return rows, checks
            except Exception as exc:
                checks.append({"date": day, "url": url, "error": type(exc).__name__})
                return [], checks
        return [], checks

    days = needed_dates(stats["rows"], cases)
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(fetch, days))
    new_rows = [row for rows, _ in results for row in rows]
    checks = [check for _, items in results for check in items]
    value = {
        "sources": ["NBA official injury PDFs", *original.get("sources", [])],
        "captured_at": captured_at,
        "original_sha256": hashlib.sha256(original_path.read_bytes()).hexdigest(),
        "stats_sha256": stats["sha256"],
        "requested_dates": days,
        "source_checks": checks,
        "rows": original["rows"] + new_rows,
        "limitations": [
            "Selected prior-day 5pm ET snapshots only. Missing reports are unknown, not healthy.",
            "Source report timestamp and retrospective ingestion timestamp retained separately.",
        ],
    }
    (output_dir / "injury-snapshot.json").write_text(json.dumps(value, indent=2) + "\n")
    print(
        json.dumps(
            {
                "requested_dates": len(days),
                "retrieved_dates": sum(bool(rows) for rows, _ in results),
                "added_rows": len(new_rows),
                "failures": [x for x in checks if x.get("error")],
            }
        )
    )


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--snapshot", type=Path, required=True)
    p.add_argument("--cases", type=Path, required=True)
    p.add_argument("--original", type=Path, required=True)
    p.add_argument("--output-dir", type=Path, required=True)
    a = p.parse_args()
    capture(a.snapshot, a.cases, a.original, a.output_dir)


if __name__ == "__main__":
    main()
