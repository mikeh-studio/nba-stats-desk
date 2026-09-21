# Validation

Use the intended dependency environment. Start with checks relevant to the change;
expand to the applicable CI checks before pushing code. Credentials being available
does not make a live warehouse write, backfill, or paid evaluation an offline check.

## Change-based checks

| Change | Relevant validation |
| --- | --- |
| Documentation or agent instructions only | Review links, file paths, command/config references, ignore rules, and `git diff --check` |
| Python API or Ask | Ruff, mypy for application changes, relevant pytest coverage; full Python suite before pushing application changes |
| Semantic calculations or planning | Semantic evaluation below plus relevant identity, scope, missingness, and calculation tests |
| Browser UI or JavaScript | Full JavaScript suite and a populated browser smoke of the changed interactions; Python API tests if response contracts change |
| Pipeline or publication | Relevant source-contract, incremental, publication, freshness, and DAG tests; Airflow parse in its dependency environment |
| dbt | Parse locally, relevant model/contract tests, and explicitly scoped warehouse checks when required |
| Terraform | Initialization without a backend and validation; plans/applies are separate operations |

Follow the current [CI workflow](../.github/workflows/ci.yml) for the complete CI
configuration. Use `python` from the selected environment so pytest and scripts
share dependencies; install development dependencies from `requirements-dev.txt`.
Airflow uses its separate runtime as documented in [Local Airflow](local-airflow.md).

## CI-aligned local commands

After staging the intended changes, run `python scripts/check_public_boundary.py`.
It checks the Git index, not unstaged content; see the
[boundary policy](public-private-boundary.md) for its scope and limitations.

```bash
ruff check .
ruff format --check .
mypy
python -m pytest -q
python scripts/evaluate_semantics.py --output reports/semantic-layer/evaluation.json
npm run test:tracking
dbt parse --project-dir . --profiles-dir dbt/profiles --target dev
git diff --check
git diff --check origin/main...HEAD
```

`npm run test:tracking` runs all `tests_js/*.test.js`, including Ask and performance
tests. Running only `tests_js/tracking.test.js` is not the full JavaScript suite.
The two diff checks cover working edits and committed branch changes respectively;
inspect new untracked files separately. Report skipped checks and their reasons.

For DAG changes, also run `make airflow-parse` in the configured local Airflow
environment. For infrastructure changes, use the CI commands:

```bash
terraform -chdir=infra/terraform init -backend=false
terraform -chdir=infra/terraform validate
```

These Terraform commands do not apply infrastructure changes. Initialization may
download providers and modules. Live checks below require the appropriate access
and task scope; fixture success and dbt parsing do not prove deployment readiness.

Publication/freshness regression coverage:

```bash
python -m pytest tests/test_publication.py tests/test_freshness.py -q
.venv-airflow/bin/python -m pytest tests/test_dag_import.py tests/test_publication.py -q
dbt parse --project-dir . --profiles-dir dbt/profiles --target dev \
  --vars '{"injury_publication_suffix": "_candidate_validation"}'
```

These cover candidate load failures, aborted transactions, invalid/mismatched
similarity keys, additive schema changes, optional-stage retries, core/optional
dbt failure isolation, offseason/preseason behavior, opening-day grace, and old
source dates after a recent pipeline run. They do not execute BigQuery DML.
Live promotion/rollback validation should use disposable tables in a test
dataset, never deliberate failures against production serving tables.

`dbt parse` does not require warehouse access.

## BigQuery-Backed Checks

These require a real BigQuery-enabled project, valid GCP auth, and project
values in `.env`.

After a one-time season replay, validate the core models with:

```bash
# In one terminal, so task execution sees the season replay overrides:
make airflow-scheduler-season

# In another terminal:
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
dbt test --project-dir . --profiles-dir dbt/profiles --target dev
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

Validate the playoff performance UI after starting a local app server with live
warehouse access:

```bash
PERFORMANCE_BASE_URL=http://127.0.0.1:8001 scripts/check_performance_ui.sh
```

For Ask UI work, run the JavaScript tests and a local `/ask` smoke. If you want
server-local chat history during manual QA, set `AGENT_HISTORY_ENABLED=true` in
`.env`; leave it disabled for public deployments.

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

## Rendered UI and evaluation checks

For UI data changes, verify populated, user-visible content in the rendered DOM,
not just a successful API response or the presence of an SVG element. Check
relevant metrics, sorting, selected-player details, and navigation after an asset
refresh. See [Evaluation](evaluation.md) for deterministic semantic fixtures,
frozen inputs, and optional model-backed reviews. Keep detailed run outputs under
ignored `reports/`; use CI or a dated PR summary for validation results.
