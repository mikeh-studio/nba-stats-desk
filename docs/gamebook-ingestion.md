# Automated gamebook ingestion in dev

[`nba_gamebook_dev`](../dags/nba_gamebook_dev_dag.py) is an independent Airflow
DAG for regular-season PDF gamebooks. It schedules every six hours, starts paused,
and reads the existing NBA API game-log and run-metadata tables. It has no task
dependencies on the production DAG, dbt, or Ask. All writes are
restricted by the existing publisher to `nba_gamebook_dev` or its dev-prefixed
datasets and private GCS objects. No Codex, LLM, or VLM is required at runtime.

## Source and publication contract

1. **Discover from the warehouse:** read the existing NBA API
   `raw_game_logs` table for the configured season and regular-season phase.
   Group repeated player rows into game/date/matchup/result contexts. Require a
   ten-digit `002` game ID with the matching season code, one calendar date,
   reciprocal `HOME vs. AWAY` / `AWAY @ HOME` matchups, known NBA teams, and
   opposing `W`/`L` results. Preserve leading zeros. Missing results, one-sided
   extracts, or conflicting contexts fail discovery and retain the rejected
   source records. The date and away/home abbreviations build the official PDF
   URL; a game ID alone is insufficient because gamebook URLs use date/matchup.
2. **Register and select:** register every discovered completed game in dev
   coverage, then choose at most 20 games per run (configurable up to 50). Never
   attempted games come first, then the oldest checked eligible games. Backlog is
   retained in coverage; there is no last-date watermark that skips failures.
   PDF publication is expected on the **next calendar day in America/New_York**.
   Same-day games remain in observed coverage but are excluded from the work
   queue. Discovery reports their IDs, `expected_on` dates and `not_yet_due`
   status separately from work deferred by the batch limit. This applies to
   warehouse and schedule discovery, including bounded backfills.
3. **Download:** create a new attempt directory and request the official
   `statsdmz.nba.com` PDF. Preserve status, response bytes, hashes and identity
   checks. No redirect following, access-control bypass, or silent source retry.
4. **Identify:** obtain game-specific canonical player names/IDs from NBA live
   box-score JSON, falling back to PBP Stats. Validate game/date/team scope as
   available in the provider contract. Normalize and sort names/IDs so unrelated
   provider statistics or retrieval timestamps do not create new revisions.
   Provider assist counts are never imported. Unresolved identities fail visibly;
   the automated path does not create manual rookie mappings or guess ambiguous
   abbreviations.
5. **Validate and publish:** run the existing native-text parser and local
   converter, including player/team scoring and assist reconciliation. Upload
   attempt evidence to private GCS and publish accepted game revisions atomically
   in BigQuery. The [cloud contract](gamebook-cloud.md) defines replay,
   concurrency guards, and immutable history. A failed replacement retains the
   prior accepted revision and records a failure. These checks establish
   same-document consistency, not independently labeled extraction accuracy.

Only scoring events and assist relationships are currently published. The
existing storage contract rejects overtime periods; blocks, possession timing,
other relationships, scanned PDFs and OCR remain unsupported. Unsupported games
stay visible as rejected, not zero-assist games.

## Warehouse discovery scope and freshness

Daily discovery requires the latest recorded `nba_api` run for the requested
season in `pipeline_run_log` to be successful and no older than 36 hours. A
recorded failed stats publication, missing run, malformed metadata, or stale
heartbeat fails visibly before returning work. The scheduled DAG never changes
to an older season or relaxes this check. With fresh successful upstream metadata,
zero observed games can be a valid no-work result; it is not independent proof
that the league had no games. The pipeline's run log currently records successful
completions; failures without a run-log entry are detected through heartbeat age,
not immediately by this reader.

The denominator is **completed games observed in the upstream NBA API ingestion**.
This path cannot discover games that the upstream ingestion missed, and does not
claim independent league-wide completeness. It reads no player statistical values
for assist extraction. The source query uses explicit season/phase/date parameters,
a 1 GB billed-byte cap, and a 6,001-context sentinel to detect a 6,000-context limit
rather than silently truncate results. Response rows, rejected contexts, source
and metadata table names, freshness evidence, query job IDs and the manifest are
saved privately and archived in GCS.

A bounded historical test/backfill can explicitly use `--mode backfill` with
`--start-date` and `--end-date` (at most 31 days). That mode labels freshness as not
required for a historical read, and compares existing coverage only within the
chosen window. It is not passed by the scheduled DAG. Previously registered games
cannot disappear silently within the discovery scope. Source tables are SELECT-only;
this code never refreshes, merges, or rolls over the upstream ingestion.

