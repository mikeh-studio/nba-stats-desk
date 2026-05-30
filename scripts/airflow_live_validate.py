#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

DAG_ID = "nba_analytics_pipeline"

BRONZE_CONTRACT_TABLES = [
    "raw_game_logs",
    "raw_game_line_scores",
    "raw_player_reference",
    "raw_schedule",
]

GOLD_CONTRACT_TABLES = [
    "dim_player",
    "dim_team",
    "dim_game",
    "fct_player_game_stats",
    "fct_team_game_scores",
    "fct_player_scoring_contribution",
    "player_recent_form",
    "player_similarity_feature_input",
]

AGENT_CONTRACT_TABLES = [
    "agent_player_search",
]

DBT_CONTRACT_SELECTOR = [
    "dim_player",
    "dim_team",
    "dim_game",
    "fct_player_game_stats",
    "fct_team_game_scores",
    "fct_player_scoring_contribution",
    "player_recent_form",
    "player_similarity_feature_input",
    "agent_player_search",
]

ACTIVE_RUN_STATES = {"queued", "running"}
TERMINAL_RUN_STATES = {"success", "failed"}

SECRET_KEY_PATTERN = (
    r"password|passwd|pwd|token|secret|api[_-]?key|access[_-]?key|"
    r"secret[_-]?key|private[_-]?key|client[_-]?secret|credential"
)
SECRET_ASSIGNMENT_PATTERN = re.compile(
    rf"(?i)\b({SECRET_KEY_PATTERN})(\s*[=:]\s*)[^\s,;]+"
)
SECRET_JSON_PATTERN = re.compile(
    rf"(?i)([\"'](?:{SECRET_KEY_PATTERN})[\"']\s*:\s*)[\"'][^\"']+[\"']"
)
SECRET_AUTH_PATTERN = re.compile(
    r"(?i)\b(authorization:\s*bearer\s+)[A-Za-z0-9._~+/=-]+"
)
SECRET_SK_PATTERN = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b")


@dataclass
class CommandResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str
    elapsed_seconds: float

    @property
    def combined_output(self) -> str:
        return "\n".join(part for part in [self.stdout, self.stderr] if part)


class CommandExecutionError(RuntimeError):
    def __init__(self, result: CommandResult):
        self.result = result
        super().__init__(
            f"Command failed with exit code {result.returncode}: "
            f"{format_command(result.args)}"
        )


class ValidationError(RuntimeError):
    pass


class CommandRunner:
    def __init__(self, *, cwd: Path, env: Mapping[str, str], report: dict[str, Any]):
        self.cwd = cwd
        self.env = dict(env)
        self.report = report

    def run(
        self,
        args: list[str],
        *,
        check: bool = True,
        timeout_seconds: int | None = None,
    ) -> CommandResult:
        started = time.monotonic()
        print(f"$ {format_command(args)}", flush=True)
        completed = subprocess.run(
            args,
            cwd=self.cwd,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
            check=False,
        )
        result = CommandResult(
            args=args,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            elapsed_seconds=round(time.monotonic() - started, 3),
        )
        self.report.setdefault("commands", []).append(command_summary(result))
        if check and result.returncode != 0:
            raise CommandExecutionError(result)
        return result


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def format_command(args: list[str]) -> str:
    return " ".join(shlex.quote(arg) for arg in args)


def redact_text(text: str, *, max_chars: int | None = None) -> str:
    redacted = SECRET_JSON_PATTERN.sub(
        lambda match: f'{match.group(1)}"<redacted>"', text
    )
    redacted = SECRET_ASSIGNMENT_PATTERN.sub(r"\1\2<redacted>", redacted)
    redacted = SECRET_AUTH_PATTERN.sub(r"\1<redacted>", redacted)
    redacted = SECRET_SK_PATTERN.sub("sk-<redacted>", redacted)
    if max_chars is not None and len(redacted) > max_chars:
        return redacted[-max_chars:]
    return redacted


