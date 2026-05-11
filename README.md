# NBA Data Platform

Production-style NBA data pipeline and public NBA stats workbench for the `2025-26` season only. Built on **GCP** (BigQuery, GCS, Cloud Run) with an optional **AWS** secondary warehouse (Redshift Serverless, S3).

The v1 target architecture is:

`NBA API + official NBA injury reports -> GCS landing -> BigQuery bronze -> dbt silver/gold -> player similarity + archetypes -> deterministic analysis snapshots -> Cloud Run site/API`

With optional Redshift sync:

`BigQuery bronze -> GCS Parquet -> S3 -> Redshift COPY -> dbt Redshift models`

Core decisions for this version:

- BigQuery (GCP) is the warehouse system of record.
- Redshift Serverless (AWS) is an optional secondary warehouse for cross-cloud portfolio/learning.
- dbt remains the bronze/silver/gold transformation layer, with cross-database macros for BigQuery/Redshift compatibility.
- Self-hosted Airflow is the supported orchestration path.
- Cloud Run is the public read-only website/API target.
- Infrastructure is managed by Terraform — GCP core infra and AWS Redshift infra (`infra/terraform-aws/`).
- GitHub Actions CI validates `pytest`, `dbt parse`, and Terraform on pull requests and pushes to `main`.
- The operational scope is fixed to season `2025-26`.
- Claude/Anthropic is not part of the v1 runtime path.
- Analysis output is deterministic and template-based, not LLM-generated.
- Supporting context now includes upcoming schedule, team line scores, player reference attributes, and official NBA injury report snapshots. Media sentiment ingestion is intentionally deferred until source, cost, and retention rules are explicit.

## Pipeline Flow

The Airflow DAG in `dags/nba_analytics_dag.py` runs this path:

1. Extract active-player game logs from `nba_api` for season `2025-26`.
2. Apply incremental filtering using a persisted watermark plus replay buffer.
3. Derive the replay-window `game_id` set and fetch team line scores for those games.
4. Fetch active-player reference attributes and roster context.
5. Fetch the upcoming schedule window from `nba_api`.
6. Fetch a capped set of official NBA injury report PDFs, defaulting to the 2026 playoff window on first run and a small replay window after watermarking.
7. Validate source contracts for all non-empty extracted domains before landing.
8. Land all five domains in GCS and load them into bronze staging tables.
9. Run DQ checks for game logs, line scores, player reference, schedule context, and injury reports.
10. Merge into `bronze.raw_game_logs`, `bronze.raw_game_line_scores`, `bronze.raw_player_reference`, `bronze.raw_schedule`, and `bronze.raw_player_injury_reports`, then validate merge reconciliation against loaded and inserted/updated counts.
11. Run dbt bronze/silver/gold models and tests for the public stats-serving layer.
12. Build player similarity vectors and archetype clusters from `gold.player_similarity_feature_input`, then publish `gold.player_similarity_features` and `gold.player_archetypes`.
13. Build a deterministic `analysis_snapshots` record from leaderboard, trend, ranking, recommendation, scoring-contribution, and player-context outputs.
14. Publish watermark and run metadata to `nba_metadata`.

## Source Contracts

Source contracts live under `contracts/` and are enforced in the Airflow extract
tasks before non-empty dataframes are uploaded to GCS. They define required
columns, expected types, business keys, season/date scope, enum values, and
domain-specific bounds for:

- `game_logs`
- `game_line_scores`
- `schedule`
- `player_reference`
- `injury_reports`

Contract severities:

- `fatal`: stop the pipeline before landing the batch.
- `quarantine`: remove the violating rows and continue if valid rows remain.
- `warning`: record the issue without dropping rows.

The source contract layer protects the ingestion boundary. The existing BigQuery
staging DQ checks and dbt tests still run after landing to protect warehouse
state and modeled outputs.

Each non-empty extract also writes a pre-validation source audit snapshot to GCS
under `nba_data/<season>/source_audit/raw_extract/source=<domain>/run_id=<run>/`.
Rows removed by `quarantine` rules are written under
`nba_data/<season>/source_audit/quarantine/source=<domain>/run_id=<run>/`.
Contract outcomes are upserted to `nba_metadata.source_contract_results` with
row counts, failure counts, GCS audit URIs, landing URI, and serialized violation
details.

## Optional Redshift Secondary Warehouse

The pipeline optionally syncs bronze tables to AWS Redshift Serverless as a secondary warehouse (cross-cloud learning/portfolio feature). Set `ENABLE_REDSHIFT=true` to enable — the DAG appends a Redshift sync task after the BigQuery bronze merge.

Data flows from BigQuery bronze as Parquet through GCS to S3, then loads into Redshift via `COPY` with automatic schema alignment. dbt models run against Redshift using cross-database compatibility macros. AWS infrastructure is managed by Terraform in `infra/terraform-aws/`. See `.env.example` for Redshift-related variables. The `dbt-redshift` adapter is required for local Redshift validation.

