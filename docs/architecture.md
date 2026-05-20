# Architecture

This project is a production-style NBA analytics platform for the `2025-26`
season. BigQuery is the warehouse system of record. Redshift is an optional
learning/portfolio path, not a replacement for the default runtime.

## Core Decisions

- BigQuery is the primary warehouse.
- GCS stores raw extract snapshots and landing files.
- dbt owns bronze/silver/gold transformation logic.
- Self-hosted Airflow is the supported orchestration path.
- FastAPI serves the public read-only site and API.
- Terraform manages GCP infrastructure and optional AWS Redshift infrastructure.
- Analysis output is deterministic and template-based.
- Media sentiment ingestion is deferred until source, cost, and retention rules
  are explicit.

## Pipeline Flow

The Airflow DAG in `dags/nba_analytics_dag.py` runs this path:

1. Extract active-player game logs for `2025-26`.
2. Apply incremental filtering using the persisted watermark plus replay buffer.
3. Derive changed `game_id` values and fetch team line scores.
4. Fetch active-player reference attributes and roster context.
5. Fetch upcoming schedule context.
6. Fetch bounded official NBA injury report PDFs.
7. Validate source contracts for all non-empty extracted domains.
8. Land source files in GCS and load bronze staging tables.
9. Run staging DQ checks.
10. Merge into bronze raw tables with reconciliation checks.
11. Run dbt bronze/silver/gold/agent models and tests.
12. Publish similarity vectors and archetype clusters.
13. Build deterministic `analysis_snapshots` output.
14. Publish watermark and run metadata to `nba_metadata`.

## Warehouse Layout

Bronze raw tables:

- `nba_bronze.raw_game_logs`
- `nba_bronze.raw_game_line_scores`
- `nba_bronze.raw_player_reference`
- `nba_bronze.raw_schedule`
- `nba_bronze.raw_player_shot_locations`
- `nba_bronze.raw_player_injury_reports`

Silver models:

- `stg_game_logs_clean`
- `stg_game_line_scores_clean`
- `stg_player_reference_clean`
- `stg_player_shot_locations_clean`
- `stg_schedule_clean`
- `stg_player_injury_reports_clean`
- `int_player_game_enriched`

Gold facts and dimensions:

- `dim_player`
- `dim_team`
- `dim_game`
- `fct_player_game_stats`
- `fct_team_game_scores`
- `fct_player_scoring_contribution`

Gold serving models:

- `daily_leaderboard`
- `player_trends`
- `player_recent_form`
- `player_category_profile`
- `player_shot_location_profile`
- `player_fantasy_rankings`
- `fantasy_insights`
- `player_opportunity_outlook`
- `player_availability_current`
- `player_search_index`
- `workbench_compare`
- `workbench_dashboard`
- `workbench_home_dashboard`
- `workbench_player_detail`
- `analysis_snapshots`

Agent serving models:

- `nba_agent.agent_player_search`

Similarity outputs:

- `player_similarity_feature_input`
- `player_similarity_features`
- `player_archetypes`

The public baseline and publish contract are documented in
[`docs/player-similarity-model.md`](player-similarity-model.md). Tuned
personal-model work should stay outside the public repo; see
[`docs/public-private-boundary.md`](public-private-boundary.md).

Metadata tables:

- `ingestion_state`
- `pipeline_run_log`
- `source_contract_results`

## Serving Path

The FastAPI service reads from gold, agent, and metadata tables. Player detail,
compare, dashboards, freshness, and analysis snapshots use curated gold read
models. Player resolution for search and `/ask` starts from
`nba_agent.agent_player_search`, which denormalizes qualified player identity,
season averages, percentiles, trend state, availability, and an answer-context
string into one agent-specific table. The OpenAI agent still reaches data only
through allowlisted application tools and does not get arbitrary SQL access.

## Optional Secondary Path

Redshift sync exports BigQuery bronze tables as Parquet through GCS, copies them
to S3, loads Redshift Serverless with `COPY`, and runs dbt Redshift models.
