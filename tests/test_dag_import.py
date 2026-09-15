from __future__ import annotations

import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("AIRFLOW_HOME", tempfile.mkdtemp(prefix="airflow_home_"))
airflow = pytest.importorskip("airflow")
from airflow.models import DagBag


def test_airflow_dag_parses_without_import_errors(tmp_path):
    dags_path = Path(__file__).resolve().parents[1] / "dags"
    dag_bag = DagBag(dag_folder=str(dags_path), include_examples=False)

    assert dag_bag.import_errors == {}
    assert "nba_analytics_pipeline" in dag_bag.dags


@pytest.mark.parametrize("core_code,injury_code", [(0, 0), (0, 1), (1, 0)])
def test_dbt_injury_candidates_cannot_block_or_replace_successful_core(
    monkeypatch, core_code, injury_code
):
    from google.cloud import bigquery

    import dbt_builds
    import publication

    dag_bag = DagBag(
        dag_folder=str(Path(__file__).resolve().parents[1] / "dags"),
        include_examples=False,
    )
    function = (
        dag_bag.dags["nba_analytics_pipeline"].get_task("dbt_build").python_callable
    )
    scope = function.__globals__
    monkeypatch.setitem(
        scope,
        "get_config",
        lambda key, default=None: {"BQ_PROJECT": "demo"}.get(key, default),
    )
    monkeypatch.setattr(bigquery, "Client", lambda **kwargs: object())
    monkeypatch.setattr(publication, "expire_candidate", lambda *args: None)
    published = []
    monkeypatch.setattr(
        publication,
        "publish_candidates",
        lambda client, candidates: published.extend(candidates),
    )
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(
            returncode=core_code if len(commands) == 1 else injury_code,
            stdout="",
            stderr="test failure",
        )

    monkeypatch.setattr(dbt_builds.subprocess, "run", run)
    context = {
        "should_build": True,
        "core_warehouse_changed": True,
        "injury_report_rows_loaded": 4,
    }
    if core_code:
        with pytest.raises(Exception, match="test failure"):
            function(context)
        assert len(commands) == 1
        assert published == []
        return
    result = function(context)
    assert result["dbt_status"] == "success"
    assert commands[0][-1] == "stg_player_injury_reports_clean+"
    assert "--exclude" in commands[0]
    assert "--select" in commands[1]
    assert "--vars" in commands[1]
    if injury_code:
        assert result["injury_status"] == "failed_non_blocking"
        assert published == []
    else:
        assert result["injury_dbt_status"] == "success"
        assert len(published) == 4
        assert all(
            "_candidate_" in candidate and "_candidate_" not in active
            for active, candidate in published
        )


def test_pipeline_task_graph_and_execution_policies_remain_stable():
    dag_bag = DagBag(
        dag_folder=str(Path(__file__).resolve().parents[1] / "dags"),
        include_examples=False,
    )
    dag = dag_bag.dags["nba_analytics_pipeline"]
    chains = [
        (
            "extract_incremental",
            "load_game_log_staging",
            "dq_game_log_staging",
            "merge_game_logs",
        ),
        (
            "extract_schedule_context",
            "load_schedule_staging",
            "dq_schedule_staging",
            "merge_schedule_context",
        ),
        (
            "extract_game_line_scores",
            "load_game_line_score_staging",
            "dq_game_line_score_staging",
            "merge_game_line_scores",
        ),
        (
            "extract_player_shot_locations",
            "load_player_shot_location_staging",
            "dq_player_shot_location_staging",
            "merge_player_shot_locations",
        ),
        (
            "extract_player_reference",
            "load_player_reference_staging",
            "dq_player_reference_staging",
            "merge_player_reference",
        ),
        (
            "extract_injury_reports",
            "load_injury_report_staging",
            "dq_injury_report_staging",
            "merge_injury_reports",
        ),
    ]
    upstream = {}
    for extract, load, check, merge in chains:
        upstream[extract] = (
            {"extract_incremental"} if extract == "extract_game_line_scores" else set()
        )
        upstream[load] = {extract}
        upstream[check] = {load}
        upstream[merge] = {check}
        for task_id, retries, delay in [
            (extract, 2, 300),
            (load, 2, 120),
            (check, 0, 120),
            (merge, 1, 120),
        ]:
            task = dag.get_task(task_id)
            assert task.retries == retries
            assert task.retry_delay.total_seconds() == delay
            assert task.execution_timeout.total_seconds() == 2700
            assert task.trigger_rule == "all_success"
    upstream.update(
        {
            "bootstrap_bronze_contract": {
                "merge_game_logs",
                "merge_schedule_context",
                "merge_game_line_scores",
                "merge_player_reference",
            },
            "combine_pipeline_results": {chain[-1] for chain in chains}
            | {"bootstrap_bronze_contract"},
            "dbt_build": {"combine_pipeline_results"},
            "build_player_similarity_assets": {"dbt_build"},
            "publish_run_metrics": {"build_player_similarity_assets"},
            "check_redshift_enabled": {"combine_pipeline_results"},
            "export_bigquery_bronze": {
                "combine_pipeline_results",
                "check_redshift_enabled",
            },
            "sync_to_s3": {"export_bigquery_bronze"},
            "load_redshift_bronze": {"sync_to_s3"},
            "dbt_build_redshift": {"load_redshift_bronze"},
            "skip_redshift_sync": {
                "combine_pipeline_results",
                "check_redshift_enabled",
            },
        }
    )
    assert {task.task_id: task.upstream_task_ids for task in dag.tasks} == upstream
    assert dag.max_active_runs == 1
    assert dag.catchup is False


@pytest.mark.parametrize(
    "task_id",
    ["load_injury_report_staging", "dq_injury_report_staging", "merge_injury_reports"],
)
def test_injury_task_adapters_keep_retry_then_soft_failure(monkeypatch, task_id):
    from airflow.operators import python

    dag_bag = DagBag(
        dag_folder=str(Path(__file__).resolve().parents[1] / "dags"),
        include_examples=False,
    )
    task = dag_bag.dags["nba_analytics_pipeline"].get_task(task_id)
    function = task.python_callable
    scope = function.__wrapped__.__globals__
    helper = {
        "load_injury_report_staging": "load_staging",
        "dq_injury_report_staging": "check_staging",
        "merge_injury_reports": "merge_staging",
    }[task_id]
    calls = []

    def fail(*args, **kwargs):
        calls.append(helper)
        raise RuntimeError("stage unavailable")

    monkeypatch.setitem(scope, helper, fail)
    monkeypatch.setitem(scope, "get_project_id", lambda: "demo")
    monkeypatch.setitem(scope, "get_config", lambda key, default=None: default)
    ti = SimpleNamespace(try_number=1, max_tries=task.retries)
    monkeypatch.setattr(python, "get_current_context", lambda: {"ti": ti})
    previous = {"watermark_before": "2026-01-01", "watermark_after": "2026-01-03"}
    if task.retries:
        with pytest.raises(RuntimeError, match="stage unavailable"):
            function(previous)
    ti.try_number = task.retries + 1
    failed = function(previous)
    assert failed["asset_status"] == "failed_non_blocking"
    assert failed["watermark_after"] == "2026-01-01"
    assert failed["rows_loaded"] == 0
    count = len(calls)
    assert function(failed) is failed
    assert len(calls) == count
