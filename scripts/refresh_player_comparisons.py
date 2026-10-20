#!/usr/bin/env python3
"""Refresh every 2025-26 player's comparison inputs from official league logs.

Fresh source reads and validation happen before candidate creation. --publish
authorizes the existing isolated rebuild and guarded atomic promotion. Reports,
raw sources and rollback references remain in ignored local operational storage.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "dags"))

from app.agent.semantic_source import BigQuerySemanticSource  # noqa: E402
from app.agent.semantics import COMPONENTS  # noqa: E402
from app.config import get_settings  # noqa: E402
from scripts.promote_game_log_repair import promote  # noqa: E402
from scripts.repair_context_snapshot import (  # noqa: E402
    capture_sources,
    repair,
    statistics_equal,
)
from scripts.repair_game_schedule import retain_warehouse_schedule  # noqa: E402
from scripts.stage_game_log_repair import raw_repaired_rows, write_new  # noqa: E402

SEASON = "2025-26"


def validate_comparison_contract(snapshot, run):
    import pandas as pd

    import nba_pipeline
    import nba_source_contracts

    rows = raw_repaired_rows(
        snapshot,
        [
            SimpleNamespace(name=field.name.lower())
            for field in nba_pipeline.get_game_logs_schema()
        ],
    )
    frame = pd.DataFrame(rows)
    frame.columns = frame.columns.str.upper()
    try:
        result = nba_source_contracts.validate_source_contract(
            "game_logs", frame, season=SEASON
        )
    except nba_source_contracts.SourceContractError as exc:
        write_new(run / "source-contract.json", exc.result)
        exc.quarantine_frame.to_csv(run / "quarantine.csv", index=False)
        raise
    write_new(run / "source-contract.json", result.result)
    if not result.quarantine_frame.empty:
        result.quarantine_frame.to_csv(run / "quarantine.csv", index=False)
        raise ValueError("Quarantined rows block the completed-season refresh")
    incomplete = frame.loc[frame[[c.upper() for c in COMPONENTS]].isna().any(axis=1)]
    if not incomplete.empty:
        incomplete.to_csv(run / "rejected-comparison-inputs.csv", index=False)
        raise ValueError("Incomplete inputs block the completed-season refresh")


def refresh(args, client, project, run):
    from google.cloud import bigquery

    sources = run / "sources"
    capture_sources(sources, SEASON, timeout=args.source_timeout)
    before = BigQuerySemanticSource(client, project=project).capture([SEASON])
    write_new(run / "before.json", before)
    candidate, audit = repair(sources, run / "before.json", SEASON)
    write_new(run / "source-validation.json", audit)
    schedule = [
        dict(row)
        for row in client.query(
            f"SELECT * FROM `{project}.nba_bronze.raw_schedule` WHERE season=@season",
            job_config=bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("season", "STRING", SEASON)
                ],
                maximum_bytes_billed=100_000_000,
            ),
        ).result()
    ]
    candidate, venue_audit = retain_warehouse_schedule(candidate, schedule)
    write_new(run / "venue-validation.json", venue_audit)
    write_new(run / "candidate.json", candidate)
    validate_comparison_contract(candidate, run)
    summary = {
        "season": SEASON,
        "player_rows": len(candidate["rows"]),
        "players": len({r["player_id"] for r in candidate["rows"]}),
        "phases": audit["phases"],
        "model_tokens": 0,
    }
    if statistics_equal(before, candidate):
        return dict(summary, status="unchanged", warehouse_mutations=0)
    if not args.publish:
        return dict(summary, status="validated_changes", warehouse_mutations=0)
    command = [
        sys.executable,
        str(ROOT / "scripts/stage_game_log_repair.py"),
        "--project",
        project,
        "--source-dir",
        str(sources),
        "--original",
        str(run / "before.json"),
        "--retain-schedule",
        "--require-original-baseline",
        "--run-dir",
        str(run / "stage"),
        "--dbt",
        str(args.dbt),
    ]
    with (run / "stage.log").open("x") as log:
        subprocess.run(
            command,
            cwd=ROOT,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=True,
            timeout=3600,
        )
    plan = json.loads((run / "stage/validated.json").read_text())
    write_new(run / "publication-attempt.json", {"manifest_sha256": plan["sha256"]})
    receipt = promote(plan, plan["sha256"], client)
    write_new(run / "publication.json", receipt)
    return dict(
        summary, status="published", tables=receipt["tables"], job_id=receipt["job_id"]
    )


def execute(args):
    from google.cloud import bigquery

    settings = get_settings()
    if (
        settings.gold_dataset != "nba_gold"
        or settings.agent_dataset != "nba_agent"
        or any(
            os.getenv("BQ_DATASET_" + layer, "nba_" + layer.lower())
            != "nba_" + layer.lower()
            for layer in ("BRONZE", "SILVER", "GOLD", "AGENT")
        )
    ):
        raise ValueError(
            "This completed-season refresh requires the standard warehouse datasets"
        )
    args.directory.mkdir(parents=True, exist_ok=True)
    run = args.directory / (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex[:12]
    )
    run.mkdir()
    with (args.directory / "refresh.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            write_new(run / "result.json", {"status": "overlap_blocked"})
            return 1
        try:
            result = refresh(
                args,
                bigquery.Client(project=settings.project_id),
                settings.project_id,
                run,
            )
        except Exception as exc:
            # Raw logs are private. The public-facing receipt never contains
            # arbitrary provider/cloud exception text or any configured secrets.
            attempted = (run / "publication-attempt.json").exists()
            result = {
                "status": "failed",
                "failure_type": type(exc).__name__,
                "serving_data_retained": not attempted,
                "publication_state": "unconfirmed" if attempted else "not_attempted",
                "stage_present": (run / "stage").exists(),
            }
            # A committed receipt, if present, distinguishes publication from
            # a later local report-write failure.
            if (run / "publication.json").exists():
                result["serving_data_retained"] = False
                result["publication_state"] = "committed"
            write_new(run / "result.json", result)
            print(json.dumps(dict(result, run_dir=str(run))))
            return 1
        write_new(run / "result.json", result)
        print(json.dumps(dict(result, run_dir=str(run))))
        return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--directory", type=Path, default=ROOT / "reports/daily-player-comparisons"
    )
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--source-timeout", type=float, default=15)
    parser.add_argument("--dbt", type=Path, default=ROOT / ".venv-airflow/bin/dbt")
    args = parser.parse_args()
    if not 1 <= args.source_timeout <= 60:
        parser.error("source timeout must be between 1 and 60 seconds")
    return execute(args)


if __name__ == "__main__":
    raise SystemExit(main())
