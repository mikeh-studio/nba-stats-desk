# NBA Stats Desk

[![CI](https://github.com/mikeh-studio/nba-stats-desk/actions/workflows/ci.yml/badge.svg)](https://github.com/mikeh-studio/nba-stats-desk/actions/workflows/ci.yml)

NBA Stats Desk is a GCP-backed NBA analytics workbench for the `2025-26` season.
It uses NBA API and injury-report sources, GCS, BigQuery, dbt, Airflow, and a
Cloud Run-ready FastAPI app to serve a natural-language `/ask` agent, recent
performance views, player research pages, and a 3D similarity map. The `/ask`
runtime can call OpenAI or Claude APIs for planning and answer generation while
all stats access stays bounded to curated warehouse-backed tools.

See the [architecture reference](docs/architecture.md) for the pipeline and
serving layout, and the
[player similarity map screenshot](docs/images/similarity-map.png) for the
current 3D research view.

Core flow:

```text
NBA API + injury reports
  -> GCS
  -> BigQuery
  -> dbt gold + agent models
  -> FastAPI `/ask`, Performance, and stats APIs
```

Optional portfolio paths include Redshift Serverless as a secondary warehouse.

## What It Shows

- **Agentic Ask flow**: `/ask` plans questions, resolves players, asks
  clarifying follow-ups, calls allowlisted semantic tools, and returns grounded
  answers with charts, tables, assumptions, and metric context.
- **Performance insights**: `/performance` compares 2025-26 playoff player games
  against season baselines with filters, minutes, shooting metrics, percentiles,
  and 30-day trend context.
- **Research views**: player detail, comparisons, rankings, leaderboards,
  recommendations, and a 3D player similarity map support deeper stat review.
- **Analytics engineering backbone**: source contracts, dbt models,
  orchestration, metadata, and read-only serving keep the public app tied to
  curated warehouse outputs.

## Stack

- **GCP**: GCS landing, BigQuery warehouse, and Cloud Run-ready serving.
- **dbt**: bronze, silver, gold, and agent models for analytics and app APIs.
- **Airflow**: orchestrates extraction, source contracts, staging loads, DQ,
  bronze merges, dbt builds, similarity publishing, and run metadata.
- **FastAPI**: serves `/ask`, Performance, player, compare, similarity, and JSON
  APIs from curated gold, agent, and metadata tables.
- **Agentic tools**: query planning, player resolution, clarification handling,
  LLM API calls, semantic metric tools, and evidence-bounded answer rendering.

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
  compare, dashboard, recent performance workbench, availability,
  recommendations, and legacy search index.
- **Agent serving table**: `nba_agent.agent_player_search` is a dedicated
  player context table for `/ask` player resolution and answer grounding.
- **Similarity outputs**: feature input plus public baseline feature vectors
  and archetypes.
- **Runtime metadata**: ingestion state, source contract outcomes, run log, and
  deterministic analysis snapshots.

See [Architecture](docs/architecture.md) for the detailed table layout.

## Public App

The FastAPI service serves read-only HTML pages and JSON routes from curated
gold, agent, and metadata tables. The root route redirects to `/ask`, making the
agent the default entry point while keeping Performance and directed research
pages one click away.

- ask, performance, player, compare, and similarity map pages
- leaderboard, trends, recent game performance, analysis snapshot,
  recommendations, and rankings
- player search/detail, game logs, percentiles, similarity, and health

The similarity map (`/similarity-map`) is a 3D PCA projection of the player
similarity vectors: players cluster by archetype, selecting one traces edges to
its true cosine-nearest matches, and each axis is labeled with the features that
drive it.

![Player similarity map](docs/images/similarity-map.png)

The `/ask` page is enabled with `OPENAI_API_KEY` and/or `ANTHROPIC_API_KEY`.
The page can switch between the OpenAI API and the Claude API, then choose a
supported model version for the request. `.env` defaults (`OPENAI_AGENT_MODEL`
and `ANTHROPIC_AGENT_MODEL`) apply when the UI does not send a model. Claude
requests stream token-by-token (answers appear in the UI as they generate),
use provider prompt caching to cut repeat-request cost, and honor a separate
`ANTHROPIC_AGENT_TIMEOUT_SECONDS` wall clock (default 90s) since structured
answers run longer than the OpenAI path's `OPENAI_AGENT_TIMEOUT_SECONDS`. The
agentic flow calls the selected LLM API for planning and answer generation, but
data access stays bounded: it resolves player names from warehouse-backed search
context, asks for
clarification on ambiguous requests, and can only call allowlisted app tools for
player resolution, game logs, trends, percentiles, rankings, similarity, and
metric leaderboards. It does not expose arbitrary SQL access.

See [Public Service](docs/public-service.md) for route and agent details.

## Public Boundary

This repo is public-safe by design: it contains the data platform, source
contracts, dbt feature layer, public baseline similarity model, and read-only
app. Tuned personal-model code, generated reports, notebooks, model artifacts,
and real credentials should stay private. See
[Public / Private Boundary](docs/public-private-boundary.md).

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

Run a one-time full-season stats replay:

```bash
make airflow-backfill-season
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
- Keep GCP, OpenAI, Anthropic, and AWS secrets in ignored local config or a
  managed secret store.
- The public service is read-only and queries curated serving tables.
- Local Airflow logs, dbt logs, pipeline triage output, notebooks, and build
  artifacts are ignored by git.