def command_summary(result: CommandResult) -> dict[str, Any]:
    return {
        "command": format_command(result.args),
        "returncode": result.returncode,
        "elapsed_seconds": result.elapsed_seconds,
        "stdout_tail": redact_text(result.stdout, max_chars=3000),
        "stderr_tail": redact_text(result.stderr, max_chars=3000),
    }


def load_dotenv_values(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}

    try:
        from dotenv import dotenv_values

        return {
            key: value
            for key, value in dotenv_values(path).items()
            if key and value is not None
        }
    except Exception:
        values: dict[str, str] = {}
        for raw_line in path.read_text().splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("'\"")
        return values


def prepare_environment(
    root: Path,
    *,
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    env = dict(base_env if base_env is not None else os.environ)
    for key, value in load_dotenv_values(root / ".env").items():
        env.setdefault(key, value)

    if env.get("GCP_PROJECT_ID"):
        env.setdefault("BQ_PROJECT", env["GCP_PROJECT_ID"])
    env.setdefault("BQ_DATASET_BRONZE", "nba_bronze")
    env.setdefault("BQ_DATASET_SILVER", "nba_silver")
    env.setdefault("BQ_DATASET_GOLD", "nba_gold")
    env.setdefault("BQ_DATASET_AGENT", "nba_agent")
    env.setdefault("BQ_METADATA_DATASET", "nba_metadata")
    env.setdefault("BQ_LOCATION", "US")
    env.setdefault("DBT_TARGET", "dev")
    env.setdefault("NBA_BRONZE_BOOTSTRAP_MODE", "auto")
    env.setdefault("AIRFLOW_HOME", str(root / "airflow_home"))
    env.setdefault("AIRFLOW__CORE__DAGS_FOLDER", str(root / "dags"))
    env.setdefault("AIRFLOW__CORE__LOAD_EXAMPLES", "False")
    env.setdefault(
        "AIRFLOW__CORE__EXECUTOR",
        env.get("AIRFLOW_LIVE_VALIDATE_EXECUTOR", "SequentialExecutor"),
    )
    env.setdefault("AIRFLOW__SCHEDULER__USE_JOB_SCHEDULE", "False")
    airflow_bin = root / ".venv-airflow" / "bin"
    if airflow_bin.exists():
        env["PATH"] = f"{airflow_bin}{os.pathsep}{env.get('PATH', '')}"
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.setdefault("OBJC_DISABLE_INITIALIZE_FORK_SAFETY", "YES")
    return env


def parse_bool(value: str | bool | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = value.strip().lower()
    if normalized in {"1", "true", "t", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "f", "no", "n", "off"}:
        return False
    return default


def env_int(env: Mapping[str, str], key: str, default: int) -> int:
    raw_value = env.get(key)
    if raw_value is None:
        return default
    try:
        return int(raw_value)
    except ValueError:
        return default


def resolve_airflow_cmd(root: Path) -> list[str]:
    candidates = sorted((root / ".venv-airflow" / "bin").glob("python3*"))
    if candidates:
        return [str(candidates[0]), "-m", "airflow"]
    airflow = shutil.which("airflow")
    if airflow:
        return [airflow]
    raise ValidationError(
        "Could not find Airflow. Install dependencies or create .venv-airflow."
    )


def resolve_dbt_cmd(root: Path) -> list[str]:
    local_dbt = root / ".venv-airflow" / "bin" / "dbt"
    if local_dbt.exists():
        return [str(local_dbt)]
    dbt = shutil.which("dbt")
    if dbt:
        return [dbt]
    raise ValidationError("Could not find dbt on PATH or in .venv-airflow.")


def write_airflow_cli_wrapper(root: Path, airflow_cmd: list[str]) -> Path:
    wrapper_dir = root / "reports" / "pipeline_triage" / "bin"
    wrapper_dir.mkdir(parents=True, exist_ok=True)
    wrapper_path = wrapper_dir / "airflow"
    wrapper_path.write_text(f'#!/bin/sh\nexec {format_command(airflow_cmd)} "$@"\n')
    wrapper_path.chmod(0o755)
    return wrapper_dir


def write_exec_task_runner_module(root: Path) -> Path:
    module_dir = root / "reports" / "pipeline_triage" / "python"
    module_dir.mkdir(parents=True, exist_ok=True)
    module_path = module_dir / "airflow_live_task_runner.py"
    module_path.write_text(
        "from __future__ import annotations\n\n"
        "import threading\n\n"
        "from airflow.task.task_runner.standard_task_runner import StandardTaskRunner\n\n\n"
        "class ExecTaskRunner(StandardTaskRunner):\n"
        "    def start(self):\n"
        "        self.process = self._start_by_exec()\n"
        "        if self.process:\n"
        "            resource_monitor = threading.Thread(\n"
        "                target=self._read_task_utilization\n"
        "            )\n"
        "            resource_monitor.daemon = True\n"
        "            resource_monitor.start()\n"
    )
    return module_dir


def extract_json_payload(text: str) -> Any:
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char not in "[{":
            continue
        try:
            payload, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        return payload
    raise ValueError("No JSON payload found in command output.")


def list_dags(runner: CommandRunner, airflow_cmd: list[str]) -> list[dict[str, Any]]:
    result = runner.run([*airflow_cmd, "dags", "list", "-o", "json"])
    payload = extract_json_payload(result.combined_output)
    if not isinstance(payload, list):
        raise ValidationError("Airflow DAG list output was not a JSON list.")
    return payload


def get_dag_pause_state(
    runner: CommandRunner, airflow_cmd: list[str], dag_id: str = DAG_ID
) -> bool:
    for row in list_dags(runner, airflow_cmd):
        if row.get("dag_id") == dag_id:
            return parse_bool(row.get("is_paused"), default=False)
    raise ValidationError(f"Airflow DAG not found: {dag_id}")


def list_dag_runs(
    runner: CommandRunner, airflow_cmd: list[str], dag_id: str = DAG_ID
) -> list[dict[str, Any]]:
    result = runner.run([*airflow_cmd, "dags", "list-runs", "-d", dag_id, "-o", "json"])
    payload = extract_json_payload(result.combined_output)
    if not isinstance(payload, list):
        raise ValidationError("Airflow DAG run list output was not a JSON list.")
    return payload


def active_dag_runs(
    runs: list[dict[str, Any]], *, ignore_run_id: str | None = None
) -> list[dict[str, Any]]:
    return [
        row
        for row in runs
        if row.get("run_id") != ignore_run_id and row.get("state") in ACTIVE_RUN_STATES
    ]


def task_states_for_run(
    runner: CommandRunner,
    airflow_cmd: list[str],
    run_id: str,
    dag_id: str = DAG_ID,
) -> list[dict[str, Any]]:
    result = runner.run(
        [*airflow_cmd, "tasks", "states-for-dag-run", dag_id, run_id, "-o", "json"]
    )
    payload = extract_json_payload(result.combined_output)
    if not isinstance(payload, list):
        raise ValidationError("Airflow task state output was not a JSON list.")
    return payload


def find_run_state(runs: list[dict[str, Any]], run_id: str) -> str | None:
    for row in runs:
        if row.get("run_id") == run_id:
            state = row.get("state")
            return str(state) if state is not None else None
    return None


def require_no_active_runs(
    runner: CommandRunner,
    airflow_cmd: list[str],
    report: dict[str, Any],
    dag_id: str = DAG_ID,
) -> None:
    runs = list_dag_runs(runner, airflow_cmd, dag_id)
    active_runs = active_dag_runs(runs)
    report["preexisting_active_runs"] = active_runs
    if active_runs:
        rendered = ", ".join(
            f"{row.get('run_id')} ({row.get('state')})" for row in active_runs
        )
        raise ValidationError(
            "Existing queued/running DagRuns would block a new scheduler-backed "
            f"validation run: {rendered}"
        )


def start_scheduler(
    airflow_cmd: list[str],
    *,
    root: Path,
    env: Mapping[str, str],
    report: dict[str, Any],
) -> tuple[subprocess.Popen, Path]:
    log_dir = root / "reports" / "pipeline_triage"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"airflow_scheduler_{utc_timestamp_for_path()}.log"
    log_handle = log_path.open("w")
    print(f"$ {format_command([*airflow_cmd, 'scheduler'])}", flush=True)
    try:
        process = subprocess.Popen(
            [*airflow_cmd, "scheduler"],
            cwd=root,
            env=dict(env),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
    finally:
        log_handle.close()
    report["scheduler"] = {
        "command": format_command([*airflow_cmd, "scheduler"]),
        "pid": process.pid,
        "log_path": str(log_path),
    }
    return process, log_path


def stop_scheduler(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=20)
    except Exception:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except Exception:
            process.kill()
        process.wait(timeout=20)


def wait_for_scheduler_start(
    process: subprocess.Popen, *, startup_seconds: int
) -> None:
    deadline = time.monotonic() + startup_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise ValidationError(
                f"Airflow scheduler exited early with code {process.returncode}."
            )
        time.sleep(1)


def trigger_dag_run(
    runner: CommandRunner,
    airflow_cmd: list[str],
    *,
    run_id: str,
    dag_id: str = DAG_ID,
) -> None:
    runner.run(
        [*airflow_cmd, "dags", "trigger", dag_id, "--run-id", run_id, "-o", "json"]
    )


def set_dag_pause_state(
    runner: CommandRunner,
    airflow_cmd: list[str],
    *,
    paused: bool,
    dag_id: str = DAG_ID,
) -> None:
    command = "pause" if paused else "unpause"
    runner.run([*airflow_cmd, "dags", command, "--yes", "-o", "json", dag_id])


def monitor_dag_run(
    runner: CommandRunner,
    airflow_cmd: list[str],
    *,
    scheduler_process: subprocess.Popen,
    run_id: str,
    timeout_seconds: int,
    poll_seconds: int,
    dag_id: str = DAG_ID,
) -> tuple[str, list[dict[str, Any]]]:
    deadline = time.monotonic() + timeout_seconds
    last_state: str | None = None
    tasks: list[dict[str, Any]] = []

    while True:
        if scheduler_process.poll() is not None:
            raise ValidationError(
                "Airflow scheduler exited before the DAG reached a terminal state "
                f"(exit code {scheduler_process.returncode})."
            )

        runs = list_dag_runs(runner, airflow_cmd, dag_id)
        state = find_run_state(runs, run_id)
        if state != last_state:
            print(f"DAG run {run_id} state: {state or 'not-found'}", flush=True)
            last_state = state

        if state in TERMINAL_RUN_STATES:
            tasks = task_states_for_run(runner, airflow_cmd, run_id, dag_id)
            return state, tasks

        if time.monotonic() >= deadline:
            tasks = task_states_for_run(runner, airflow_cmd, run_id, dag_id)
            raise TimeoutError(
                f"Timed out after {timeout_seconds}s waiting for DAG run {run_id}."
            )

        time.sleep(poll_seconds)


def query_bigquery_contract(env: Mapping[str, str]) -> dict[str, list[dict[str, Any]]]:
    project_id = env.get("BQ_PROJECT") or env.get("GCP_PROJECT_ID")
    if not project_id:
        raise ValidationError(
            "BQ_PROJECT or GCP_PROJECT_ID is required for BigQuery checks."
        )

    try:
        from google.cloud import bigquery
        from google.cloud.exceptions import NotFound
    except Exception as exc:
        raise ValidationError(f"Could not import BigQuery client: {exc}") from exc

    client = bigquery.Client(
        project=project_id, location=env.get("BQ_LOCATION") or None
    )
    bronze_dataset = env.get("BQ_DATASET_BRONZE", "nba_bronze")
    gold_dataset = env.get("BQ_DATASET_GOLD", "nba_gold")
    agent_dataset = env.get("BQ_DATASET_AGENT", "nba_agent")

    def count_table(dataset: str, table: str) -> dict[str, Any]:
        table_id = f"{project_id}.{dataset}.{table}"
        try:
            client.get_table(table_id)
        except NotFound:
            return {"table": table_id, "exists": False, "row_count": None}

        query = f"select count(*) as row_count from `{table_id}`"
        row = next(iter(client.query(query).result()))
        return {"table": table_id, "exists": True, "row_count": int(row["row_count"])}

    return {
        "bronze": [
            count_table(bronze_dataset, table) for table in BRONZE_CONTRACT_TABLES
        ],
        "gold": [count_table(gold_dataset, table) for table in GOLD_CONTRACT_TABLES],
        "agent": [count_table(agent_dataset, table) for table in AGENT_CONTRACT_TABLES],
    }


def run_dbt_contract_build(
    runner: CommandRunner,
    dbt_cmd: list[str],
    *,
    target: str,
) -> CommandResult:
    return runner.run(
        [
            *dbt_cmd,
            "build",
            "--project-dir",
            ".",
            "--profiles-dir",
            "dbt/profiles",
            "--target",
            target,
            "--select",
            *DBT_CONTRACT_SELECTOR,
        ],
        timeout_seconds=None,
    )


def utc_timestamp_for_path() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def utc_timestamp_for_run_id() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def report_paths(root: Path, timestamp: str) -> tuple[Path, Path]:
    report_dir = root / "reports" / "pipeline_triage"
    report_dir.mkdir(parents=True, exist_ok=True)
    return (
        report_dir / f"live_validation_{timestamp}.json",
        report_dir / f"live_validation_{timestamp}.md",
    )


def write_report(root: Path, report: dict[str, Any]) -> tuple[Path, Path]:
    timestamp = report.setdefault("report_timestamp", utc_timestamp_for_path())
    json_path, md_path = report_paths(root, timestamp)
    json_path.write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str) + "\n"
    )
    md_path.write_text(render_markdown_report(report))
    return json_path, md_path


def render_markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# Live Airflow Validation",
        "",
        f"- Status: `{report.get('status', 'unknown')}`",
        f"- DAG: `{report.get('dag_id', DAG_ID)}`",
        f"- Run id: `{report.get('run_id', '')}`",
        f"- Started: `{report.get('started_at', '')}`",
        f"- Completed: `{report.get('completed_at', '')}`",
    ]
    if report.get("error"):
        lines.extend(["", "## Error", "", redact_text(str(report["error"]))])

    if report.get("preexisting_active_runs"):
        lines.extend(["", "## Preexisting Active Runs", ""])
        for row in report["preexisting_active_runs"]:
            lines.append(f"- `{row.get('run_id')}`: `{row.get('state')}`")

    contract = report.get("bigquery_contract")
    if contract:
        lines.extend(["", "## BigQuery Contract", ""])
        for layer in ["bronze", "gold"]:
            rows = contract.get(layer, [])
            if not rows:
                continue
            lines.append(f"### {layer.title()}")
            for row in rows:
                count = row.get("row_count")
                exists = row.get("exists")
                lines.append(
                    f"- `{row.get('table')}`: exists=`{exists}`, row_count=`{count}`"
                )

    if report.get("dag_state"):
        lines.extend(["", "## DAG Result", "", f"- State: `{report['dag_state']}`"])
    if report.get("failed_tasks"):
        lines.extend(["", "## Failed Tasks", ""])
        for task_id in report["failed_tasks"]:
            lines.append(f"- `{task_id}`")
    if report.get("scheduler", {}).get("log_path"):
        lines.extend(
            ["", "## Scheduler", "", f"- Log: `{report['scheduler']['log_path']}`"]
        )
    lines.append("")
    return "\n".join(lines)