## Warehouse Layout

- `bronze.raw_game_logs`: replay-safe raw source table
- `bronze.raw_game_line_scores`: team final score and quarter/OT line score by game
- `bronze.raw_player_reference`: stable player profile and roster attributes
- `bronze.raw_schedule`: upcoming schedule context by team
- `silver.stg_game_logs_clean`: season-scoped cleaned source model
- `silver.stg_game_line_scores_clean`: cleaned team line scores
- `silver.stg_player_reference_clean`: cleaned player profile and roster context
- `silver.int_player_game_enriched`: matchup and team enrichment
- `silver.stg_schedule_clean`: cleaned schedule context
- `gold.fct_player_game_stats`: fact table for player game stats
- `gold.fct_team_game_scores`: team score, quarter totals, margin, and opponent context
- `gold.fct_player_scoring_contribution`: player points as a share of team and game scoring
- `gold.dim_player`: player dimension
- `gold.player_trends`: recent-vs-prior player trend model
- `gold.player_recent_form`: rolling recent form and box-score-derived proxy output
- `gold.player_category_profile`: category-score profile for the ranking surface
- `gold.player_opportunity_outlook`: schedule-only opportunity context
- `gold.player_fantasy_rankings`: deterministic ranking surface
- `gold.player_similarity_feature_input`: clustering feature table built from season-to-date and recent-form stat shape
- `gold.player_similarity_features`: normalized similarity vectors plus per-player summary traits
- `gold.player_archetypes`: batch-assigned archetype labels and confidence by player
- `gold.workbench_compare`: fixed-window compare input model for bounded compare windows
- `gold.workbench_dashboard`: dashboard-oriented player read model with bounded reason fields
- `gold.workbench_home_dashboard`: seven-day dashboard snapshot model keyed by `as_of_date`
- `gold.workbench_player_detail`: player-detail read model built from dashboard + compare windows
- `gold.fantasy_insights`: structured recommendation cards
- `gold.daily_leaderboard`: daily leaderboard output
- `gold.analysis_snapshots`: deterministic narrative snapshot output written by the DAG

dbt is intentionally centered on `2025-26` only. The silver layer filters to that season and the accepted in-season date window.

The FastAPI service now reads from the similarity outputs in addition to the existing gold and metadata tables. Player detail uses `gold.player_similarity_features` and `gold.player_archetypes`, and compare adds a stat-profile similarity summary when both players have stable feature vectors.

When Redshift sync is enabled, bronze tables are also available in Redshift under the configured schema (default `nba_bronze`).

## Public Service

The FastAPI service is intended for Cloud Run and serves both HTML and JSON from the same process.

Public HTML routes:

- `/`
- `/players/{player_id}`
- `/compare`
- `/visualize`

Public JSON routes:

- `/api/leaderboard`
- `/api/trends`
- `/api/analysis/latest`
- `/api/recommendations`
- `/api/rankings`
- `/api/players/search`
- `/api/players/{player_id}`
- `/api/compare`
- `/api/health`

The service reads only from gold tables and metadata tables. It is public read-only for v1 and does not include auth.

Freshness is reported from the latest successful pipeline run in `nba_metadata.pipeline_run_log`, evaluated against a daily freshness threshold.

UI freshness states now render as relative time labels (for example, "2 days ago") with the exact ISO timestamp preserved in the hover title.

The compare page supports two entry modes: direct `player_a_id` deep links and first-player search when no initial player is provided.

`/api/analysis/latest` now returns the existing narrative fields plus nested `score_contribution` and `player_context` sections sourced from the expanded snapshot record.

`/api/players/{player_id}` now includes `archetype`, `similar_players`, and `similarity_reason` fields, and the player page renders an archetype card plus a similar-player panel when the sample is stable.

`/api/compare` now includes a `similarity` block with pair score, shared traits, and contrasting traits, and the compare page renders that stat-profile summary inline.

## Local Setup

1. Create and activate a virtual environment.
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Copy the local env template and fill in your project-specific values:

```bash
cp .env.example .env
```

4. Configure environment variables. Minimum useful local values:

