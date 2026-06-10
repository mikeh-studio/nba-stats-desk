#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

mkdir -p logs

export AIRFLOW_HOME="${AIRFLOW_HOME:-${REPO_ROOT}/airflow_home}"
export AIRFLOW__CORE__DAGS_FOLDER="${AIRFLOW__CORE__DAGS_FOLDER:-${REPO_ROOT}/dags}"
export AIRFLOW__CORE__LOAD_EXAMPLES="${AIRFLOW__CORE__LOAD_EXAMPLES:-False}"
export PATH="${REPO_ROOT}/.venv-airflow/bin:${PATH}"

exec make airflow-scheduler
