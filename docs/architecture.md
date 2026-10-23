# Architecture

This project is an NBA analytics platform serving `2023-24` through `2026-27`.
BigQuery is the warehouse system of record.

## Core Decisions

- BigQuery is the primary warehouse.
- GCS stores raw extract snapshots and landing files.
- dbt owns bronze/silver/gold transformation logic.
- Self-hosted Airflow is the supported orchestration path.
- FastAPI serves the public read-only site and API.
- Terraform manages GCP infrastructure.
- Governed statistics are computed deterministically; model-generated narratives
  use bounded application evidence.
- Similarity training runs offline; validated public outputs are published to
  gold tables and read by the application.

## Pipeline Flow

The Airflow DAG in `dags/nba_analytics_dag.py` runs this path:

1. Extract active-player game logs for the configured current season.
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
12. Publish similarity vectors and archetype clusters best-effort.
13. Publish watermark and run metadata to `nba_metadata`, including any
    non-blocking asset status.

### Publication and partial failures

Injury extraction, staging, DQ, and bronze merge stages retain their configured
Airflow retries. On the final failed attempt they return a failed asset status;
downstream injury stages short-circuit, allowing valid core stats to continue.
Empty injury responses never advance the injury watermark.

dbt builds the core excluding `stg_player_injury_reports_clean+`. It then builds
that injury branch (clean injury rows, current availability, and dependent agent
context) under unique candidate aliases using `injury_publication_suffix`.
Candidate models expire after 24 hours. Only a successful dbt build, including
its tests, can publish all four serving tables in a single BigQuery transaction,
including dated `what_changed_injury_reports` evidence.
An injury failure leaves their previous serving versions intact. On the first
ever build, an unsuccessful optional publication may leave those tables absent;
the health response does not claim a previous version exists in that case.

Similarity output validation precedes both candidate loads. After both finish,
one transaction replaces the affected seasons in the feature and archetype
tables together. Load or transaction failure leaves previous rows untouched.
Nullable schema additions occur before DML; incompatible schema changes reject
publication. Candidate cleanup failure cannot turn a committed publication into
a reported failure; expiration provides a fallback cleanup mechanism.

The run log retains `status=success` to mean core success and adds explicit
`publication_status`, `stats_status`, `injuries_status`, and source dates to its
existing details field. Health aggregates asset success/failure attempts across
runs so later no-op runs cannot hide a failed optional publication. These
records use pipeline completion time as publication time, an upper bound on the
actual table commit. A worker failure before metadata publication can leave
newer serving data than the recorded status; it does not remove serving data.

## Warehouse Layout

The independent [`nba_gamebook_dev` DAG](gamebook-ingestion.md) discovers completed
regular-season games by reading existing NBA API game logs and upstream run health,
then processes PDF gamebooks into isolated dev GCS/BigQuery
storage. It starts paused, records source failures and validation rejections,
revisits games for corrections, and preserves prior accepted revisions on failed
replacement. It is not connected to the production analytics DAG, dbt, or Ask.

The separate [ESPN dev pilot](espn-dev-pilot.md) captures alternative-provider
games, box scores and assist events, preserves ESPN/NBA ID crosswalks, and
compares mapped events with official PDFs. Its revisioned dev tables and run
health are separate from NBA API production ingestion. It also starts paused.

The manual [ESPN-only dev worker](espn-only-dev.md) publishes reconciled PBP and
assist facts to a separate `nba_gamebook_dev_espn_only` dataset, with independent
NBA identity and revision-specific PDF verification statuses. It retains prior
valid facts on failed replacements and is not connected to a scheduler or Ask.

The manual [ESPN correction worker](espn-corrections-dev.md) independently accepts
reported final scores, player box scores, and reconciled PBP, with revision-bound
dependencies and bounded correction-window discovery. Its dev-only tables retain
failed replacement evidence and exclude stale assist relationships from the
validated view. Airflow scheduling and production consumers remain unchanged.

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

- `player_trends`
- `player_recent_form`
- `player_category_profile`
- `player_shot_location_profile`
- `player_fantasy_rankings`
- `player_opportunity_outlook`
- `player_availability_current`
- `player_search_index`
- `recent_performance_workbench`
- `workbench_compare`
- `workbench_dashboard`
- `workbench_home_dashboard`
- `workbench_player_detail`

Agent serving models:

- `nba_agent.agent_player_search`

Similarity outputs:

- `player_similarity_feature_input`
- `player_similarity_features` (also carries the selected public archetype
  assignment, per-model comparison metadata, the 3D map projection
  `proj_x/y/z`, and per-axis driver metadata `projection_axes`)
- `player_archetypes`

The public baseline and publish contract are documented in
[Profile similarity](pages.md#statistical-profile-similarity).
Tuned personal-model work
should stay outside the public repo; see
[`docs/public-private-boundary.md`](public-private-boundary.md).

Metadata tables:

- `ingestion_state`
- `pipeline_run_log`
- `source_contract_results`

Context models:

- `team_game_context`
- `team_defense_before_game`
- `player_game_reported_status`
- `player_game_context`

These preserve player-game grain and distinguish prior opponent/status evidence
from same-game outcomes. See [Player context](pages.md#player-context) for availability
cutoffs, coverage rules, and the offline integration boundary.

## Serving Path

The FastAPI service reads from gold, agent, and metadata tables. Player detail,
compare, performance, dashboards, and freshness use curated
gold read models. Repository search and compatibility agent tools use
`nba_agent.agent_player_search`, which denormalizes qualified player identity,
season averages, percentiles, trend state, availability, and an answer-context
string. Governed Ask metrics and overviews instead use `SemanticWarehouse` to
capture complete season facts and resolve identities from that evidence. The
[Phase 1 serving contract](ask-serving-contract.md) defines offline aggregate and
publication validation; it does not switch those routes to serving tables.
Governed metric queries use the
[semantic contract](semantic-contract.md). The stats agent reaches data only
through allowlisted application tools before calling the selected OpenAI API or
Claude API model, and it does not get arbitrary SQL access.