```env
GCP_PROJECT_ID=your_gcp_project
BQ_PROJECT=your_gcp_project
GCS_BUCKET_NAME=your_gcs_bucket
BQ_DATASET_BRONZE=nba_bronze
BQ_DATASET_SILVER=nba_silver
BQ_DATASET_GOLD=nba_gold
BQ_METADATA_DATASET=nba_metadata
BQ_LOCATION=US
NBA_MAX_PLAYERS=0
NBA_REPLAY_DAYS=3
NBA_API_TIMEOUT_SECONDS=15
NBA_API_RETRIES=3
NBA_API_RETRY_BASE_DELAY_SECONDS=1.0
NBA_API_RETRY_BACKOFF_MULTIPLIER=2.0
NBA_API_RETRY_MAX_DELAY_SECONDS=8.0
NBA_BRONZE_BOOTSTRAP_MODE=auto
NBA_ENABLE_INJURY_REPORTS=true
NBA_INJURY_REPORT_START_DATE=2026-04-18
NBA_INJURY_REPORT_END_DATE=
NBA_INJURY_REPORT_MAX_REPORTS=21
NBA_INJURY_REPORT_REPLAY_DAYS=2
NBA_INJURY_REPORT_TIMES_ET=05_00PM
NBA_INJURY_REPORT_DELAY_SECONDS=0.25
AIRFLOW_LIVE_VALIDATE_TIMEOUT_SECONDS=7200
AIRFLOW_LIVE_VALIDATE_POLL_SECONDS=30
AIRFLOW_LIVE_VALIDATE_RUN_DBT=true
AIRFLOW_LIVE_VALIDATE_FAIL_ON_ACTIVE_RUNS=true
AIRFLOW_LIVE_VALIDATE_EXECUTOR=SequentialExecutor
NBA_ARCHETYPE_CLUSTERS=6
NBA_SCHEDULE_LOOKAHEAD_DAYS=7
DBT_TARGET=dev
API_FRESHNESS_THRESHOLD_HOURS=36
API_MAX_SEARCH_RESULTS=12
PORT=8080
AIRFLOW_HOME=./airflow_home
ENABLE_REDSHIFT=false
```

## Running Airflow Locally

This repo supports a host-based Airflow workflow without Docker. The included `Makefile`
standardizes the repo-local `AIRFLOW_HOME` path and points Airflow at `dags/`.
If a `.env` file exists in the repo root, `make` exports those variables into the Airflow
commands automatically. If `airflow` is not on your global `PATH`, the `Makefile` falls back
to the repo-local `.venv-airflow` Python and runs `python -m airflow`.

Initialize the local Airflow metadata database:

```bash
make airflow-init
```

This migrates the local metadata DB, reserializes DAGs, and lists the registered DAGs.

Create an admin user for the local web UI:

```bash
make airflow-create-user
```

Start the scheduler and webserver in separate terminals:

```bash
make airflow-scheduler
make airflow-webserver
```

Trigger the pipeline manually:

```bash
make airflow-trigger
```

The trigger target runs the same local init/sync path first so `nba_analytics_pipeline`
is registered in Airflow's metadata DB before creating a DAG run.

Run a bounded scheduler-backed live validation:

```bash
make airflow-live-validate
```

This starts a local scheduler, temporarily unpauses `nba_analytics_pipeline` if it was
paused, triggers one uniquely named manual run, waits for a terminal DAG state, restores
the previous pause state, and stops the scheduler. It then checks the four bronze contract
tables and runs the targeted dbt gold contract build. Reports are written under
`reports/pipeline_triage/`, which is ignored by git. The validation refuses to start if
existing queued or running DagRuns are present unless
`AIRFLOW_LIVE_VALIDATE_FAIL_ON_ACTIVE_RUNS=false` or `--allow-existing-active-runs` is
used. During validation the local scheduler runs with scheduled-run creation disabled so
the temporary unpause does not create an unrelated scheduled DagRun. The harness also
generates ignored local wrappers so Airflow task subprocesses use the known-good
`python -m airflow` entrypoint and an exec-based task runner instead of macOS `fork`.

Bronze bootstrap behavior is controlled by `NBA_BRONZE_BOOTSTRAP_MODE`:

- `auto`: derive missing or empty auxiliary bronze tables from `raw_game_logs`.
- `off`: disable derived bronze bootstrap.
- `force`: re-merge derived auxiliary bronze rows even when tables already have rows.

NBA API timeout and retry behavior is controlled by `NBA_API_TIMEOUT_SECONDS`,
`NBA_API_RETRIES`, `NBA_API_RETRY_BASE_DELAY_SECONDS`,
`NBA_API_RETRY_BACKOFF_MULTIPLIER`, and `NBA_API_RETRY_MAX_DELAY_SECONDS`.

