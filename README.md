# NBA Data Platform

Production-style NBA data pipeline and public stats workbench for the `2025-26`
season. The core platform is GCP-first: GCS for landing files, BigQuery for the
warehouse, dbt for transformations, Airflow for orchestration, and FastAPI for a
Cloud Run-ready public site/API.

Primary flow:

```text
NBA API + official NBA injury reports
  -> GCS landing
  -> BigQuery bronze
  -> dbt silver/gold/agent
  -> similarity, rankings, snapshots, agent search
  -> FastAPI site/API
```

Optional portfolio paths include Redshift Serverless as a secondary warehouse.

## Core Components

- **Airflow**: orchestrates extraction, source contracts, staging loads, DQ,
  bronze merges, dbt builds, similarity publishing, and run metadata.
- **BigQuery**: system-of-record warehouse across bronze, silver, gold, agent,
  and metadata datasets.
- **dbt**: owns cleaned, modeled, and serving-layer tables for analytics and the
  public application, including the agent-specific search context table.
- **FastAPI**: serves HTML pages and JSON APIs from curated gold, agent, and
  metadata tables.
- **Source contracts**: validate source shape and business rules before
  non-empty extracts are landed.
- **OpenAI stats agent**: optional `/ask` experience over allowlisted semantic
  stats tools.

## Data Domains

The pipeline ingests five source domains:

- player game logs
- team line scores
- player reference and roster context
- upcoming schedule context
- official NBA injury reports

Game logs are the hard-gated source. Schedule, line score, and player reference
extracts can soft-fail after retries so a valid game-log run can still advance
when supporting endpoints are unavailable.

## Warehouse Outputs

The current warehouse is centered on the `2025-26` season.

- **Bronze**: raw source tables and operational staging tables.
- **Silver**: cleaned source models plus enriched player-game rows.
- **Gold facts/dimensions**: player stats, team scores, scoring contribution,
  players, teams, and games.
- **Gold serving tables**: leaderboard, trends, rankings, player detail,
  compare, dashboard, availability, recommendations, and legacy search index.
- **Agent serving table**: `nba_agent.agent_player_search` is a dedicated
  player context table for `/ask` player resolution and answer grounding.
- **Similarity outputs**: feature input plus published feature vectors and
  archetypes.
- **Runtime metadata**: ingestion state, source contract outcomes, run log, and
  deterministic analysis snapshots.

See [Architecture](docs/architecture.md) for the detailed table layout.

## Public App

The FastAPI service serves both HTML pages and JSON routes:

- home dashboard, player pages, compare, visualize, and ask pages
- leaderboard, trends, analysis snapshot, recommendations, rankings
- player search/detail, game logs, percentiles, similarity, and health

The service is public read-only for v1. It reads from curated gold, agent, and
metadata tables; it does not expose arbitrary SQL access.

The `/ask` page is enabled with `OPENAI_API_KEY`. The agent can only call
allowlisted app tools for player resolution, game logs, trends, percentiles,
rankings, similarity, and metric leaderboards.

See [Public Service](docs/public-service.md) for route and agent details.

## Local Quickstart

Create a virtual environment and install dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create local configuration:

```bash
cp .env.example .env
```

Minimum useful local values:

```env
GCP_PROJECT_ID=your_gcp_project
BQ_PROJECT=your_gcp_project
GCS_BUCKET_NAME=your_gcs_bucket
BQ_DATASET_BRONZE=nba_bronze
BQ_DATASET_SILVER=nba_silver
BQ_DATASET_GOLD=nba_gold
BQ_DATASET_AGENT=nba_agent
BQ_METADATA_DATASET=nba_metadata
BQ_LOCATION=US
DBT_TARGET=dev
```

Run the app locally:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8001
```

App URL: `http://localhost:8001`

Airflow's local webserver also defaults to port `8080`, so use a separate app
port when running both services.

## Pipeline Quickstart

This repo supports host-based local Airflow without Docker. The `Makefile`
exports `.env`, uses repo-local `airflow_home/`, and falls back to
`.venv-airflow/bin/python -m airflow` when available.

Initialize Airflow:

```bash
make airflow-init
```

Run parser checks:

```bash
make airflow-parse
```

Start scheduler and webserver in separate terminals:

```bash
make airflow-scheduler
make airflow-webserver
```

Trigger a manual run:

```bash
make airflow-trigger
```

Run the bounded live validation harness:

```bash
make airflow-live-validate
```

Airflow UI: `http://localhost:8080`

See [Local Airflow](docs/local-airflow.md) for replay windows, API retry
settings, injury ingestion, bootstrap behavior, and validation details.

## Validation

Fast local checks:

```bash
python -m compileall dags app scripts tests
PYTHONPATH=. pytest
dbt parse --project-dir . --profiles-dir dbt/profiles --target dev
```

Warehouse-backed dbt tests and live Airflow validation require working GCP auth,
a BigQuery-enabled project, and project-specific `.env` values.

See [Validation](docs/validation.md) for the full QA matrix.

## Optional Paths

- [Redshift Secondary Warehouse](docs/optional-redshift.md)
- [Source Contracts](docs/source-contracts.md)

## Security Hygiene

- Do not commit credentials or local `.env` files.
- Keep GCP, OpenAI, and AWS secrets in ignored local config or a managed secret
  store.
- The public service is read-only and queries curated serving tables.
- Local Airflow logs, dbt logs, pipeline triage output, notebooks, and build
  artifacts are ignored by git.
