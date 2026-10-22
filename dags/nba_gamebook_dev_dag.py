"""Independent dev-only PDF ingestion. No production pipeline dependencies."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from airflow.decorators import dag, task
from airflow.exceptions import AirflowException, AirflowFailException

from season_config import CURRENT_SEASON

ROOT = Path(__file__).resolve().parents[1]


def run_worker(command, arguments):
    """Use the app dependency environment; XCom receives small results only."""
    output = ROOT / "reports/gamebook-automation" / (command + "-" + uuid4().hex)
    default_python = ROOT / ".venv-app/bin/python"
    python = os.getenv(
        "GAMEBOOK_PYTHON",
        str(default_python) if default_python.exists() else sys.executable,
    )
    try:
        result = subprocess.run(
            [
                python,
                str(ROOT / "scripts/gamebook_ingestion.py"),
                command,
                "--output",
                str(output),
                *arguments,
            ],
            cwd=ROOT,
            timeout=1080,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AirflowException(f"Worker timed out; inspect {output}") from exc
    if result.returncode in (2, 3):
        raise AirflowFailException(
            f"Validation rejected or query needs inspection; see {output}"
        )
    if result.returncode:
        raise AirflowException(f"Dev gamebook attempt failed; see {output}")
    return json.loads((output / "result.json").read_text())


@dag(
    dag_id="nba_gamebook_dev",
    schedule="0 */6 * * *",
    start_date=datetime(2026, 10, 1),
    catchup=False,
    max_active_runs=1,
    max_active_tasks=2,
    is_paused_upon_creation=True,
    default_args={
        "retries": 2,
        "retry_delay": timedelta(minutes=5),
        "retry_exponential_backoff": True,
        "max_retry_delay": timedelta(minutes=30),
    },
    tags=["nba", "gamebook", "dev"],
)
def gamebook_dev_dag():
    @task(execution_timeout=timedelta(minutes=20))
    def discover_games():
        # Actual observation time: delayed/manual runs must not pretend to see historical source state.
        as_of = datetime.now(ZoneInfo("America/New_York"))
        return run_worker(
            "discover",
            [
                "--season",
                os.getenv("GAMEBOOK_SEASON", CURRENT_SEASON),
                "--discovery-source",
                os.getenv("GAMEBOOK_DISCOVERY_SOURCE", "warehouse"),
                "--as-of",
                as_of.isoformat(),
            ],
        )

    @task(execution_timeout=timedelta(minutes=20), max_active_tis_per_dag=1)
    def ingest_game(game):
        return run_worker("process", ["--game-json", json.dumps(game)])

    # A failing mapped game remains a failed leaf; no all_done success can hide it.
    ingest_game.expand(game=discover_games())


nba_gamebook_dev = gamebook_dev_dag()
