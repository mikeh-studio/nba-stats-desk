"""Deterministic pipeline health triage for Airflow DAG runs.

This module keeps the operational logic explicit:
- collect task/XCom signals from an Airflow run
- classify a single primary failure type with ordered rules
- recommend next operator actions
- render and persist a machine-readable incident artifact

It intentionally avoids any LLM dependency. Human-readable summaries are
rendered from structured evidence only.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger("nba_pipeline_triage")

FAILED_STATES = {"failed", "upstream_failed"}
SUCCESS_STATES = {"success"}
SKIPPED_STATES = {"skipped"}
ACTIVE_STATES = {"queued", "scheduled", "running", "up_for_retry", "restarting"}

STAGE_ORDER = [
    "extract_landing",
    "bronze_staging_load",
    "dq_checks",
    "merge_to_bronze_raw",
    "dbt_run_test",
    "similarity_archetype_publish",
    "analysis_snapshot_publish",
    "optional_redshift_sync",
    "run_metadata_publish",
    "unknown",
]

TASK_STAGE_MAP = {
    "extract_incremental": "extract_landing",
    "extract_game_line_scores": "extract_landing",
    "extract_player_reference": "extract_landing",
    "extract_schedule_context": "extract_landing",
    "load_game_log_staging": "bronze_staging_load",
    "load_schedule_staging": "bronze_staging_load",
    "load_game_line_score_staging": "bronze_staging_load",
    "load_player_reference_staging": "bronze_staging_load",
    "dq_game_log_staging": "dq_checks",
    "dq_schedule_staging": "dq_checks",
    "dq_game_line_score_staging": "dq_checks",
    "dq_player_reference_staging": "dq_checks",
    "merge_game_logs": "merge_to_bronze_raw",
    "merge_schedule_context": "merge_to_bronze_raw",
    "merge_game_line_scores": "merge_to_bronze_raw",
    "merge_player_reference": "merge_to_bronze_raw",
    "combine_pipeline_results": "merge_to_bronze_raw",
    "dbt_build": "dbt_run_test",
    "build_player_similarity_assets": "similarity_archetype_publish",
    "build_analysis_snapshot": "analysis_snapshot_publish",
    "check_redshift_enabled": "optional_redshift_sync",
    "export_bigquery_bronze": "optional_redshift_sync",
    "sync_to_s3": "optional_redshift_sync",
    "load_redshift_bronze": "optional_redshift_sync",
    "dbt_build_redshift": "optional_redshift_sync",
    "skip_redshift_sync": "optional_redshift_sync",
    "publish_run_metrics": "run_metadata_publish",
}

FAILURE_TYPE_BY_STAGE = {
    "extract_landing": "ingestion_failure",
    "bronze_staging_load": "ingestion_failure",
    "dq_checks": "dq_failure",
    "merge_to_bronze_raw": "merge_reconciliation_failure",
    "dbt_run_test": "dbt_failure",
    "similarity_archetype_publish": "downstream_publish_failure",
    "analysis_snapshot_publish": "downstream_publish_failure",
    "optional_redshift_sync": "downstream_publish_failure",
    "run_metadata_publish": "downstream_publish_failure",
    "unknown": "unknown_failure",
}

SEVERITY_BY_FAILURE_TYPE = {
    "healthy": "info",
    "ingestion_failure": "high",
    "dq_failure": "high",
    "merge_reconciliation_failure": "high",
    "dbt_failure": "high",
    "downstream_publish_failure": "medium",
    "unknown_failure": "high",
}

INTERESTING_METRIC_KEYS = {
    "domain",
    "gcs_uri",
    "row_count",
    "rows_loaded",
    "rows_inserted",
    "rows_updated",
    "rows_unchanged",
    "staging_table",
    "raw_table",
    "season",
    "watermark_before",
    "watermark_after",
    "dq_results",
    "reconciliation",
    "dbt_status",
    "dbt_command",
    "dbt_failure_summary",
    "similarity_status",
    "similarity_player_count",
    "similarity_archetype_count",
    "analysis_snapshot_status",
    "analysis_snapshot_id",
    "redshift_status",
    "redshift_load_status",
    "redshift_dbt_status",
    "redshift_gcs_prefix",
    "redshift_s3_prefix",
    "should_build",
}

DBT_SELECTOR_PATTERNS = [
    re.compile(r"Failure in model ([A-Za-z0-9_\.]+)"),
    re.compile(r"Failure in test ([A-Za-z0-9_\.]+)"),
    re.compile(r"on model ([A-Za-z0-9_\.]+)"),
    re.compile(r"on test ([A-Za-z0-9_\.]+)"),
]

SECRET_KEY_PATTERN = (
    r"password|passwd|pwd|token|secret|api[_-]?key|access[_-]?key|"
    r"secret[_-]?key|private[_-]?key|client[_-]?secret"
)
SECRET_JSON_PATTERN = re.compile(
    rf"(?i)([\"'](?:{SECRET_KEY_PATTERN})[\"']\s*:\s*)[\"'][^\"']+[\"']"
)
SECRET_ASSIGNMENT_PATTERN = re.compile(rf"(?i)\b({SECRET_KEY_PATTERN})\s*=\s*[^,\s]+")
SECRET_AUTH_PATTERN = re.compile(
    r"(?i)\b(authorization:\s*bearer\s+)[A-Za-z0-9._~+/=-]+"
)
SECRET_SK_PATTERN = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b")


@dataclass
class TaskHealth:
    task_id: str
    stage: str
    state: str
    started_at: str | None = None
    finished_at: str | None = None
    duration_seconds: float | None = None
    try_number: int | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass
class TriageArtifact:
    run_id: str
    run_ts: str
    overall_status: str
    primary_failure_type: str
    severity: str
    impacted_stage: str
    failing_tasks: list[str]
    stage_statuses: dict[str, str]
    evidence: dict[str, Any]
    recommended_actions: list[str]
    human_summary: str
    task_outcomes: list[TaskHealth] = field(default_factory=list)
    artifact_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["task_outcomes"] = [asdict(task) for task in self.task_outcomes]
        return payload


def _serialize_ts(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _sanitize_error(message: Any) -> str | None:
    if message in (None, ""):
        return None
    cleaned = " ".join(str(message).strip().split())
    cleaned = SECRET_JSON_PATTERN.sub(
        lambda match: f'{match.group(1)}"[REDACTED]"', cleaned
    )
    cleaned = SECRET_ASSIGNMENT_PATTERN.sub(
        lambda match: f"{match.group(1)}=[REDACTED]",
        cleaned,
    )
    cleaned = SECRET_AUTH_PATTERN.sub(r"\1[REDACTED]", cleaned)
    cleaned = SECRET_SK_PATTERN.sub("sk-[REDACTED]", cleaned)
    return cleaned[:1000] if cleaned else None


def _stage_for_task(task_id: str) -> str:
    return TASK_STAGE_MAP.get(task_id, "unknown")


def _status_for_stage(states: list[str]) -> str:
    normalized = [state for state in states if state]
    if not normalized:
        return "not_run"
    if any(state in FAILED_STATES for state in normalized):
        return "failed"
    if any(state in ACTIVE_STATES for state in normalized):
        return "running"
    if any(state in SUCCESS_STATES for state in normalized):
        if all(state in SUCCESS_STATES | SKIPPED_STATES for state in normalized):
            return "success"
    if all(state in SKIPPED_STATES for state in normalized):
        return "skipped"
    return normalized[0]


def _interesting_metrics(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {key: value[key] for key in INTERESTING_METRIC_KEYS if key in value}


def _domain_for_task(task: TaskHealth) -> str | None:
    metrics_domain = task.metrics.get("domain")
    if metrics_domain:
        return str(metrics_domain)
    if "schedule" in task.task_id:
        return "schedule"
    if "line_score" in task.task_id:
        return "game_line_scores"
    if "player_reference" in task.task_id:
        return "player_reference"
    if "game_log" in task.task_id or "game_logs" in task.task_id:
        return "game_logs"
    return None


def _collect_domain_metrics(tasks: Iterable[TaskHealth]) -> dict[str, dict[str, Any]]:
    domains: dict[str, dict[str, Any]] = {}
    for task in tasks:
        domain = _domain_for_task(task)
        if not domain:
            continue
        entry = domains.setdefault(domain, {})
        for key in (
            "row_count",
            "rows_loaded",
            "rows_inserted",
            "rows_updated",
            "rows_unchanged",
            "staging_table",
            "raw_table",
            "gcs_uri",
            "watermark_before",
            "watermark_after",
        ):
            if key in task.metrics:
                entry[key] = task.metrics[key]
        if "dq_results" in task.metrics:
            entry["dq_results"] = task.metrics["dq_results"]
        if "reconciliation" in task.metrics:
            entry["reconciliation"] = task.metrics["reconciliation"]
    return domains


def _collect_failed_task_details(tasks: Iterable[TaskHealth]) -> list[dict[str, Any]]:
    failed = []
    for task in tasks:
        if task.state not in FAILED_STATES:
            continue
        failed.append(
            {
                "task_id": task.task_id,
                "stage": task.stage,
                "state": task.state,
                "error": task.error,
                "duration_seconds": task.duration_seconds,
                "try_number": task.try_number,
            }
        )
    return failed


def _collect_stage_statuses(tasks: Iterable[TaskHealth]) -> dict[str, str]:
    grouped: dict[str, list[str]] = {}
    for task in tasks:
        grouped.setdefault(task.stage, []).append(task.state)

    return {
        stage: _status_for_stage(grouped.get(stage, []))
        for stage in STAGE_ORDER
        if stage in grouped or stage != "unknown"
    }


def collect_run_task_health(
    *,
    task_instances: Iterable[Any],
    current_error_by_task: dict[str, str] | None = None,
) -> list[TaskHealth]:
    """Normalize Airflow task instances into deterministic task health records."""
    current_error_by_task = current_error_by_task or {}
    tasks: list[TaskHealth] = []

    for task_instance in task_instances:
        task_id = getattr(task_instance, "task_id", "unknown_task")
        state = getattr(task_instance, "state", "unknown")
        try:
            xcom_value = task_instance.xcom_pull(task_ids=task_id)
        except Exception:
            xcom_value = None

        task = TaskHealth(
            task_id=task_id,
            stage=_stage_for_task(task_id),
            state=state,
            started_at=_serialize_ts(getattr(task_instance, "start_date", None)),
            finished_at=_serialize_ts(getattr(task_instance, "end_date", None)),
            duration_seconds=getattr(task_instance, "duration", None),
            try_number=getattr(task_instance, "try_number", None),
            metrics=_interesting_metrics(xcom_value),
            error=_sanitize_error(current_error_by_task.get(task_id)),
        )
        tasks.append(task)

    rank = {stage: idx for idx, stage in enumerate(STAGE_ORDER)}
    return sorted(tasks, key=lambda item: (rank.get(item.stage, 999), item.task_id))


def _find_primary_failed_task(tasks: Iterable[TaskHealth]) -> TaskHealth | None:
    failed = [task for task in tasks if task.state in FAILED_STATES]
    if not failed:
        return None
    rank = {stage: idx for idx, stage in enumerate(STAGE_ORDER)}
    failed.sort(key=lambda item: (rank.get(item.stage, 999), item.task_id))
    return failed[0]


def _first_dq_issue(
    tasks: Iterable[TaskHealth],
) -> tuple[str, dict[str, Any], TaskHealth] | None:
    for task in tasks:
        dq_results = task.metrics.get("dq_results")
        if not isinstance(dq_results, dict):
            continue
        for metric in (
            "total_rows",
            "null_key_rows",
            "duplicate_key_rows",
            "invalid_season_rows",
            "invalid_season_type_rows",
            "out_of_window_rows",
            "invalid_wl_rows",
            "invalid_pct_rows",
            "invalid_points_rows",
            "missing_name_rows",
        ):
            value = dq_results.get(metric)
            if value in (None, 0):
                continue
            if metric == "total_rows" and value > 0:
                continue
            return metric, dq_results, task
        if dq_results.get("total_rows") == 0:
            return "total_rows", dq_results, task
    return None


def _classify_failure_type(
    tasks: list[TaskHealth], overall_state: str
) -> tuple[str, str]:
    if overall_state == "success" and not any(
        task.state in FAILED_STATES for task in tasks
    ):
        return "healthy", "none"

    dq_issue = _first_dq_issue(tasks)
    if dq_issue:
        return "dq_failure", dq_issue[2].stage

    primary_failed = _find_primary_failed_task(tasks)
    if primary_failed:
        failure_type = FAILURE_TYPE_BY_STAGE.get(
            primary_failed.stage, "unknown_failure"
        )
        if "reconciliation failed" in (primary_failed.error or "").lower():
            failure_type = "merge_reconciliation_failure"
        return failure_type, primary_failed.stage

    if overall_state in {"failed", "upstream_failed"}:
        return "unknown_failure", "unknown"
    return "healthy", "none"


def _extract_dbt_selector(error_text: str | None) -> str | None:
    if not error_text:
        return None
    for pattern in DBT_SELECTOR_PATTERNS:
        match = pattern.search(error_text)
        if match:
            return match.group(1)
    return None


def _recommended_actions(
    *,
    failure_type: str,
    tasks: list[TaskHealth],
    impacted_stage: str,
) -> list[str]:
    if failure_type == "healthy":
        return ["No immediate action required. Monitor the next scheduled run."]

    failed_task = _find_primary_failed_task(tasks)
    domain_metrics = _collect_domain_metrics(tasks)
    actions: list[str] = []

    if failure_type == "ingestion_failure":
        actions.append(
            "Inspect the failed extract/load task logs for API, GCS, or BigQuery load errors, then rerun the affected ingestion window."
        )
        if failed_task and failed_task.metrics.get("gcs_uri"):
            actions.append(
                f"Verify the landed file exists and is readable at {failed_task.metrics['gcs_uri']}."
            )
        actions.append(
            "Confirm the replay window and watermark inputs are correct before rerunning the DAG."
        )
        return actions

    if failure_type == "dq_failure":
        dq_issue = _first_dq_issue(tasks)
        if dq_issue:
            metric, _, task = dq_issue
            staging_table = task.metrics.get(
                "staging_table", "the affected staging table"
            )
            if metric == "total_rows":
                actions.append(
                    f"Rerun the extract window for {task.metrics.get('domain', task.task_id)} and confirm {staging_table} is not empty before DQ."
                )
            if metric == "null_key_rows":
                actions.append(
                    f"Inspect null business keys in {staging_table}; preserve the expected key contract before retrying."
                )
            if metric == "duplicate_key_rows":
                actions.append(
                    f"Inspect duplicate business key violations in {staging_table} before rerunning the merge."
                )
            if metric not in {"total_rows", "null_key_rows", "duplicate_key_rows"}:
                actions.append(
                    f"Inspect {metric} in {staging_table} and correct the staged data before retrying."
                )
        actions.append(
            "Keep the DQ gate blocking until the staged dataset is clean; do not bypass the failing check."
        )
        return actions

    if failure_type == "merge_reconciliation_failure":
        if failed_task:
            raw_table = failed_task.metrics.get("raw_table", "the bronze raw table")
            actions.append(
                f"Review merge reconciliation counts for {raw_table} against staging rows loaded, inserted, updated, and post-merge row count."
            )
        actions.append(
            "Inspect the merge task logs for inserted/updated accounting mismatches before rerunning only the merge stage."
        )
        actions.append(
            "Verify the replay window did not introduce unexpected duplicate business keys in staging."
        )
        return actions

    if failure_type == "dbt_failure":
        selector = _extract_dbt_selector(failed_task.error if failed_task else None)
        if selector:
            actions.append(
                f"Run `dbt build --project-dir . --profiles-dir dbt/profiles --target dev --select {selector}` to reproduce the failing node."
            )
        else:
            actions.append(
                "Review the dbt task logs and rerun the failing dbt model/test selection locally."
            )
        actions.append(
            "Inspect the upstream bronze and silver relations referenced by the failing dbt node before rerunning the DAG."
        )
        return actions

    if failure_type == "downstream_publish_failure":
        if impacted_stage == "optional_redshift_sync":
            actions.append(
                "Verify the optional Redshift dependency path separately; do not block the core BigQuery/dbt platform on this branch."
            )
        else:
            actions.append(
                "Verify the downstream publish inputs in BigQuery and rerun only the affected publish task after the core warehouse path is green."
            )
        actions.append(
            "Check the failing task logs for missing gold inputs, permission issues, or optional dependency outages."
        )
        return actions

    actions.append(
        "Inspect the failing task logs and Airflow task-instance states, then rerun the smallest affected stage once the root cause is confirmed."
    )
    if domain_metrics:
        actions.append(
            "Use the attached domain row counts, DQ metrics, and reconciliation signals to narrow the failure before rerunning."
        )
    return actions


def _build_evidence(
    *,
    tasks: list[TaskHealth],
    overall_state: str,
    failure_type: str,
) -> dict[str, Any]:
    failed_task_details = _collect_failed_task_details(tasks)
    domain_metrics = _collect_domain_metrics(tasks)
    primary_failed = _find_primary_failed_task(tasks)

    evidence = {
        "dag_state": overall_state,
        "failed_task_details": failed_task_details,
        "domain_metrics": domain_metrics,
        "dq_metrics": {
            task.task_id: task.metrics.get("dq_results")
            for task in tasks
            if "dq_results" in task.metrics
        },
        "merge_reconciliation": {
            task.task_id: task.metrics.get("reconciliation")
            for task in tasks
            if "reconciliation" in task.metrics
        },
        "dbt": {
            "task_id": primary_failed.task_id
            if primary_failed and primary_failed.stage == "dbt_run_test"
            else "dbt_build",
            "status": (
                "failed"
                if failure_type == "dbt_failure"
                else next(
                    (
                        task.metrics.get("dbt_status")
                        for task in tasks
                        if task.task_id == "dbt_build"
                        and task.metrics.get("dbt_status")
                    ),
                    "unknown",
                )
            ),
            "failure_summary": primary_failed.error
            if primary_failed and primary_failed.stage == "dbt_run_test"
            else None,
            "command": next(
                (
                    task.metrics.get("dbt_command")
                    for task in tasks
                    if task.task_id == "dbt_build" and task.metrics.get("dbt_command")
                ),
                "dbt build --project-dir . --profiles-dir dbt/profiles --target dev",
            ),
        },
        "task_timestamps": {
            task.task_id: {
                "started_at": task.started_at,
                "finished_at": task.finished_at,
                "duration_seconds": task.duration_seconds,
            }
            for task in tasks
        },
    }
    return evidence


def render_human_summary(artifact: TriageArtifact) -> str:
    if artifact.primary_failure_type == "healthy":
        return (
            f"Run {artifact.run_id} completed cleanly. Core stages passed: "
            f"extract/landing, bronze load, DQ, merge, and dbt. "
            "No operator action is required."
        )

    failed_task_details = artifact.evidence.get("failed_task_details", [])
    first_failed = failed_task_details[0] if failed_task_details else {}
    detail = ""
    if first_failed.get("error"):
        detail = f" Evidence: {first_failed['error']}."

    return (
        f"Run {artifact.run_id} failed with primary classification "
        f"{artifact.primary_failure_type} at stage {artifact.impacted_stage}. "
        f"Failing task(s): {', '.join(artifact.failing_tasks) or 'unknown'}."
        f"{detail} Next: {artifact.recommended_actions[0]}"
    )


def classify_run_health(
    *,
    run_id: str,
    run_ts: str,
    overall_state: str,
    tasks: list[TaskHealth],
) -> TriageArtifact:
    failure_type, impacted_stage = _classify_failure_type(tasks, overall_state)
    stage_statuses = _collect_stage_statuses(tasks)
    failing_tasks = [task.task_id for task in tasks if task.state in FAILED_STATES]
    overall_status = "healthy" if failure_type == "healthy" else "failed"
    evidence = _build_evidence(
        tasks=tasks,
        overall_state=overall_state,
        failure_type=failure_type,
    )
    recommended_actions = _recommended_actions(
        failure_type=failure_type,
        tasks=tasks,
        impacted_stage=impacted_stage,
    )

    artifact = TriageArtifact(
        run_id=run_id,
        run_ts=run_ts,
        overall_status=overall_status,
        primary_failure_type=failure_type,
        severity=SEVERITY_BY_FAILURE_TYPE[failure_type],
        impacted_stage=impacted_stage,
        failing_tasks=failing_tasks,
        stage_statuses=stage_statuses,
        evidence=evidence,
        recommended_actions=recommended_actions,
        human_summary="",
        task_outcomes=tasks,
    )
    artifact.human_summary = render_human_summary(artifact)
    return artifact


def _artifact_dir() -> Path:
    configured = os.getenv("PIPELINE_TRIAGE_DIR")
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[1] / "reports" / "pipeline_triage"


def write_triage_artifact(artifact: TriageArtifact) -> str:
    """Persist the triage artifact as JSON and return the path."""
    safe_run_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", artifact.run_id)
    dag_dir = _artifact_dir() / "nba_analytics_pipeline"
    dag_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = dag_dir / f"{safe_run_id}.json"
    artifact.artifact_path = str(artifact_path)
    artifact_path.write_text(
        json.dumps(artifact.to_dict(), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return str(artifact_path)


def summarize_subprocess_failure(
    *,
    command: list[str],
    returncode: int,
    stdout: str | None,
    stderr: str | None,
) -> str:
    """Build a concise deterministic error summary for failed subprocess tasks."""
    output = stderr or stdout or ""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    tail = " | ".join(lines[-8:]) if lines else "no subprocess output captured"
    return (
        _sanitize_error(
            f"Command `{' '.join(command)}` failed with exit code {returncode}. {tail}"
        )
        or f"Command `{' '.join(command)}` failed with exit code {returncode}."
    )


def emit_triage_from_context(context: dict[str, Any]) -> dict[str, Any] | None:
    """Collect task states for a run, classify them, and write a JSON artifact."""
    dag_run = context.get("dag_run")
    if dag_run is None:
        logger.warning("Skipping triage artifact write because dag_run is unavailable")
        return None

    current_error_by_task: dict[str, str] = {}
    task_instance = context.get("task_instance")
    exception = context.get("exception")
    if task_instance is not None and exception is not None:
        current_error_by_task[task_instance.task_id] = _sanitize_error(exception) or ""

    try:
        task_instances = dag_run.get_task_instances()
        tasks = collect_run_task_health(
            task_instances=task_instances,
            current_error_by_task=current_error_by_task,
        )
        run_ts = _serialize_ts(
            getattr(dag_run, "logical_date", None)
            or getattr(dag_run, "start_date", None)
            or datetime.now(timezone.utc)
        )
        artifact = classify_run_health(
            run_id=dag_run.run_id,
            run_ts=run_ts or datetime.now(timezone.utc).isoformat(),
            overall_state=getattr(dag_run, "state", "unknown"),
            tasks=tasks,
        )
        write_triage_artifact(artifact)
        logger.info("Wrote pipeline triage artifact to %s", artifact.artifact_path)
        logger.info("Pipeline triage summary: %s", artifact.human_summary)
        return artifact.to_dict()
    except Exception:
        logger.exception(
            "Failed to emit pipeline triage artifact for run %s", dag_run.run_id
        )
        return None


def write_pipeline_triage_on_success(context: dict[str, Any]) -> None:
    emit_triage_from_context(context)


def write_pipeline_triage_on_failure(context: dict[str, Any]) -> None:
    emit_triage_from_context(context)
