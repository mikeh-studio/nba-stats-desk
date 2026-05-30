from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import airflow_live_validate as validate


class FakeRunner:
    def __init__(self, stdout: str):
        self.stdout = stdout
        self.calls: list[list[str]] = []

    def run(self, args: list[str], **_: object) -> validate.CommandResult:
        self.calls.append(args)
        return validate.CommandResult(
            args=args,
            returncode=0,
            stdout=self.stdout,
            stderr="",
            elapsed_seconds=0.01,
        )


def test_extract_json_payload_skips_airflow_logs() -> None:
    output = """
/path/to/airflow.py:22 RemovedInAirflow3Warning: warning text
[2026-04-28T18:04:31.209-0700] {plugins.py:37} INFO - setup plugin
[{"dag_id": "nba_analytics_pipeline", "is_paused": "True"}]
"""

    assert validate.extract_json_payload(output) == [
        {"dag_id": "nba_analytics_pipeline", "is_paused": "True"}
    ]


def test_prepare_environment_applies_pipeline_defaults(tmp_path: Path) -> None:
    (tmp_path / ".venv-airflow" / "bin").mkdir(parents=True)
    (tmp_path / ".env").write_text("GCP_PROJECT_ID=demo-project\n")

    env = validate.prepare_environment(tmp_path, base_env={"PATH": "/usr/bin"})

    assert env["GCP_PROJECT_ID"] == "demo-project"
    assert env["BQ_PROJECT"] == "demo-project"
    assert env["BQ_DATASET_BRONZE"] == "nba_bronze"
    assert env["BQ_DATASET_SILVER"] == "nba_silver"
    assert env["BQ_DATASET_GOLD"] == "nba_gold"
    assert env["BQ_DATASET_AGENT"] == "nba_agent"
    assert env["BQ_METADATA_DATASET"] == "nba_metadata"
    assert env["BQ_LOCATION"] == "US"
    assert env["NBA_BRONZE_BOOTSTRAP_MODE"] == "auto"
    assert env["AIRFLOW__CORE__DAGS_FOLDER"] == str(tmp_path / "dags")
    assert env["AIRFLOW__CORE__EXECUTOR"] == "SequentialExecutor"
    assert env["AIRFLOW__SCHEDULER__USE_JOB_SCHEDULE"] == "False"
    assert env["PATH"].startswith(str(tmp_path / ".venv-airflow" / "bin"))
    assert env["PYTHONUNBUFFERED"] == "1"
    assert env["OBJC_DISABLE_INITIALIZE_FORK_SAFETY"] == "YES"


def test_prepare_environment_does_not_override_existing_env(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "GCP_PROJECT_ID=env-file-project\nBQ_DATASET_GOLD=env_file_gold\n"
    )

    env = validate.prepare_environment(
        tmp_path,
        base_env={"GCP_PROJECT_ID": "shell-project", "BQ_DATASET_GOLD": "shell_gold"},
    )

    assert env["GCP_PROJECT_ID"] == "shell-project"
    assert env["BQ_PROJECT"] == "shell-project"
    assert env["BQ_DATASET_GOLD"] == "shell_gold"


def test_arg_parser_disables_redshift_by_default() -> None:
    parser = validate.build_arg_parser({"ENABLE_REDSHIFT": "true"})

    args = parser.parse_args([])

    assert args.enable_redshift is False


def test_arg_parser_can_enable_redshift_for_live_validation() -> None:
    parser = validate.build_arg_parser({})

    args = parser.parse_args(["--enable-redshift"])

    assert args.enable_redshift is True


def test_prepare_environment_allows_live_validation_executor_override(
    tmp_path: Path,
) -> None:
    env = validate.prepare_environment(
        tmp_path,
        base_env={"AIRFLOW_LIVE_VALIDATE_EXECUTOR": "SequentialExecutor"},
    )

    assert env["AIRFLOW__CORE__EXECUTOR"] == "SequentialExecutor"


def test_get_dag_pause_state_parses_airflow_string_bool() -> None:
    runner = FakeRunner('[{"dag_id": "nba_analytics_pipeline", "is_paused": "True"}]')

    assert validate.get_dag_pause_state(runner, ["airflow"]) is True
    assert runner.calls == [["airflow", "dags", "list", "-o", "json"]]


def test_active_dag_runs_filters_terminal_and_current_run() -> None:
    runs = [
        {"run_id": "old-queued", "state": "queued"},
        {"run_id": "old-running", "state": "running"},
        {"run_id": "current", "state": "running"},
        {"run_id": "done", "state": "success"},
        {"run_id": "failed", "state": "failed"},
    ]

    active = validate.active_dag_runs(runs, ignore_run_id="current")

    assert active == [
        {"run_id": "old-queued", "state": "queued"},
        {"run_id": "old-running", "state": "running"},
    ]


def test_markdown_report_includes_active_run_blocker() -> None:
    report = {
        "status": "failed",
        "dag_id": "nba_analytics_pipeline",
        "run_id": "manual__core_validate_20260429T010000Z",
        "error": "Existing queued/running DagRuns would block validation.",
        "preexisting_active_runs": [
            {"run_id": "manual__stale", "state": "running"},
        ],
    }

    markdown = validate.render_markdown_report(report)

    assert "Existing queued/running DagRuns" in markdown
    assert "`manual__stale`: `running`" in markdown


def test_redact_text_masks_obvious_secret_values() -> None:
    text = (
        'PASSWORD=abc123 token: xyz789 {"api_key": "abc123"} '
        "Authorization: Bearer bearer123 sk-testredactiontoken12345 normal=value"
    )

    redacted = validate.redact_text(text)

    assert "abc123" not in redacted
    assert "xyz789" not in redacted
    assert "bearer123" not in redacted
    assert "sk-testredactiontoken12345" not in redacted
    assert "normal=value" in redacted


def test_write_airflow_cli_wrapper_delegates_to_resolved_command(
    tmp_path: Path,
) -> None:
    wrapper_dir = validate.write_airflow_cli_wrapper(
        tmp_path, ["/tmp/venv/bin/python", "-m", "airflow"]
    )

    wrapper_path = wrapper_dir / "airflow"

    assert wrapper_path.exists()
    assert 'exec /tmp/venv/bin/python -m airflow "$@"' in wrapper_path.read_text()
    assert wrapper_path.stat().st_mode & 0o111


def test_write_exec_task_runner_module_forces_exec_path(tmp_path: Path) -> None:
    module_dir = validate.write_exec_task_runner_module(tmp_path)
    module_path = module_dir / "airflow_live_task_runner.py"

    content = module_path.read_text()

    assert "class ExecTaskRunner(StandardTaskRunner)" in content
    assert "self.process = self._start_by_exec()" in content
    assert "_start_by_fork" not in content