`GAMEBOOK_DISCOVERY_SOURCE=schedule` retains the previous explicit alternative:
NBA CDN schedule with NBA Stats fallback, requiring final status `3`. It is not an
automatic fallback for stale warehouse data. The default warehouse path makes no
schedule HTTP requests. PDF retrieval and identity retrieval still use their
existing network sources.

## Retries and corrections

The shared [publication policy](../scripts/gamebook_publication.py) uses a fixed
one-day lag: an October 21 game becomes eligible on October 22 at midnight Eastern
Time, and is picked up by a subsequent scheduled run. This is an operational
expectation, not a guaranteed NBA publication deadline or a 24-hour delay after
tipoff. Discovery does not attempt PDFs before that boundary. Explicit manual
`process` commands and retrieval diagnostics can still probe a PDF earlier.
Once due, unavailable files and timeouts remain visible failures under the retry
policy below; the lag does not clear existing failures or their history.

Each game is an independently mapped task. A failed game does not prevent other
mapped games from running. A run with a failed game remains failed; there is no
successful `all_done` leaf hiding it. A valid source observation with no eligible work
produces a skipped mapped task and a successful no-work run.

- Source failures, including delayed/404 PDFs, rate limits, timeouts and missing
  identity feeds, are audited and retried twice with Airflow exponential backoff
  starting at five minutes and capped at 30 minutes.
- Deterministic validation rejections fail immediately without repeating the same
  work within that run. They remain eligible for a later scheduled retry.
- Games in the most recent seven days are eligible for re-download every six
  hours. Failed attempts are eligible again after 24 hours, including games older
  than the correction window. Older accepted games are eligible for a weekly
  sweep. These are eligibility intervals; the per-run cap can delay processing.
- Every selected game is downloaded and validated again. Identical accepted
  revisions produce an audited no-op. Valid corrections replace the current
  pointer atomically while preserving history. Invalid corrections leave
  previously accepted facts intact.
- The publisher deliberately retains failure health after a no-op replay. A
  later valid new revision clears health; a replay does not erase prior failure
  evidence. Scheduling uses the last attempt's outcome and time, while consumers
  must continue to inspect coverage health.

The DAG allows one active run and one game worker at a time, matching the small
publisher's serialized control transaction. Use this DAG as the sole automated
writer for its dataset. Parallel manual publishers are not an endorsed operating
mode.

Before submitting each BigQuery query, the worker records the job ID in a private
GCS checkpoint under `automation_queries/`. It clears that checkpoint only after
observing success. On retry or a later run, a fresh worker checks the previous
job: terminal jobs allow a new attempt, running jobs stop immediate retries, and
missing jobs require inspection. A missing job is not assumed to mean that a
submission never reached BigQuery. Generation preconditions prevent a stale
checkpoint writer from overwriting a newer checkpoint. Never manually clear a
checkpoint while its job outcome or another worker's status is uncertain.

## Runtime configuration

The scheduler uses the repository's normal Airflow environment. Workers run in
`GAMEBOOK_PYTHON`, defaulting to `.venv-app/bin/python` when present, otherwise the
scheduler interpreter. Install the repository's Python requirements in that
worker environment, including the supported pypdf version. This avoids relying
on an older PDF library installed in an existing Airflow environment.

Configure these environment variables for the scheduler/worker:

```dotenv
GCP_PROJECT_ID=your-project
GCS_BUCKET_NAME=your-private-bucket
BQ_LOCATION=US
GAMEBOOK_DEV_DATASET=nba_gamebook_dev
GAMEBOOK_MAX_GAMES_PER_RUN=20
GAMEBOOK_DISCOVERY_SOURCE=warehouse
# Source datasets are read-only; default to the existing BQ_* settings.
# GAMEBOOK_SOURCE_BRONZE_DATASET=nba_bronze
# GAMEBOOK_SOURCE_METADATA_DATASET=nba_metadata
# Optional: otherwise the season.yml setting is used.
GAMEBOOK_SEASON=2026-27
# Optional absolute path to the worker environment's Python.
GAMEBOOK_PYTHON=/absolute/path/to/.venv-app/bin/python
```

The worker loads the local `.env` without overriding environment variables.
`GAMEBOOK_SEASON`, `GAMEBOOK_DISCOVERY_SOURCE` and `GAMEBOOK_PYTHON` are read by
Airflow, so export them into its environment; the repository Make targets already
export `.env` values.
Keep credentials outside Git. Use application-default credentials for a dev
identity permitted to read bucket metadata/IAM, create/read private objects,
submit BigQuery jobs, SELECT the source game-log/run-metadata tables, and manage
the dev destination dataset. The read-only source permissions are separate from
dev write permissions. This code does not grant roles or create a service account.

