"""Independent paused ESPN pilot; no production NBA API metadata writes."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

from airflow.decorators import dag, task
from airflow.exceptions import AirflowException, AirflowFailException

from season_config import CURRENT_SEASON

ROOT = Path(__file__).resolve().parents[1]


def worker(command, arguments):
    output = ROOT / "reports/espn-automation" / (command + "-" + uuid4().hex)
    try:
        result = subprocess.run(
            [
                os.getenv("ESPN_PYTHON", str(ROOT / ".venv-app/bin/python")),
                str(ROOT / "scripts/espn_ingestion.py"),
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
        raise AirflowException(f"ESPN worker timed out: {output}") from exc
    if result.returncode in (2, 3):
        raise AirflowFailException(
            f"ESPN validation/query inspection required: {output}"
        )
    if result.returncode:
        raise AirflowException(f"ESPN source/worker failed: {output}")
    return json.loads((output / "result.json").read_text())


@dag(
    dag_id="nba_espn_dev",
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
    tags=["nba", "espn", "dev"],
)
def espn_dev():
    @task(execution_timeout=timedelta(minutes=20))
    def discover_games():
        return worker("discover", ["--season", CURRENT_SEASON])

    @task(execution_timeout=timedelta(minutes=20), max_active_tis_per_dag=1)
    def ingest_game(game):
        return worker("process", ["--game-json", json.dumps(game)])

    ingest_game.expand(game=discover_games())


nba_espn_dev = espn_dev()
