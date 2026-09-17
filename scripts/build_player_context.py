#!/usr/bin/env python3
"""Execute the actual context dbt SELECTs locally against frozen evidence.

SQLite validates relational behavior without publishing warehouse objects.
BigQuery compilation/dry runs remain a separate validation boundary.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, StrictUndefined

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.agent.semantic_source import load_snapshot  # noqa: E402

MODELS = (
    "team_game_context",
    "team_defense_before_game",
    "player_game_reported_status",
    "player_game_context",
)
INJURY_COLUMNS = (
    "season",
    "player_id",
    "team_abbr",
    "game_date",
    "matchup",
    "injury_status",
    "reason",
    "report_timestamp_utc",
    "ingested_at_utc",
    "source_url",
)


def render_model(name, *, ref=lambda name: f'"{name}"', day_start=None):
    env = Environment(undefined=StrictUndefined)
    return env.from_string((ROOT / f"dbt/models/gold/{name}.sql").read_text()).render(
        config=lambda **kwargs: "",
        env_var=lambda name, default: default,
        ref=ref,
        context_day_start=day_start or (lambda expr: f"({expr} || 'T00:00:00+00:00')"),
    )


def load_table(connection, name, rows, columns=None):
    columns = list(columns or rows[0])
    # Identifiers originate only from validated snapshots or the fixed schema.
    if any(not c.replace("_", "").isalnum() for c in columns):
        raise ValueError("Invalid source column")
    connection.execute(
        f'CREATE TABLE "{name}" (' + ", ".join(f'"{c}"' for c in columns) + ")"
    )
    connection.executemany(
        f'INSERT INTO "{name}" VALUES (' + ",".join("?" for _ in columns) + ")",
        [[row.get(c) for c in columns] for row in rows],
    )


def build_context(connection, rows, reports):
    connection.row_factory = sqlite3.Row
    connection.create_function(
        "concat", -1, lambda *args: "".join(str(x) for x in args)
    )
    load_table(connection, "fct_player_game_stats", rows)
    normalized = []
    for report in reports:
        row = dict(report)
        for field in ("report_timestamp_utc", "ingested_at_utc"):
            if row.get(field):
                stamp = datetime.fromisoformat(row[field].replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    raise ValueError("Report timestamps must include a timezone")
                row[field] = stamp.astimezone(timezone.utc).isoformat()
        normalized.append(row)
    load_table(
        connection, "stg_player_injury_reports_clean", normalized, INJURY_COLUMNS
    )
    steps = []
    for name in MODELS:
        started = time.perf_counter()
        sql = render_model(name)
        connection.execute(f'CREATE TABLE "{name}" AS {sql}')
        steps.append(
            {
                "model": name,
                "rows": connection.execute(f'SELECT count(*) FROM "{name}"').fetchone()[
                    0
                ],
                "seconds": round(time.perf_counter() - started, 3),
                "llm_tokens": 0,
                "sql_sha256": hashlib.sha256(sql.encode()).hexdigest(),
            }
        )
    audits = {
        "source_rows": len(rows),
        "context_rows": connection.execute(
            "select count(*) from player_game_context"
        ).fetchone()[0],
        "duplicate_keys": connection.execute(
            "select count(*) from (select season,game_id,player_id from player_game_context group by 1,2,3 having count(*)>1)"
        ).fetchone()[0],
        "future_opponent_rows": connection.execute(
            "select count(*) from player_game_context where opponent_latest_prior_game_date >= game_date"
        ).fetchone()[0],
        "late_status_rows": connection.execute(
            "select count(*) from player_game_context where status_reported_at >= game_date || 'T00:00:00+00:00'"
        ).fetchone()[0],
        "opponent_context_rows": connection.execute(
            "select count(*) from player_game_context where opponent_prior_win_pct is not null"
        ).fetchone()[0],
        "injury_source_rows": len(reports),
        "unresolved_injury_player_ids": sum(
            r.get("player_id") is None for r in reports
        ),
        "injury_month_counts": dict(
            connection.execute(
                "select substr(game_date,1,7), count(*) from stg_player_injury_reports_clean group by 1"
            ).fetchall()
        ),
        "steps": steps,
        "limits": [
            "Before-game-date UTC cutoff, not latest before tipoff.",
            "Corrected historical facts; ingestion timestamps can postdate games.",
            "Appearance rows do not constitute historical roster membership.",
            "Opponent win% and eFG% allowed are prior-only descriptive context, not defensive rating or causal adjustment.",
        ],
    }
    if audits["source_rows"] != audits["context_rows"] or any(
        audits[k]
        for k in ("duplicate_keys", "future_opponent_rows", "late_status_rows")
    ):
        raise ValueError(f"Context audit failed: {audits}")
    connection.commit()
    return audits


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--injury-snapshot", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    snapshot, _ = load_snapshot(args.snapshot)
    injuries = json.loads(args.injury_snapshot.read_text())
    args.output_dir.mkdir(parents=True, exist_ok=False)
    with sqlite3.connect(args.output_dir / "context.sqlite") as connection:
        audit = build_context(connection, snapshot["rows"], injuries["rows"])
    audit.update(
        snapshot_sha256=snapshot["sha256"],
        context_sha256=hashlib.sha256(
            (args.output_dir / "context.sqlite").read_bytes()
        ).hexdigest(),
        injury_snapshot_path=str(args.injury_snapshot.resolve()),
        injury_snapshot_sha256=hashlib.sha256(
            args.injury_snapshot.read_bytes()
        ).hexdigest(),
        injury_capture={k: v for k, v in injuries.items() if k != "rows"},
    )
    (args.output_dir / "context-audit.json").write_text(
        json.dumps(audit, indent=2) + "\n"
    )
    print(json.dumps(audit, indent=2))


if __name__ == "__main__":
    main()