def failed_task_ids(tasks: list[dict[str, Any]]) -> list[str]:
    return [
        str(row.get("task_id"))
        for row in tasks
        if row.get("state") in {"failed", "upstream_failed"}
    ]


def run_validation(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    root = repo_root()
    env = prepare_environment(root)
    env["ENABLE_REDSHIFT"] = "true" if args.enable_redshift else "false"
    task_runner_module_dir = write_exec_task_runner_module(root)
    env["PYTHONPATH"] = (
        f"{task_runner_module_dir}{os.pathsep}{env.get('PYTHONPATH', '')}"
    )
    env.setdefault(
        "AIRFLOW__CORE__TASK_RUNNER",
        "airflow_live_task_runner.ExecTaskRunner",
    )
    report: dict[str, Any] = {
        "status": "running",
        "dag_id": DAG_ID,
        "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "commands": [],
        "config": {
            "timeout_seconds": args.timeout_seconds,
            "poll_seconds": args.poll_seconds,
            "run_dbt": args.run_dbt,
            "fail_on_active_runs": args.fail_on_active_runs,
            "airflow_home": env["AIRFLOW_HOME"],
            "executor": env["AIRFLOW__CORE__EXECUTOR"],
            "enable_redshift": env["ENABLE_REDSHIFT"],
            "task_runner": env["AIRFLOW__CORE__TASK_RUNNER"],
            "use_job_schedule": env["AIRFLOW__SCHEDULER__USE_JOB_SCHEDULE"],
            "objc_fork_safety_disabled": env["OBJC_DISABLE_INITIALIZE_FORK_SAFETY"],
        },
    }
    runner = CommandRunner(cwd=root, env=env, report=report)
    airflow_cmd: list[str] = []
    dbt_cmd: list[str] = []
    scheduler_process: subprocess.Popen | None = None
    initial_pause_state: bool | None = None
    run_id = f"manual__core_validate_{utc_timestamp_for_run_id()}"
    report["run_id"] = run_id

    try:
        airflow_cmd = resolve_airflow_cmd(root)
        dbt_cmd = resolve_dbt_cmd(root) if args.run_dbt else []
        airflow_wrapper_dir = write_airflow_cli_wrapper(root, airflow_cmd)
        env["PATH"] = f"{airflow_wrapper_dir}{os.pathsep}{env.get('PATH', '')}"
        runner.env = dict(env)
        report["config"]["airflow_wrapper_dir"] = str(airflow_wrapper_dir)
        Path(env["AIRFLOW_HOME"]).mkdir(parents=True, exist_ok=True)
        runner.run([*airflow_cmd, "db", "migrate"])
        runner.run([*airflow_cmd, "dags", "reserialize"])
        runner.run([*airflow_cmd, "tasks", "list", DAG_ID])

        initial_pause_state = get_dag_pause_state(runner, airflow_cmd)
        report["initial_dag_paused"] = initial_pause_state
        if args.fail_on_active_runs:
            require_no_active_runs(runner, airflow_cmd, report)

        scheduler_process, _ = start_scheduler(
            airflow_cmd, root=root, env=env, report=report
        )
        wait_for_scheduler_start(
            scheduler_process, startup_seconds=args.scheduler_startup_seconds
        )

        if initial_pause_state:
            set_dag_pause_state(runner, airflow_cmd, paused=False)
        trigger_dag_run(runner, airflow_cmd, run_id=run_id)
        dag_state, tasks = monitor_dag_run(
            runner,
            airflow_cmd,
            scheduler_process=scheduler_process,
            run_id=run_id,
            timeout_seconds=args.timeout_seconds,
            poll_seconds=args.poll_seconds,
        )
        report["dag_state"] = dag_state
        report["task_states"] = tasks
        report["failed_tasks"] = failed_task_ids(tasks)

        report["bigquery_contract"] = query_bigquery_contract(env)

        if dag_state != "success":
            report["status"] = "failed"
            report["error"] = f"Airflow DAG run finished with state {dag_state}."
            return 1, report

        if args.run_dbt:
            run_dbt_contract_build(runner, dbt_cmd, target=env["DBT_TARGET"])
            report["dbt_contract_build"] = {
                "status": "success",
                "selector": DBT_CONTRACT_SELECTOR,
            }
            report["bigquery_contract_after_dbt"] = query_bigquery_contract(env)

        if dag_state == "success":
            report["status"] = "success"
            return 0, report
        return 1, report
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = str(exc)
        if isinstance(exc, CommandExecutionError):
            report["failed_command"] = command_summary(exc.result)
        return 1, report
    finally:
        if initial_pause_state is True and airflow_cmd:
            try:
                set_dag_pause_state(runner, airflow_cmd, paused=True)
                report["restored_dag_paused"] = True
            except Exception as exc:
                report["pause_restore_error"] = str(exc)
        if scheduler_process is not None:
            stop_scheduler(scheduler_process)
            report["scheduler_stopped"] = True
        report["completed_at"] = dt.datetime.now(dt.timezone.utc).isoformat()


def build_arg_parser(env: Mapping[str, str]) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a bounded scheduler-backed live validation for the NBA DAG."
    )
    parser.set_defaults(
        run_dbt=parse_bool(env.get("AIRFLOW_LIVE_VALIDATE_RUN_DBT"), default=True),
        fail_on_active_runs=parse_bool(
            env.get("AIRFLOW_LIVE_VALIDATE_FAIL_ON_ACTIVE_RUNS"), default=True
        ),
        enable_redshift=parse_bool(
            env.get("AIRFLOW_LIVE_VALIDATE_ENABLE_REDSHIFT"), default=False
        ),
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=env_int(env, "AIRFLOW_LIVE_VALIDATE_TIMEOUT_SECONDS", 7200),
        help="Maximum seconds to wait for the triggered DAG run.",
    )
    parser.add_argument(
        "--poll-seconds",
        type=int,
        default=env_int(env, "AIRFLOW_LIVE_VALIDATE_POLL_SECONDS", 30),
        help="Seconds between Airflow DAG state polls.",
    )
    parser.add_argument(
        "--scheduler-startup-seconds",
        type=int,
        default=env_int(env, "AIRFLOW_LIVE_VALIDATE_SCHEDULER_STARTUP_SECONDS", 10),
        help="Seconds to wait after starting the local scheduler.",
    )
    parser.add_argument(
        "--skip-dbt",
        dest="run_dbt",
        action="store_false",
        help="Skip the targeted dbt contract build after the DAG run.",
    )
    parser.add_argument(
        "--run-dbt",
        dest="run_dbt",
        action="store_true",
        help="Run the targeted dbt contract build after the DAG run.",
    )
    parser.add_argument(
        "--allow-existing-active-runs",
        dest="fail_on_active_runs",
        action="store_false",
        help="Do not fail preflight when queued/running DagRuns already exist.",
    )
    parser.add_argument(
        "--fail-on-active-runs",
        dest="fail_on_active_runs",
        action="store_true",
        help="Fail preflight when queued/running DagRuns already exist.",
    )
    parser.add_argument(
        "--enable-redshift",
        dest="enable_redshift",
        action="store_true",
        help="Include optional Redshift tasks in the live validation run.",
    )
    parser.add_argument(
        "--disable-redshift",
        dest="enable_redshift",
        action="store_false",
        help="Skip optional Redshift tasks in the live validation run.",
    )
    return parser


def main() -> int:
    root = repo_root()
    env = prepare_environment(root)
    parser = build_arg_parser(env)
    args = parser.parse_args()
    exit_code, report = run_validation(args)
    json_path, md_path = write_report(root, report)
    print(f"Live validation report: {json_path}")
    print(f"Live validation summary: {md_path}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