Official injury-report ingestion is bounded by `NBA_INJURY_REPORT_MAX_REPORTS`,
`NBA_INJURY_REPORT_TIMES_ET`, and `NBA_INJURY_REPORT_REPLAY_DAYS`. The default
first-run window starts at `2026-04-18` for playoff backfill and then advances
with a separate injury-report watermark, which keeps repeated runs from fetching
or loading a large historical range. The injury watermark advances after a
bounded candidate batch is checked, even if the official PDFs are not published
yet, but metadata persistence keeps the maximum existing watermark so replay
or manually bounded runs cannot move it backward. The replay window still
refetches recent report dates for late updates.
The official NBA PDF endpoint can return parser text as one token per line, so
the ingestion normalizes tokenized PDF output back into logical injury-report
rows before DQ and merge. Player-name matching strips diacritics from NBA static
lookup names so official report spellings like ASCII transliterations still
resolve to player IDs, and `PLAYER_ID` is serialized as a nullable integer for
BigQuery CSV loads.
When injury reports are the only changed domain, the DAG runs a targeted dbt
build for injury availability models instead of rebuilding the full warehouse.
`gold.player_availability_current` includes `is_report_stale` so consumers can
separate current availability signals from older report appearances.
Game logs remain the hard-gated source; schedule, line-score, and player-reference
timeouts soft-fail after retries so the bronze bootstrap path can keep the core
contract moving when `raw_game_logs` already has rows.

Live validation behavior is controlled by `AIRFLOW_LIVE_VALIDATE_TIMEOUT_SECONDS`,
`AIRFLOW_LIVE_VALIDATE_POLL_SECONDS`, `AIRFLOW_LIVE_VALIDATE_RUN_DBT`,
`AIRFLOW_LIVE_VALIDATE_FAIL_ON_ACTIVE_RUNS`, and
`AIRFLOW_LIVE_VALIDATE_ENABLE_REDSHIFT`. The local executor defaults to
`AIRFLOW_LIVE_VALIDATE_EXECUTOR=SequentialExecutor`. Core GCP validation disables the
optional Redshift branch by default; pass `--enable-redshift` to include it.

Useful URLs and commands:

- Airflow UI: `http://localhost:8080`
- List DAGs: `make airflow-list`
- Run a DAG parse check: `make airflow-parse`
- Run scheduler-backed live validation: `make airflow-live-validate`

## Running the App Locally

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

The service is available at `http://localhost:8080`. See **Public Service** above for available routes.

## QA Expectations

The repo now expects QA coverage across data logic, orchestration, and the public service.

Primary validation commands:

```bash
python -m compileall dags app tests
PYTHONPATH=. pytest
dbt parse --project-dir . --profiles-dir dbt/profiles
dbt test --project-dir . --profiles-dir dbt/profiles --target dev \
  --exclude source:gold_runtime.analysis_snapshots path:dbt/tests/no_duplicate_analysis_snapshots.sql
dbt build --project-dir . --profiles-dir dbt/profiles --target dev \
  --select player_similarity_feature_input
dbt test --project-dir . --profiles-dir dbt/profiles --target dev \
  --select workbench_compare workbench_dashboard workbench_home_dashboard workbench_player_detail
```

Additional checks when validating Airflow changes:

- DAG import/parse in the local Airflow environment

When validating Redshift cross-db compatibility:

```bash
dbt parse --project-dir . --profiles-dir dbt/profiles --target redshift
dbt build --project-dir . --profiles-dir dbt/profiles --target redshift --select path:dbt/models/silver
```

Current local validation caveats:

- `dbt parse` runs locally without warehouse access.
- `dbt build --target redshift --select path:dbt/models/silver` is the recommended compatibility check for the Redshift secondary warehouse and requires working Redshift credentials plus the `dbt-redshift` adapter.
- `dbt test` requires a real BigQuery-enabled project and valid GCP auth; it will fail against placeholder projects such as `local-project`.
- The targeted workbench-model `dbt test --select ...` command has the same BigQuery auth requirement.
- In the latest local validation run for this branch, `python -m compileall dags scripts tests`, `PYTHONPATH=. pytest`, `dbt parse`, `make airflow-parse`, and targeted dbt tests for `dim_game`, `fct_team_game_scores`, `player_fantasy_rankings`, and `stg_schedule_clean` all pass.
- In the latest live validation run for this branch, the configured BigQuery project is reachable and the minimum core chain builds successfully:
  `dim_player dim_team dim_game fct_player_game_stats fct_team_game_scores fct_player_scoring_contribution player_recent_form player_similarity_feature_input`.
- The injury availability production path has been validated with the targeted dbt selector:
  `stg_player_injury_reports_clean player_availability_current`.
- Live Airflow orchestration still needs a scheduler-backed validation run. Local `make airflow-trigger` now registers the DAG before triggering, and NBA API calls now use bounded timeout/retry settings. The prior warehouse validation was repaired directly in BigQuery.

## Security Hygiene

- Runtime services read from GCP auth and environment configuration; credentials must not be committed.
- The public service is read-only and only queries curated gold and metadata tables.
- Pipeline triage artifacts are operational reports. They redact obvious credential-looking tokens before writing subprocess failure summaries, but runtime logs and ignored local `.env` files should still be treated as sensitive.
- Any previously used Anthropic credentials should be treated as local-only and rotated if they were ever stored outside ignored files.