The supported initial deployment is the existing host-based Airflow setup, with
this checkout and worker environment available on the host. Artifacts stay in
ignored `reports/gamebook-automation/` and private GCS; XCom carries only bounded
game manifests and outcome summaries. Ephemeral/multi-host deployment, IAM for a
new service identity, and artifact retention need separate runtime validation.

Run a bounded discovery attempt independently of Airflow:

```bash
.venv-app/bin/python scripts/gamebook_ingestion.py discover \
  --season 2026-27 --as-of 2026-10-22T09:00:00-04:00 \
  --output reports/gamebook-automation/discovery-check-unique
```

Use the actual observation time and a new output directory. `discovery.json`
records the full discovered count, due count, selected games and deferred count.
Missing/stale upstream ingestion returns nonzero and archives its evidence. It
never substitutes the five-game manual manifest for automatic discovery.

For a bounded historical warehouse test (use the actual observation timestamp):

```bash
GAMEBOOK_DEV_DATASET=nba_gamebook_dev_warehouse GAMEBOOK_MAX_GAMES_PER_RUN=2 \
.venv-app/bin/python scripts/gamebook_ingestion.py discover \
  --discovery-source warehouse --mode backfill --season 2025-26 \
  --start-date 2025-10-22 --end-date 2025-10-22 \
  --as-of 2026-10-22T09:00:00-04:00 \
  --output reports/gamebook-automation/warehouse-check-unique
```

The returned `result.json` is the same bounded list mapped by Airflow. Run each
entry through the worker's `process --game-json <entry> --output <new-directory>`
command to test PDF retrieval and publication without enabling the scheduler.

After current-season upstream ingestion, discovery and the dev runtime succeed,
initialize the usual local Airflow metadata, unpause **only** this dev DAG, and
trigger a dev run:

```bash
make airflow-init
# Export AIRFLOW_HOME and AIRFLOW__CORE__DAGS_FOLDER as described in operations.md.
.venv-airflow/bin/python -m airflow dags unpause nba_gamebook_dev
.venv-airflow/bin/python -m airflow dags trigger nba_gamebook_dev
```

A running scheduler and available host are required for scheduled executions.
The existing `make airflow-unpause` target controls the production analytics DAG;
do not use it to enable this dev workflow. Reading upstream tables does not enable
production serving integration or change the upstream ingestion schedule.

## Validation

Run focused source, worker, storage, and Airflow graph tests:

```bash
.venv-app/bin/python -m pytest tests/test_gamebook_warehouse_discovery.py \
  tests/test_gamebook_ingestion.py \
  tests/test_gamebook_cloud.py tests/test_gamebook_storage.py \
  tests/test_gamebook_assists.py tests/test_gamebook_identity_map.py \
  tests/test_gamebook_retrieval.py -q
AIRFLOW_HOME=/tmp/gamebook-dag-tests AIRFLOW__CORE__DAGS_FOLDER="$PWD/dags" \
  .venv-airflow/bin/python -m pytest tests/test_gamebook_dag.py tests/test_dag_import.py -q
```

Execute actual Airflow task mapping and retry transitions in a **fresh isolated**
metadata directory, with synthetic HTTP and local transactional publication:

```bash
AIRFLOW_HOME="$PWD/reports/gamebook-airflow-check-unique" \
AIRFLOW__CORE__DAGS_FOLDER="$PWD/dags" AIRFLOW__CORE__LOAD_EXAMPLES=False \
  .venv-airflow/bin/python tests/gamebook_airflow_scenarios.py
```

The harness covers a delayed PDF that succeeds on retry, replay, correction,
isolated validation failure, a no-work day, and exhausted discovery retries. It
uses Airflow `dag.test()` and worker subprocesses; it does not establish a deployed
scheduler or real NBA source access. The mapping pattern follows [Airflow's
TaskFlow dynamic mapping documentation](https://airflow.apache.org/docs/apache-airflow/2.10.5/authoring-and-scheduling/dynamic-task-mapping.html).

The opt-in cloud scenario harness makes real writes to a new synthetic dev dataset:

```bash
.venv-app/bin/python tests/gamebook_automation_cloud_scenarios.py \
  --output reports/gamebook-cloud-check-unique
```

It tests the unattended worker's registration SQL, query checkpoints, replay,
correction and rejected replacement against real GCS/BigQuery using synthetic
PDF/HTTP sources. It is not a source-availability test. All detailed run results,
job IDs and response bodies remain under ignored reports and private GCS.

### Validation boundary

Synthetic worker, Airflow and cloud scenarios exercise replay, correction,
retry and failed-replacement behavior. Live source availability and complete
current-season coverage require separate bounded checks. Preserve their raw
responses and detailed run evidence in private storage. Keep the schedule paused
until daily discovery, upstream source health and a manual dev run succeed.
