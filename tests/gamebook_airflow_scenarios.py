"""Run real Airflow mapping/retries with synthetic HTTP and local publication.

Use a fresh AIRFLOW_HOME under reports/. Workers use GAMEBOOK_PYTHON (or the
app venv), just like the deployed DAG, without altering Airflow's dependencies.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))


def fixture_attempt(output, phase, game, late):
    from scripts import gamebook_ingestion as worker

    from test_gamebook_ingestion import (
        LocalCloud,
        Response,
        Session,
        game_session,
        synthetic_sources,
    )

    assisted = phase not in ("correction", "failed_replacement")
    bad = phase == "failed_replacement" and game["game_id"].endswith("2")
    data = synthetic_sources(
        output / "source",
        assisted=assisted if not bad else True,
        box_assists=2 if bad else None,
    )
    store = LocalCloud(output.parent / "store")
    try:
        session = (
            Session(Response(b"not posted yet", 404)) if late else game_session(*data)
        )
        outcome = worker.process_game(game, output, store, session)
        (output / "result.json").write_text(json.dumps(outcome))
        return (
            0
            if outcome["status"] in ("published", "noop")
            else 1
            if outcome["retryable"]
            else 2
        )
    finally:
        store.local.close()


def main():
    home = Path(os.environ["AIRFLOW_HOME"]).resolve()
    assert home.is_relative_to(ROOT / "reports") and not (home / "airflow.db").exists()
    from airflow.exceptions import AirflowException, AirflowFailException
    from airflow.models import DagBag
    from airflow.utils import db
    from airflow.utils.timezone import datetime

    from test_gamebook_assists import GAME

    game = {**GAME, "game_id": "0022500001"}
    db.initdb()
    output = home / "scenarios"
    output.mkdir()
    bag = DagBag(dag_folder=str(ROOT / "dags"), include_examples=False)
    assert not bag.import_errors
    dag = bag.dags["nba_gamebook_dev"]
    for task in dag.tasks:
        task.retry_delay = timedelta(0)
        task.retry_exponential_backoff = False
    scope = dag.get_task("discover_games").python_callable.__globals__
    phase = ""
    calls = []

    def run_worker(command, arguments):
        attempt_dir = output / f"attempt-{len(calls)}"
        attempt_dir.mkdir()
        calls.append((phase, command))
        if command == "discover":
            if phase == "blocked_discovery":
                raise AirflowException("synthetic source outage")
            if phase == "empty":
                return []
            return (
                [game, {**game, "game_id": "0022500002"}]
                if phase == "failed_replacement"
                else [game]
            )
        late = (
            phase == "initial_retry"
            and sum(p == phase and c == "process" for p, c in calls) == 1
        )
        python = os.getenv("GAMEBOOK_PYTHON", str(ROOT / ".venv-app/bin/python"))
        completed = subprocess.run(
            [
                python,
                __file__,
                "--attempt",
                str(attempt_dir),
                phase,
                arguments[1],
                str(int(late)),
            ],
            check=False,
            timeout=120,
        )
        if completed.returncode == 2:
            raise AirflowFailException("synthetic failed validation")
        if completed.returncode:
            raise AirflowException("synthetic transient source failure")
        return json.loads((attempt_dir / "result.json").read_text())

    results = []
    with patch.dict(scope, run_worker=run_worker):
        for i, (phase, expected) in enumerate(
            (
                ("initial_retry", "success"),
                ("replay", "success"),
                ("correction", "success"),
                ("failed_replacement", "failed"),
                ("empty", "success"),
                ("blocked_discovery", "failed"),
            )
        ):
            run = dag.test(execution_date=datetime(2026, 10, 10 + i))
            states = [
                {
                    "task": t.task_id,
                    "map_index": t.map_index,
                    "state": t.state,
                    "tries": t.try_number,
                }
                for t in run.get_task_instances()
            ]
            assert run.state == expected, (phase, run.state, states)
            results.append({"scenario": phase, "state": run.state, "tasks": states})
    assert sum(p == "initial_retry" and c == "process" for p, c in calls) == 2
    assert sum(p == "blocked_discovery" and c == "discover" for p, c in calls) == 3
    failed = next(r for r in results if r["scenario"] == "failed_replacement")
    assert {t["state"] for t in failed["tasks"] if t["task"] == "ingest_game"} == {
        "success",
        "failed",
    }
    with sqlite3.connect(output / "store/store.sqlite3") as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM current_event_relationships").fetchone()[
                0
            ]
            == 0
        )
    report = {
        "scope": "Real Airflow DAG execution; synthetic HTTP and local SQLite publication, not live cloud/scheduler deployment",
        "scenarios": results,
        "calls": calls,
    }
    (output / "results.json").write_text(json.dumps(report, indent=2) + "\n")
    print(f"PASS: {len(results)} Airflow scenarios; {output / 'results.json'}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--attempt":
        sys.exit(
            fixture_attempt(
                Path(sys.argv[2]),
                sys.argv[3],
                json.loads(sys.argv[4]),
                bool(int(sys.argv[5])),
            )
        )
    main()
