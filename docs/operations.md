# Operations

Running, validating, and maintaining the pipeline and Ask locally. Live warehouse, backfill, and paid evaluation commands are marked where they need credentials.

- [Local Airflow](#local-airflow)
- [Source Contracts](#source-contracts)
- [Validation](#validation)
- [Historical seasons](#historical-seasons)
- [Daily player comparison updates](#daily-player-comparison-updates)
- [Model catalog review](#model-catalog-review)

## Local Airflow

This repo supports a host-based Airflow workflow without Docker. The `Makefile`
standardizes the repo-local `AIRFLOW_HOME` path and points Airflow at `dags/`.
If `.env` exists, `make` exports those values into Airflow commands.

If `airflow` is not on the global `PATH`, the `Makefile` uses the repo-local
`.venv-airflow` Python and runs `python -m airflow`.

### Setup

Initialize the local metadata DB and registered DAGs:

```bash
make airflow-init
```

The init target also installs `scripts/airflow_local_settings.py` into the
ignored local `airflow_home/config/` directory. That keeps scheduler task
subprocesses pointed at the repo-local `.venv-airflow` executable when the DAG
runs under launchd.

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

The DAG is scheduled daily at `0 11 * * *` UTC through `2026-07-01`, giving the
2025-26 season a post-Finals correction buffer before scheduled runs stop. To
allow scheduled runs, keep the scheduler running and unpause the DAG:

```bash
make airflow-unpause
```

### Daily Local Scheduler

For a hands-off local setup on macOS, install a LaunchAgent generated for the
current checkout. It starts the Airflow scheduler at login, keeps it alive, and
writes scheduler logs under ignored local `logs/` files:

```bash
scripts/install_airflow_launch_agent.sh
```

Before relying on scheduled runs, initialize Airflow and unpause the DAG:

```bash
make airflow-init
make airflow-unpause
```

Check the service:

```bash
launchctl print "gui/$(id -u)/com.nba-gcp.airflow-scheduler"
tail -f logs/airflow-scheduler.launchd.err.log
```

Trigger manually:

```bash
make airflow-trigger
```

The trigger target runs local initialization first so
`nba_analytics_pipeline` is registered before creating a run.

### Live Validation

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

### Replay and Backfill

Game-log extraction defaults to:

```env
NBA_REPLAY_DAYS=3
NBA_MAX_PLAYERS=0
NBA_BRONZE_BOOTSTRAP_MODE=auto
```

For a full `2025-26` stats backfill without resetting metadata, use the
dedicated scheduler and trigger targets. Start the scheduler with replay
overrides in one terminal:

```bash
make airflow-scheduler-season
```

Then trigger the run from another terminal:

```bash
make airflow-backfill-season
```

Those targets run the DAG with `NBA_REPLAY_DAYS=365` and
`NBA_BRONZE_BOOTSTRAP_MODE=force`. The scheduler-side override matters because
the trigger command queues a DagRun, while the scheduler process is what
executes the tasks and reads environment fallback values. The forced bootstrap
matters when auxiliary bronze tables already have partial rows: it re-derives
observed schedule, line-score, and player-reference rows from the full replayed
`raw_game_logs` instead of leaving an older partial `raw_schedule` untouched.

To use a different replay window:

```bash
FULL_SEASON_REPLAY_DAYS=420 make airflow-scheduler-season
FULL_SEASON_REPLAY_DAYS=420 make airflow-backfill-season
```

Return to the normal daily path with `NBA_REPLAY_DAYS=3` and
`NBA_BRONZE_BOOTSTRAP_MODE=auto` after the one-time replay.

### NBA API Retry Controls

Use these variables for bounded endpoint behavior:

```env
NBA_API_TIMEOUT_SECONDS=15
NBA_API_RETRIES=3
NBA_API_RETRY_BASE_DELAY_SECONDS=1.0
NBA_API_RETRY_BACKOFF_MULTIPLIER=2.0
NBA_API_RETRY_MAX_DELAY_SECONDS=8.0
NBA_SHOT_LOCATION_SEASON_TYPE="Regular Season"
```

Game logs are currently fetched through the active-player game-log endpoint, so
`NBA_MAX_PLAYERS=0` should remain in place for season coverage runs.
Aggregate shot-location data uses one league-wide player endpoint call per DAG
run and lands in `raw_player_shot_locations`.

### Injury Reports

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
  --end-date 2026-05-13 \
  --dry-run

python -m dotenv run --no-override -- .venv-airflow/bin/python scripts/backfill_injury_reports.py \
  --start-date 2025-10-21 \
  --end-date 2026-05-13
```

The dry run only builds the candidate plan. The live run derives project,
bucket, and dataset settings from `.env`, fetches one official `05_00PM` report
per day by default, validates the source contract, loads bronze staging, runs
the injury DQ checks, merges `bronze.raw_player_injury_reports`, updates the
injury watermark, and runs the targeted dbt injury availability build.

The utility has a default `240` candidate safety cap. Raise it with
`--max-candidates` or pass `--allow-large-window` for intentionally larger
windows. Official CDN `403` and `404` responses are treated as missing reports
so long season windows can skip dates where no archived PDF is available.
Archive filename formats and available dates can vary. Check the saved
source results for the requested period rather than inferring coverage from a
successful command.

### Bronze Bootstrap

`NBA_BRONZE_BOOTSTRAP_MODE` controls auxiliary bronze bootstrap behavior:

- `auto`: derive missing or empty auxiliary bronze tables from `raw_game_logs`.
- `off`: disable derived bronze bootstrap.
- `force`: re-merge derived auxiliary bronze rows even when tables already have rows.

### Useful Commands

```bash
make airflow-list
make airflow-parse
make airflow-pause
make airflow-unpause
make airflow-live-validate
```

## Source Contracts

Source contracts live under `contracts/` and run inside Airflow extract tasks
before non-empty dataframes are uploaded to GCS.

### Covered Domains

- `game_logs`
- `game_line_scores`
- `schedule`
- `player_reference`
- `player_shot_locations`
- `injury_reports`

Contracts define required columns, expected types, business keys, season/date
scope, enum values, and domain-specific bounds.

### Severities

- `fatal`: stop the pipeline before landing the batch.
- `quarantine`: remove violating rows and continue if valid rows remain.
- `warning`: record the issue without dropping rows.

The contract layer protects the ingestion boundary. BigQuery staging DQ checks
and dbt tests still run after landing to protect warehouse state and modeled
outputs.

### Audit Output

Each non-empty extract writes a pre-validation source audit snapshot to GCS:

```text
nba_data/<season>/source_audit/raw_extract/source=<domain>/run_id=<run>/
```

Rows removed by quarantine rules are written to:

```text
nba_data/<season>/source_audit/quarantine/source=<domain>/run_id=<run>/
```

Contract outcomes are upserted into
`nba_metadata.source_contract_results` with row counts, failure counts, GCS audit
URIs, landing URI, and serialized violation details.

### Operational Notes

Game logs remain hard-gated because they are the main fact source. Supporting
domains are allowed to soft-fail after bounded retries when the pipeline can
still move core game-log data safely.

See [Players and research breakdowns](pages.md#players-and-research-breakdowns) for shared detailed queries, multi-stat teammate studies, and versioned pregame context.

## Validation

Use the intended dependency environment. Start with checks relevant to the change;
expand to the applicable CI checks before pushing code. Credentials being available
does not make a live warehouse write, backfill, or paid evaluation an offline check.

### Change-based checks

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
Airflow uses its separate runtime as documented in [Local Airflow](operations.md#local-airflow).

### CI-aligned local commands

After staging the intended changes, run `python scripts/check_public_boundary.py`.
It checks the Git index, not unstaged content; see the
[boundary policy](public-private-boundary.md) for its scope and limitations.

```bash
ruff check .
ruff format --check .
mypy
python -m pytest -q
python scripts/evaluate_semantics.py --output reports/semantic-layer/evaluation.json
npm ci
npm run test:tracking
python scripts/evaluate_ask.py --output reports/ask-harness/unique-validation-run
npx playwright install chromium
npm run test:ask-browser
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
terraform -chdir=infra/terraform fmt -check -recursive
terraform -chdir=infra/terraform init -backend=false
terraform -chdir=infra/terraform validate
terraform -chdir=infra/terraform test
```

The policy tests use a mock provider and require Terraform 1.7 or later.
Read the [pipeline IAM migration](../infra/terraform/README.md) before planning
or applying changes to existing deployments.

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

### BigQuery-Backed Checks

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

For the targeted official injury-report backfill, follow
[Injury Reports](#injury-reports): dry run first, then the live run only when GCP
credentials and the target project are available.

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

### Airflow Validation

See [Live Validation](#live-validation) for the scheduler-backed harness.

### Caveats

- BigQuery tests fail against placeholder projects such as `local-project`.
- Live Airflow validation depends on NBA endpoint availability and configured
  GCP access.

### Rendered UI and evaluation checks

For UI data changes, verify populated, user-visible content in the rendered DOM,
not just a successful API response or the presence of an SVG element. Check
relevant metrics, sorting, selected-player details, and navigation after an asset
refresh. See [Evaluation](evaluation.md) for deterministic semantic fixtures,
frozen inputs, and optional model-backed reviews. Keep detailed run outputs under
ignored `reports/`; use CI or a dated PR summary for validation results.

## Historical seasons

The app supports `2025-26`, `2024-25`, and `2023-24`. All tabs default to the
latest supported season, with no shared season selector. Ask accepts a historical
season in the question and preserves it in follow-ups. Unspecified periods use
the latest available coverage, checking supported seasons newest first and
disclosing any fallback. Explicit historical API reads still accept
`?season=2023-24`; page links with that parameter redirect to the latest view.
Season-specific API caches and history files remain isolated.

Each archive uses its own datasets:

| Season | Bronze | Silver | Gold | Agent | Metadata |
| --- | --- | --- | --- | --- | --- |
| 2023-24 | nba_bronze_2023_24 | nba_silver_2023_24 | nba_gold_2023_24 | nba_agent_2023_24 | nba_metadata_2023_24 |
| 2024-25 | nba_bronze_2024_25 | nba_silver_2024_25 | nba_gold_2024_25 | nba_agent_2024_25 | nba_metadata_2024_25 |

Configured dataset base names receive the same suffix. Current-season datasets
and the scheduled Airflow DAG retain their existing names and behavior. A
historical dbt build uses one season and the final observed game date, keeping
rankings and recent-game calculations within that season.

### Backfill

Run with the environment that provides `nba_api`, BigQuery, dbt, and pandas:

```bash
.venv-airflow/bin/python scripts/backfill_historical_seasons.py \
  --directory reports/historical-seasons
```

This fetches regular-season and playoff player logs, independent team logs,
shot-location profiles, and daily official injury reports using the legacy
`05PM` archive filenames (the exact timestamp is read from each PDF).
League-wide queries include players who are no longer active. It validates both requested seasons before any warehouse write.

To publish the validated inputs and build the archives:

```bash
.venv-airflow/bin/python scripts/backfill_historical_seasons.py \
  --directory reports/historical-seasons --skip-extract --publish \
  --project YOUR_PROJECT --bucket YOUR_BUCKET
```

Use `--seasons 2023-24` to process one archive. The command refuses the current
season. The app's BigQuery principal needs read access to the new gold, agent,
and metadata datasets, plus its existing query-job permission.

Validation checks the production column/type/key contracts with the exact
historical date bounds; rejected rows block writes. Coverage checks require
1,230 regular-season games, 82 team appearances per team, and 15 playoff series
with a four-win series winner. Every game must have two team rows and player
scoring totals and dates must match independent team logs. Shot-profile player
coverage must match the corresponding game logs.

GCS inputs are stored at content-addressed paths under
`nba_data/<season>/historical_backfill/`. Reports record object generations,
SHA-256 fingerprints, row counts, source checks, and validation results. Unmatched injury-player
names are retained with their source name and a NULL player ID, as a contract
warning; they are not assigned to a guessed player.
Bronze writes use `WRITE_EMPTY`; retries require the same source fingerprint
and cannot overwrite a different archive by default. A retry can resume a matching empty
table after a failed initial load. To add newly validated sources or correct
this command's own historical inputs, `--replace-owned-archive` first copies
the affected tables to timestamped backups, then replaces them. It refuses
unowned tables and empty replacements. Previous local reports are retained
under `report_history/`. dbt and similarity outputs rebuild only the
selected historical datasets. Normal ingestion watermarks and production audit
records are not changed.

After successful dbt and similarity builds, the archive publishes its own
`historical_backfill_manifest` table. Failed builds retain their local report
and do not publish a success manifest. dbt job creation, execution, and the
build process have explicit time bounds.

### Coverage limits

- Coverage follows the NBA API's `Regular Season` and `Playoffs` categories.
- Team final scores are reconciled against official team logs. Quarter and
  overtime-period scores are unavailable in these league-wide extracts and
  remain NULL, including the derived overtime total.
- Schedule rows describe observed completed games. Neutral-site games whose
  logs mark both teams away use official game-summary home/visitor IDs. Player reference rows carry
  the season's last observed name/team; biography and physical measurements
  unavailable in these extracts remain NULL.
- Injury history uses one daily archive file and is never inferred from missed appearances.
  A supplied `<season>_injuries.parquet` must satisfy the injury contract.
  No injury rows means unavailable evidence, not a healthy player.
  `<season>_injury_source_checks.json` records every attempted daily archive.
  Download and parsing failures are recorded per day with `status: error`;
  other days continue. If no reports are usable, validation records missing
  injury coverage. Transport/HTTP failures receive up to three attempts;
  malformed headers stop after one. To retry failed days, remove only the
  combined `<season>_injuries.parquet` and rerun extraction; successful daily
  caches are reused. Local storage failures still abort because audit evidence
  cannot be reliably saved.
  Later intraday status changes are outside this collection; discrepancies
  between an earlier Out report and actual appearances remain warning rows.
- Historical API reads report archive status and do not expect daily refreshes.
  Similarity outputs can have less physical-profile context than the current
  season because historical biography fields are incomplete.

The backfill report is the evidence for actual coverage. A successful local
app check does not deploy the app or grant a production service account access.

### Metric semantics

The [governed semantic contract](semantic-contract.md) describes implemented
scope, aggregation, qualification, and evidence boundaries. Use the
[evaluation workflow](evaluation.md) to validate a frozen archive. Actual run
counts, source-capture audits, and warehouse job records belong in local reports.

## Daily player comparison updates

The completed 2025–26 comparison inputs can be checked daily for official
statistical corrections. The local maintenance command fetches fresh regular
season and playoff league player logs and independent team logs. It includes
every recorded player, including players no longer listed as active. Players
without appearances are not assigned fabricated zero production.

```bash
# Fresh capture and validation, without warehouse writes:
.venv-app/bin/python scripts/refresh_player_comparisons.py

# Publish validated changes after the isolated build and tests:
.venv-app/bin/python scripts/refresh_player_comparisons.py --publish
```

The command loads the existing local `.env` configuration. It requires the
standard `nba_bronze`, `nba_silver`, `nba_gold` and `nba_agent` datasets and the
existing BigQuery credentials and dbt runtime. Credentials and operational
reports remain private. Reports use unique directories under
`reports/daily-player-comparisons/`; runs cannot overlap on the same host.

Before creating candidates, validation checks the complete season, player/team
totals, minutes rounding bounds, shooting identities, every previous appearance,
and the game-log source contract. Quarantined rows block the refresh and retain
their audit export. Existing verified warehouse schedule assignments preserve
neutral-site home/away designations; a newer season's CDN schedule is not used.
New games or changed date/opponent assignments require a separately verified
schedule. This job does not expand the supported season list.

An unchanged capture performs no warehouse writes or dbt build. Changed inputs
use the [existing isolated rebuild and publication](evaluation.md#split-routing-and-completed-season-warehouse-repair)
with expiring candidates, backups, dbt tests, exact fact equality, refreshed
dependent serving/similarity models, and a transactional promotion. The original
comparison snapshot must match the backed-up fact before the build begins.
All candidate contents and live baselines are checked again within publication.
Failed source reads or validation leave serving data intact. A lost connection
during publication is recorded as an unconfirmed outcome rather than falsely
claiming the old data remains; inspect the BigQuery job before any retry.

Scheduling this command is separate from validating one run. A local Codex
heartbeat can invoke the publish command daily; it requires the local host and
Codex to be available. It does not repair or restart the older
[Airflow service](operations.md#local-airflow). The existing Airflow DAG remains the regular
incremental ingestion path, whose 2025–26 scheduled window ended in July.

The command itself makes no model calls. A Codex heartbeat uses its configured
model to run and review it. Avoid automatic retries, source substitution, warehouse
repairs outside this command, or paid Ask evaluations. Report new failures,
recoveries, and validated changes, preserving prior receipts for comparison.

Ask derives FG%, 3P%, FT%, eFG%, TS%, turnovers per game, and points/assists per
36 from the published player-game inputs. Existing saved answers remain frozen;
new questions use current warehouse data after normal cache expiry. Win Shares
and possession metrics remain unavailable and hidden.

## Model catalog review

Ask's enabled models remain in `app/config.py`. Discovery never adds a model to
the dropdown or changes a default. An optional local check provides a review
report and an explicit candidate smoke test.

### Check availability

From the repository root, with dependencies installed and provider credentials
configured in your local `.env`:

```bash
python -m app.model_catalog
```

Read `reports/model-catalog/review.md`. The report includes configured IDs missing
from the provider, unconfigured GPT/Claude IDs to review, additions/removals since
the last successful snapshot, and shutdown dates when the provider supplies them.
Unconfigured IDs may include specialized models and snapshots; they are discovery
candidates, not necessarily compatible Ask models. An older model is not
necessarily deprecated. Absence of a shutdown date is not a guarantee of support.

The checker follows SDK pagination and attempts to retrieve configured aliases
that are absent from the list. A missing credential or failed request produces
exit code 2 and an unverified/stale report, retaining the previous successful
snapshot. Exit code 0 means both catalogs were refreshed, not that all models
passed review. `latest.json` stores state; timestamped JSON files preserve runs.
Reports are ignored by git. Use separate output directories for different accounts.

Catalog checks run locally on demand. Provider credentials stay in your local
environment; no GitHub Actions secrets or scheduled workflow are required.

### Evaluate one candidate

```bash
python scripts/evaluate_agent_questions.py \
  --provider openai --model CANDIDATE_MODEL_ID \
  --fixture tests/fixtures/model_compatibility.yml
```

Use `--provider claude` for Anthropic. This is an explicit live evaluation: it
requires configured BigQuery access and provider credentials, and incurs normal
warehouse/model usage. The catalog check does not run inference evaluations.
A candidate does not have to be enabled in Ask to run this command.

The three questions exercise rankings, player trends, and similarity through the
existing agent. Checks cover expected tool names, expected terms, nonempty answers,
and structured top-level fields. Reports record the exact provider/model, case
failures, and elapsed seconds in separate `reports/model-evals/<timestamp>.json`
files. These are smoke tests, not proof of numeric accuracy, raw provider schema
compliance, streaming, conversation history, or cost. Manually inspect those paths
before promoting a model, then update the existing options/defaults in config.

Provider API references:
- https://developers.openai.com/api/reference/python/resources/models/methods/list
- https://platform.claude.com/docs/en/api/models/list

## Private local Ask traces

Set `AGENT_TRACE_ENABLED=true` only for local diagnosis. The default is false.
JSON and SSE Ask requests capture successes, business validation failures, rate
limits, capacity rejections, worker-start failures and execution failures when the
original socket peer is loopback, Host is localhost, no forwarding header is
present, and any Origin matches. Remote requests continue normally without
capture. Run Uvicorn with `--no-proxy-headers`; this is not authentication.
Server history retains its stricter remote-request rejection when enabled.
Do not place a same-host proxy or tunnel in front of a trace-enabled instance:
a proxy that strips forwarding headers and rewrites Host cannot be detected by
this predicate. Known proxy headers, duplicate Host/Origin and nonmatching Origin
are rejected for both private capture and server history; these checks tighten
history ingress as well. This is not remote user authentication.

`AGENT_TRACE_DIRECTORY` defaults to ignored `local_notes/ask_traces`. Each
server-generated request ID gets a unique JSON file. The final directory must
be owned by the current user and mode 0700; files are 0600. Symlink paths and
untrusted targets are refused. A process-wide nonblocking single-writer limit
and nonblocking file lock skip capture on contention. Normal telemetry remains
inside worker admission; only private persistence runs after worker completion,
with at most one such writer per process. Storage errors emit only constant
diagnostic codes and do not change the response. Do not configure a tracked/public directory
or publish these records as attachments. Ignore rules are not access control.

Limits are `AGENT_TRACE_MAX_BYTES` (2048–1048576, default 65536),
`AGENT_TRACE_MAX_RECORDS` (1–10000, default 100), and
`AGENT_TRACE_RETENTION_DAYS` (1–365, default 7). Retention runs on writes under
an exclusive file lock after a new record is successfully published, removing
expired/oldest recognized private trace files;
it is not a background deletion guarantee. Stop the server and delete the local
trace directory to purge it, including backups under your own retention policy.
Local backup and document-sync services may copy these files. Ancestor symlinks
(including macOS `/tmp` and `/var`) and non-0700 directory modes fail closed;
filesystem ACLs are not inspected.

The versioned allowlisted projection includes redacted question/context,
selected provider/model, available plan/scope/evidence identifiers, route,
outcome, static version and diagnostic source/config fingerprints. The cached
source digest snapshots on-disk `app/**/*.py` at first capture (including a cached
unavailable result); it does not prove loaded code, dependencies, templates or
other assets. The config fingerprint covers nine allowlisted fields and is not
B's replay-compatibility binding. Conversation IDs use an unsalted hash and are
pseudonymous, not anonymized; guessable client IDs can be recovered. Provider prompts, raw responses, error bodies,
headers and credentials are excluded. Nested values, text, records and counts
are bounded; recognizable secrets/email/user-ID assignments are redacted.
Recognizable local user-file paths are also redacted. Redaction is best effort,
so treat all records as private. Omitted fields,
truncation, redaction and unavailable evidence are recorded as incomplete reasons.
Hashes identify projected content; these records are not complete replay inputs.
Request-schema rejection before endpoint execution is outside this capture path.
No trace is exposed by an API or added to the public service summary.

For offline verification run `python -m pytest tests/test_private_traces.py -q`.
The tests use synthetic endpoint agents and temporary storage; they do not
establish live-provider, warehouse or deployed-ingress behavior.
