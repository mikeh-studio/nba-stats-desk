from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("AIRFLOW_HOME", tempfile.mkdtemp(prefix="gamebook_dag_"))
pytest.importorskip("airflow")
from airflow.exceptions import AirflowException, AirflowFailException  # noqa: E402
from airflow.models import DagBag  # noqa: E402


def load_dag():
    bag = DagBag(
        dag_folder=str(Path(__file__).resolve().parents[1] / "dags"),
        include_examples=False,
    )
    assert bag.import_errors == {}
    return bag.dags["nba_gamebook_dev"]


def test_dev_dag_is_isolated_bounded_and_preserves_failed_leaves():
    dag = load_dag()
    assert set(dag.task_ids) == {"discover_games", "ingest_game"}
    assert dag.get_task("ingest_game").upstream_task_ids == {"discover_games"}
    assert dag.max_active_runs == 1 and dag.catchup is False
    assert dag.is_paused_upon_creation is True
    for task in dag.tasks:
        assert task.retries == 2
        assert task.retry_exponential_backoff is True
        assert task.execution_timeout.total_seconds() == 1200
        assert task.trigger_rule == "all_success"
    assert dag.get_task("ingest_game").max_active_tis_per_dag == 1


@pytest.mark.parametrize(
    "code,error",
    [
        (1, AirflowException),
        (2, AirflowFailException),
        (3, AirflowFailException),
        (-9, AirflowException),
    ],
)
def test_worker_exit_codes_control_retry_and_failures(
    monkeypatch, tmp_path, code, error
):
    function = (
        load_dag().get_task("discover_games").python_callable.__globals__["run_worker"]
    )
    monkeypatch.setitem(function.__globals__, "ROOT", tmp_path)
    monkeypatch.setattr(
        subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=code)
    )
    with pytest.raises(error):
        function("discover", [])


def test_worker_success_has_no_runtime_llm_or_shared_file_xcom(monkeypatch, tmp_path):
    function = (
        load_dag().get_task("discover_games").python_callable.__globals__["run_worker"]
    )
    monkeypatch.setitem(function.__globals__, "ROOT", tmp_path)
    monkeypatch.setenv("GAMEBOOK_PYTHON", "/example/python")

    def run(command, **kwargs):
        assert command[0] == "/example/python"
        assert kwargs["timeout"] == 1080
        assert "--output" in command and not kwargs.get("shell")
        output = Path(command[command.index("--output") + 1])
        output.mkdir(parents=True)
        (output / "result.json").write_text(json.dumps([{"game_id": "0022500001"}]))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", run)
    assert function("discover", []) == [{"game_id": "0022500001"}]


def test_scheduled_discovery_defaults_to_warehouse_daily_mode(monkeypatch):
    function = load_dag().get_task("discover_games").python_callable
    monkeypatch.delenv("GAMEBOOK_DISCOVERY_SOURCE", raising=False)
    calls = []
    monkeypatch.setitem(
        function.__globals__,
        "run_worker",
        lambda command, args: calls.append((command, args)) or [],
    )
    assert function() == []
    command, args = calls[0]
    assert command == "discover"
    assert args[args.index("--discovery-source") + 1] == "warehouse"
    assert "--mode" not in args and "--start-date" not in args
