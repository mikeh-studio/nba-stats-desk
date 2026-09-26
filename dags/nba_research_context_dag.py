"""Opt-in context capture, independent of successful player-game ingestion."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

from airflow.decorators import dag, task

ROOT = Path(__file__).resolve().parents[1]


@dag(
    dag_id="nba_research_context",
    schedule="*/30 * * * *",
    start_date=datetime(2026, 9, 25),
    catchup=False,
    max_active_runs=1,
    is_paused_upon_creation=True,
    default_args={"retries": 1, "retry_delay": timedelta(minutes=5)},
    tags=["nba", "research"],
)
def research_context_dag():
    @task(execution_timeout=timedelta(minutes=10))
    def capture():
        output = os.environ.get("RESEARCH_CAPTURE_DIRECTORY")
        memberships = os.environ.get("RESEARCH_MEMBERSHIPS_PATH")
        if not output or not memberships:
            raise ValueError(
                "Configure durable private capture storage and reviewed memberships"
            )
        subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/capture_research_context.py"),
                "--output",
                output,
                "--memberships",
                memberships,
                "--season",
                os.environ.get("RESEARCH_CAPTURE_SEASON", "2026-27"),
            ],
            cwd=ROOT,
            timeout=540,
            check=True,
        )

    capture()


nba_research_context = research_context_dag()
