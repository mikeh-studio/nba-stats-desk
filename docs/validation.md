# Validation

Use quick local checks for code shape and targeted warehouse-backed checks when
GCP credentials are available.

## Fast Local Checks

```bash
python -m compileall dags app scripts tests
PYTHONPATH=. pytest
dbt parse --project-dir . --profiles-dir dbt/profiles --target dev
make airflow-parse
```

`dbt parse` does not require warehouse access.

## BigQuery-Backed Checks

These require a real BigQuery-enabled project, valid GCP auth, and project
values in `.env`.

After a one-time season replay, validate the core models with:

```bash
make airflow-backfill-season
dbt build --project-dir . --profiles-dir dbt/profiles --target dev \
  --select dim_player dim_team dim_game fct_player_game_stats fct_team_game_scores \
    fct_player_scoring_contribution player_recent_form player_shot_location_profile
```

For the targeted official injury-report backfill, start with a local candidate
plan and then run the live backfill only when GCP credentials and the target
project are available:

```bash
python -m dotenv run --no-override -- .venv-airflow/bin/python scripts/backfill_injury_reports.py \
  --start-date 2025-10-21 \
  --end-date 2026-05-13 \
  --dry-run

python -m dotenv run --no-override -- .venv-airflow/bin/python scripts/backfill_injury_reports.py \
  --start-date 2025-10-21 \
  --end-date 2026-05-13
```

```bash
dbt test --project-dir . --profiles-dir dbt/profiles --target dev \
  --exclude source:gold_runtime.analysis_snapshots path:dbt/tests/no_duplicate_analysis_snapshots.sql
```

Validate core serving dependencies:

```bash
dbt build --project-dir . --profiles-dir dbt/profiles --target dev \
  --select dim_player dim_team dim_game fct_player_game_stats fct_team_game_scores \
    fct_player_scoring_contribution player_recent_form player_shot_location_profile \
    player_similarity_feature_input agent_player_search
```

Validate public player similarity baseline training without live BigQuery:

```bash
pytest tests/test_player_similarity_model.py tests/test_incremental_pipeline.py -q
```

Validate workbench read models:

```bash
dbt test --project-dir . --profiles-dir dbt/profiles --target dev \
  --select workbench_compare workbench_dashboard workbench_home_dashboard workbench_player_detail
```

Validate injury availability models:

```bash
dbt build --project-dir . --profiles-dir dbt/profiles --target dev \
  --select stg_player_injury_reports_clean player_availability_current
```

Validate only the agent search table after gold models already exist:

```bash
dbt build --project-dir . --profiles-dir dbt/profiles --target dev \
  --select agent_player_search
```

Validate that latest pre-game `Out` injury-report rows do not have same-game
minutes:

```bash
dbt test --project-dir . --profiles-dir dbt/profiles --target dev \
  --select out_injury_pregame_same_game_has_no_minutes
```

## Airflow Validation

```bash
make airflow-live-validate
```

The live harness starts and stops a local scheduler, triggers a unique run, and
writes ignored reports under `reports/pipeline_triage/`.

## Optional Path Checks

Redshift:

```bash
dbt parse --project-dir . --profiles-dir dbt/profiles --target redshift
dbt build --project-dir . --profiles-dir dbt/profiles --target redshift \
  --select path:dbt/models/silver
```

## Caveats

- BigQuery tests fail against placeholder projects such as `local-project`.
- Redshift checks require credentials and the `dbt-redshift` adapter.
- Live Airflow validation depends on NBA endpoint availability and configured
  GCP access.
