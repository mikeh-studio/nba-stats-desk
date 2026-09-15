from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dags"))


@pytest.fixture
def redshift_dag(tmp_path, monkeypatch):
    monkeypatch.setenv("AIRFLOW_HOME", str(tmp_path / "airflow"))
    pytest.importorskip("airflow")
    from airflow import DAG
    from airflow.decorators import task

    from redshift_tasks import add_redshift_branch

    config = {
        "GCS_BUCKET_NAME": "gcs",
        "AWS_S3_BUCKET_NAME": "s3",
        "REDSHIFT_IAM_ROLE_ARN": "role",
    }
    with DAG(
        "redshift_contract",
        start_date=datetime(2025, 1, 1),
        schedule=None,
        default_args={
            "retry_delay": timedelta(minutes=2),
            "execution_timeout": timedelta(minutes=45),
        },
    ) as dag:

        @task
        def combine_pipeline_results():
            return {}

        add_redshift_branch(
            combine_pipeline_results(),
            get_config=lambda key, default=None: config.get(key, default),
            get_project_id=lambda: "demo",
            get_dataset=lambda key, default: default,
            get_dbt_repo_root=lambda: tmp_path,
            season="2025-26",
        )
    return dag, config


def test_redshift_factory_keeps_named_tasks_edges_and_retries(redshift_dag):
    dag, _ = redshift_dag
    assert {task.task_id: task.upstream_task_ids for task in dag.tasks} == {
        "combine_pipeline_results": set(),
        "check_redshift_enabled": {"combine_pipeline_results"},
        "export_bigquery_bronze": {
            "combine_pipeline_results",
            "check_redshift_enabled",
        },
        "sync_to_s3": {"export_bigquery_bronze"},
        "load_redshift_bronze": {"sync_to_s3"},
        "dbt_build_redshift": {"load_redshift_bronze"},
        "skip_redshift_sync": {"combine_pipeline_results", "check_redshift_enabled"},
    }
    for task in dag.tasks:
        if task.task_id == "combine_pipeline_results":
            continue
        assert task.retries == (
            0 if task.task_id in {"check_redshift_enabled", "skip_redshift_sync"} else 1
        )
        assert task.retry_delay.total_seconds() == (
            300 if task.task_id == "dbt_build_redshift" else 120
        )
        assert task.trigger_rule == "all_success"


@pytest.mark.parametrize(
    "enabled,changed,expected",
    [
        ("false", True, "skip_redshift_sync"),
        ("true", False, "skip_redshift_sync"),
        ("true", True, "export_bigquery_bronze"),
    ],
)
def test_redshift_branch_decision_is_unchanged(
    redshift_dag, enabled, changed, expected
):
    dag, config = redshift_dag
    config["ENABLE_REDSHIFT"] = enabled
    assert (
        dag.get_task("check_redshift_enabled").python_callable(
            {"should_build": changed}
        )
        == expected
    )


def test_redshift_exports_copies_and_loads_the_same_four_tables(
    redshift_dag, monkeypatch
):
    import nba_redshift_sync as sync

    dag, _ = redshift_dag
    mocks = {}
    for name in [
        "export_bq_to_gcs_parquet",
        "copy_gcs_to_s3",
        "create_redshift_schemas_and_tables",
        "load_s3_to_redshift",
        "merge_redshift_staging",
        "run_redshift_dq_checks",
    ]:
        mocks[name] = Mock()
        monkeypatch.setattr(sync, name, mocks[name])
    result = {}
    for task_id in ["export_bigquery_bronze", "sync_to_s3", "load_redshift_bronze"]:
        assert dag.get_task(task_id).python_callable(result) is result
    tables = [
        "raw_game_logs",
        "raw_schedule",
        "raw_game_line_scores",
        "raw_player_reference",
    ]
    keys = [
        ["player_id", "game_date", "matchup"],
        ["schedule_date", "team_abbr", "opponent_abbr"],
        ["game_id", "team_id"],
        ["player_id"],
    ]
    prefix = result["redshift_gcs_prefix"]
    assert result["redshift_s3_prefix"] == prefix
    for index, (table, key) in enumerate(zip(tables, keys)):
        assert mocks["export_bq_to_gcs_parquet"].call_args_list[index].args == (
            "demo",
            "nba_bronze",
            table,
            "gcs",
            prefix,
        )
        assert mocks["copy_gcs_to_s3"].call_args_list[index].args == (
            "gcs",
            f"{prefix}/{table}/",
            "s3",
            f"{prefix}/{table}",
        )
        assert mocks["load_s3_to_redshift"].call_args_list[index].args == (
            "s3",
            f"{prefix}/{table}/",
            "nba_bronze",
            table,
            "role",
        )
        assert mocks["merge_redshift_staging"].call_args_list[index].args == (
            "nba_bronze",
            table,
            key,
        )
        assert mocks["run_redshift_dq_checks"].call_args_list[index].args == (
            "nba_bronze",
            table,
            key,
        )
    assert all(
        mock.call_count == 4
        for name, mock in mocks.items()
        if name != "create_redshift_schemas_and_tables"
    )
    assert result["redshift_load_status"] == "success"


def test_redshift_errors_still_propagate(redshift_dag, monkeypatch):
    import nba_redshift_sync as sync

    dag, _ = redshift_dag
    monkeypatch.setattr(
        sync, "copy_gcs_to_s3", Mock(side_effect=RuntimeError("copy failed"))
    )
    with pytest.raises(RuntimeError, match="copy failed"):
        dag.get_task("sync_to_s3").python_callable({"redshift_gcs_prefix": "run"})
