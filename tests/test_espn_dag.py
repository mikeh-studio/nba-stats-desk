import os
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("AIRFLOW_HOME", tempfile.mkdtemp(prefix="espn_dag_"))
pytest.importorskip("airflow")
from airflow.models import DagBag


def test_espn_dag_is_paused_isolated_and_failures_remain_visible():
    bag = DagBag(
        dag_folder=str(Path(__file__).resolve().parents[1] / "dags"),
        include_examples=False,
    )
    assert not bag.import_errors
    dag = bag.dags["nba_espn_dev"]
    assert dag.is_paused_upon_creation and not dag.catchup and dag.max_active_runs == 1
    assert set(dag.task_ids) == {"discover_games", "ingest_game"}
    assert dag.get_task("ingest_game").max_active_tis_per_dag == 1
    for task in dag.tasks:
        assert task.retries == 2 and task.retry_exponential_backoff
        assert task.trigger_rule == "all_success"


def test_espn_dag_uses_configured_current_season(monkeypatch):
    bag = DagBag(
        dag_folder=str(Path(__file__).resolve().parents[1] / "dags"),
        include_examples=False,
    )
    function = bag.dags["nba_espn_dev"].get_task("discover_games").python_callable
    calls = []
    monkeypatch.setitem(
        function.__globals__, "worker", lambda *args: calls.append(args) or []
    )
    assert function() == []
    assert calls[0][0] == "discover"
    assert calls[0][1] == ["--season", function.__globals__["CURRENT_SEASON"]]
