"""Local Airflow runtime tweaks for the NBA_GCP host scheduler."""

from __future__ import annotations

import os
from pathlib import Path

from airflow.executors.base_executor import BaseExecutor
from airflow.models.taskinstance import TaskInstance

REPO_ROOT = Path(__file__).resolve().parents[2]
VENV_BIN = REPO_ROOT / ".venv-airflow" / "bin"
AIRFLOW_BIN = VENV_BIN / "airflow"

path_parts = os.environ.get("PATH", "").split(os.pathsep)
venv_bin = str(VENV_BIN)
if venv_bin not in path_parts:
    os.environ["PATH"] = os.pathsep.join([venv_bin, *path_parts])

_original_generate_command = TaskInstance.generate_command
_original_validate_command = BaseExecutor.validate_airflow_tasks_run_command


def _generate_command_with_absolute_airflow(*args, **kwargs):
    command = _original_generate_command(*args, **kwargs)
    if command and command[0] == "airflow":
        command[0] = str(AIRFLOW_BIN)
    return command


TaskInstance.generate_command = staticmethod(_generate_command_with_absolute_airflow)


def _validate_absolute_airflow_task_command(command):
    if (
        len(command) >= 3
        and command[0] in {"airflow", str(AIRFLOW_BIN)}
        and command[1:3] == ["tasks", "run"]
    ):
        return
    return _original_validate_command(command)


BaseExecutor.validate_airflow_tasks_run_command = staticmethod(
    _validate_absolute_airflow_task_command
)
