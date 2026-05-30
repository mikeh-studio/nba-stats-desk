from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dags"))

from nba_pipeline_triage import (
    TaskHealth,
    classify_run_health,
    summarize_subprocess_failure,
)


def make_task(
    task_id: str,
    *,
    stage: str,
    state: str = "success",
    metrics: dict | None = None,
    error: str | None = None,
) -> TaskHealth:
    return TaskHealth(
        task_id=task_id,
        stage=stage,
        state=state,
        metrics=metrics or {},
        error=error,
    )


def test_classify_healthy_run() -> None:
    tasks = [
        make_task(
            "extract_incremental",
            stage="extract_landing",
            metrics={"domain": "game_logs", "row_count": 24},
        ),
        make_task(
            "load_game_log_staging",
            stage="bronze_staging_load",
            metrics={"domain": "game_logs", "row_count": 24},
        ),
        make_task(
            "dq_game_log_staging",
            stage="dq_checks",
            metrics={
                "domain": "game_logs",
                "staging_table": "project.nba_bronze.stg_game_logs",
                "dq_results": {
                    "total_rows": 24,
                    "null_key_rows": 0,
                    "duplicate_key_rows": 0,
                },
            },
        ),
        make_task(
            "merge_game_logs",
            stage="merge_to_bronze_raw",
            metrics={
                "domain": "game_logs",
                "rows_loaded": 24,
                "rows_inserted": 12,
                "rows_updated": 8,
                "rows_unchanged": 4,
                "reconciliation": {
                    "rows_loaded": 24,
                    "pre_count": 200,
                    "post_count": 212,
                    "inserted": 12,
                    "updated": 8,
                    "unchanged": 4,
                },
            },
        ),
        make_task(
            "dbt_build",
            stage="dbt_run_test",
            metrics={"dbt_status": "success"},
        ),
    ]

    artifact = classify_run_health(
        run_id="manual__2026-04-21T11:00:00+00:00",
        run_ts="2026-04-21T11:00:00+00:00",
        overall_state="success",
        tasks=tasks,
    )

    assert artifact.overall_status == "healthy"
    assert artifact.primary_failure_type == "healthy"
    assert artifact.severity == "info"
    assert artifact.failing_tasks == []
    assert "completed cleanly" in artifact.human_summary


def test_classify_dq_failures_for_zero_null_and_duplicate_keys() -> None:
    scenarios = [
        (
            {"total_rows": 0, "null_key_rows": 0, "duplicate_key_rows": 0},
            "Rerun the extract window",
        ),
        (
            {"total_rows": 12, "null_key_rows": 2, "duplicate_key_rows": 0},
            "Inspect null business keys",
        ),
        (
            {"total_rows": 12, "null_key_rows": 0, "duplicate_key_rows": 3},
            "Inspect duplicate business key violations",
        ),
    ]

    for dq_results, expected_action_fragment in scenarios:
        tasks = [
            make_task(
                "dq_game_log_staging",
                stage="dq_checks",
                state="failed",
                metrics={
                    "domain": "game_logs",
                    "staging_table": "project.nba_bronze.stg_game_logs",
                    "dq_results": dq_results,
                },
                error="DQ failed in staging",
            )
        ]

        artifact = classify_run_health(
            run_id="manual__2026-04-21T12:00:00+00:00",
            run_ts="2026-04-21T12:00:00+00:00",
            overall_state="failed",
            tasks=tasks,
        )

        assert artifact.primary_failure_type == "dq_failure"
        assert artifact.impacted_stage == "dq_checks"
        assert artifact.failing_tasks == ["dq_game_log_staging"]
        assert any(
            expected_action_fragment in action
            for action in artifact.recommended_actions
        )


def test_classify_merge_reconciliation_failure() -> None:
    tasks = [
        make_task(
            "merge_game_logs",
            stage="merge_to_bronze_raw",
            state="failed",
            metrics={
                "domain": "game_logs",
                "raw_table": "project.nba_bronze.raw_game_logs",
                "rows_loaded": 25,
            },
            error="Reconciliation failed for game_logs: expected post_count 120 from pre_count 110 + inserted 8, got 119",
        )
    ]

    artifact = classify_run_health(
        run_id="manual__2026-04-21T13:00:00+00:00",
        run_ts="2026-04-21T13:00:00+00:00",
        overall_state="failed",
        tasks=tasks,
    )

    assert artifact.primary_failure_type == "merge_reconciliation_failure"
    assert artifact.impacted_stage == "merge_to_bronze_raw"
    assert any(
        "Review merge reconciliation counts" in action
        for action in artifact.recommended_actions
    )


def test_classify_dbt_failure() -> None:
    tasks = [
        make_task(
            "dbt_build",
            stage="dbt_run_test",
            state="failed",
            error=(
                "Command `dbt build --project-dir . --profiles-dir dbt/profiles --target dev` "
                "failed with exit code 1. Failure in model player_similarity_feature_input"
            ),
        )
    ]

    artifact = classify_run_health(
        run_id="manual__2026-04-21T14:00:00+00:00",
        run_ts="2026-04-21T14:00:00+00:00",
        overall_state="failed",
        tasks=tasks,
    )

    assert artifact.primary_failure_type == "dbt_failure"
    assert artifact.impacted_stage == "dbt_run_test"
    assert any(
        "player_similarity_feature_input" in action
        for action in artifact.recommended_actions
    )
    assert artifact.evidence["dbt"]["failure_summary"] is not None


def test_classify_unknown_failure_fallback() -> None:
    tasks = [
        make_task(
            "mystery_task",
            stage="unknown",
            state="failed",
            error="Unexpected failure with no mapped stage",
        )
    ]

    artifact = classify_run_health(
        run_id="manual__2026-04-21T15:00:00+00:00",
        run_ts="2026-04-21T15:00:00+00:00",
        overall_state="failed",
        tasks=tasks,
    )

    assert artifact.primary_failure_type == "unknown_failure"
    assert artifact.impacted_stage == "unknown"
    assert artifact.severity == "high"


def test_subprocess_failure_summary_redacts_obvious_secrets() -> None:
    secret_value = "plain-" + "text"
    secret_key = "sk-" + "testredactiontoken12345"
    summary = summarize_subprocess_failure(
        command=["dbt", "build"],
        returncode=1,
        stdout=None,
        stderr=(
            f'password={secret_value}\n{{"api_key": "{secret_value}"}}\n'
            f"Authorization: Bearer {secret_value}\n{secret_key}"
        ),
    )

    assert "plain-text" not in summary
    assert "password=[REDACTED]" in summary
    assert '"api_key": "[REDACTED]"' in summary
    assert "Authorization: Bearer [REDACTED]" in summary
    assert "sk-[REDACTED]" in summary
