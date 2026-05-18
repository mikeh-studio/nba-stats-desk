# Local Airflow

This repo supports a host-based Airflow workflow without Docker. The `Makefile`
standardizes the repo-local `AIRFLOW_HOME` path and points Airflow at `dags/`.
If `.env` exists, `make` exports those values into Airflow commands.

If `airflow` is not on the global `PATH`, the `Makefile` uses the repo-local
`.venv-airflow` Python and runs `python -m airflow`.

## Setup

Initialize the local metadata DB and registered DAGs:

```bash
make airflow-init
```

Create a local admin user:

```bash
make airflow-create-user
```

Start scheduler and webserver in separate terminals:

```bash
make airflow-scheduler
make airflow-webserver
```

Airflow UI: `http://localhost:8080`

The DAG is scheduled daily at `0 11 * * *` UTC. To allow scheduled runs, keep
the scheduler running and unpause the DAG:

```bash
make airflow-unpause
```

Trigger manually:

```bash
make airflow-trigger
```

The trigger target runs local initialization first so
`nba_analytics_pipeline` is registered before creating a run.

## Live Validation

Run a bounded scheduler-backed validation:

```bash
make airflow-live-validate
```

The harness starts a local scheduler, temporarily unpauses the DAG when needed,
triggers a unique manual run, waits for a terminal state, restores the previous
pause state, and stops the scheduler.

Reports are written under ignored local `reports/pipeline_triage/`.

The validation refuses to start if queued or running DagRuns already exist unless
`AIRFLOW_LIVE_VALIDATE_FAIL_ON_ACTIVE_RUNS=false` or
`--allow-existing-active-runs` is used.

## Replay and Backfill

Game-log extraction defaults to:

```env
NBA_GAME_LOG_EXTRACT_MODE=league
NBA_GAME_LOG_SEASON_TYPES=Regular Season,Playoffs
NBA_REPLAY_DAYS=3
```

For a full `2025-26` stats backfill without resetting metadata, run with a large
replay window:

```bash
NBA_REPLAY_DAYS=365 make airflow-trigger
```

Return `NBA_REPLAY_DAYS` to the normal daily value after the replay.

## NBA API Retry Controls

Use these variables for bounded endpoint behavior:

```env
NBA_API_TIMEOUT_SECONDS=15
NBA_API_RETRIES=3
NBA_API_RETRY_BASE_DELAY_SECONDS=1.0
NBA_API_RETRY_BACKOFF_MULTIPLIER=2.0
NBA_API_RETRY_MAX_DELAY_SECONDS=8.0
```

The older per-player game-log endpoint remains available with:

```env
NBA_GAME_LOG_EXTRACT_MODE=players
```

## Injury Reports

Official injury-report ingestion is bounded by:

```env
NBA_ENABLE_INJURY_REPORTS=true
NBA_INJURY_REPORT_START_DATE=2026-04-18
NBA_INJURY_REPORT_MAX_REPORTS=21
NBA_INJURY_REPORT_REPLAY_DAYS=2
NBA_INJURY_REPORT_TIMES_ET=05_00PM
```

The injury report pipeline normalizes tokenized PDF output, resolves player IDs
through NBA static lookup names, and writes availability models through dbt.

When injury reports are the only changed domain, Airflow can run a targeted dbt
build for injury availability models instead of rebuilding the full warehouse.

For a targeted season-wide injury report backfill without rerunning unrelated
extracts, use:

```bash
python -m dotenv run --no-override -- .venv-airflow/bin/python scripts/backfill_injury_reports.py \
  --start-date 2025-10-21 \
  --end-date 2026-05-13
```

The utility derives project, bucket, and dataset settings from `.env`, fetches
one official `05_00PM` report per day by default, loads bronze staging, runs the
injury DQ checks, merges `bronze.raw_player_injury_reports`, updates the injury
watermark, and runs the targeted dbt injury availability build.
Official CDN `403` and `404` responses are treated as missing reports so long
season windows can skip dates where no archived PDF is available.

## Bronze Bootstrap

`NBA_BRONZE_BOOTSTRAP_MODE` controls auxiliary bronze bootstrap behavior:

- `auto`: derive missing or empty auxiliary bronze tables from `raw_game_logs`.
- `off`: disable derived bronze bootstrap.
- `force`: re-merge derived auxiliary bronze rows even when tables already have rows.

## Useful Commands

```bash
make airflow-list
make airflow-parse
make airflow-pause
make airflow-unpause
make airflow-live-validate
```
