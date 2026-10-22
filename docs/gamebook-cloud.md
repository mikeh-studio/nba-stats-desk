# Gamebook dev storage in GCS and BigQuery

[`scripts/gamebook_cloud.py`](../scripts/gamebook_cloud.py) publishes the local
gamebook contract to an isolated BigQuery dev dataset and private GCS objects.
It does not modify the production bronze/silver/gold datasets, the active season,
Airflow, dbt or Ask. The [local storage guide](gamebook-storage.md) defines the
event/relationship grain and the same-document validation boundary.

The separate [automated dev ingestion DAG](gamebook-ingestion.md) now wraps this
publisher with read-only warehouse discovery, PDF/identity retrieval, per-game retries and
correction sweeps. It starts paused and does not connect the production DAG or Ask.

## Cloud contracts

[`contracts/gamebook_bigquery.json`](../contracts/gamebook_bigquery.json) provides
explicit BigQuery schemas. Dates use `DATE`, conversion/publication times use
`TIMESTAMP`, IDs use `STRING`, and the SQLite JSON-string fields remain strings.
The seven logical tables and four current/historical views retain their local
names. `gamebook_store_control` is an additional technical table whose single
row is mutated by each publication transaction to serialize competing writers.
The expected previous revision is checked within that transaction.

GCS objects live under `gamebooks/dev/<dataset>/`. Each object name contains its
SHA-256 hash. Uploads use a create-only generation precondition and explicit
private ACLs when uniform bucket access is disabled. Existing objects are read
at their recorded generation and checked against the expected hash. The uploader
rejects a bucket with public IAM bindings; it does not change bucket/project IAM.
Source snapshots, PDFs, identities, failed-input payloads, metadata bundles and
candidate JSONL remain separate immutable objects. Local `uploads.json` records
their URIs, generations, hashes and byte counts.

Candidate tables have unique attempt names and 24-hour expiration. Loads use
explicit schemas, zero tolerated bad records and `WRITE_EMPTY`. Publication
asserts scope, counts, unique event/relationship keys and valid joins before
inserting facts, recording history and advancing the current pointer in one
BigQuery transaction. Statement failure rolls that transaction back. Confirmed
failures are audited separately; unknown query outcomes retain their job IDs
and abort without claiming a failed or successful publication. Inspect the job
before retrying an uncertain commit.

Identical accepted revisions avoid new load jobs: the converter revalidates
the frozen input, and a transaction verifies the current revision/payload hash
and records a no-op. It preserves any previous failure state. Historical
revisions cannot silently replace newer revisions. All queries have a 1 GB
maximum-bytes-billed limit per query job; inspect `jobs.json` for actual jobs.
The single-row control serializes this small dev publisher; production capacity,
concurrent retry policy and service-account permissions still need runtime tests.

These mechanics follow Google's [transaction documentation](https://cloud.google.com/bigquery/docs/transactions)
and [GCS preconditions](https://cloud.google.com/storage/docs/request-preconditions).

## Running a bounded dev batch

Use the configured application environment and application-default credentials.
The script reads `GCP_PROJECT_ID`, `GCS_BUCKET_NAME` and `BQ_LOCATION` from the
environment or local `.env`. Output must be a new directory inside `reports/`.
Dataset names are restricted to `nba_gamebook_dev` or a suffix of that prefix;
existing datasets must carry the `purpose=gamebook-dev` label and expected schemas.

```sh
.venv-app/bin/python scripts/gamebook_cloud.py \
  --snapshot reports/gamebook-pilot/<pairs-run>/snapshot.json \
  --expected-games reports/gamebook-pilot/<manifest>.json \
  --identity-dir reports/gamebook-pilot/<identity-directory> \
  --capture reports/gamebook-pilot/<access-run> \
  --dataset nba_gamebook_dev \
  --output reports/gamebook-pilot/<new-cloud-run>
```

`--capture` archives retrieval metadata after verifying the frozen manifest.
Identity metadata files are included when present. Repeat `--supporting-evidence`
with explicit private files for reviewed identity mappings, original reference
snapshots or discovery provenance. No arbitrary directory is uploaded wholesale.
The script returns nonzero when any scoped game is rejected/failed/pending,
even if other games publish successfully. Complete per-game publication prevents
partial assisted-basket counts from entering serving views.

`published.json` uses one BigQuery statement to snapshot all facts, current pair
results and coverage together and archives that report under `report_snapshots/`
in GCS. Saved reports preserve this membership. Consumers
must filter season, phase and expected-game scope and disclose unavailable games;
the dev dataset may contain both historical development and current-season games.

## Canonical identities and current-season expansion

[`scripts/fetch_gamebook_identities.py`](../scripts/fetch_gamebook_identities.py)
captures game-specific PBP Stats references with status and hash metadata. Only
identity fields are used by the PDF parser; provider statistics are not merged.
Timeouts/HTTP errors remain failed attempts.

When that provider is unavailable,
[`scripts/build_gamebook_identity_map.py`](../scripts/build_gamebook_identity_map.py)
can build an explicitly identity-only map from a frozen warehouse name/ID
reference plus reviewed official NBA player-page mappings. Teams and game scope
come from PDF participant rows, not the reference's potentially old team data.
Mappings require a unique exact normalized full-name match; there is no fuzzy
matching or inferred rookie ID. The supplement records the official player URL,
reviewer and review time and remains private. Its source hashes are preserved.

This fallback is not an independent assist-label dataset or a fully automated
identity-refresh service. Ambiguous play abbreviations still reject a game even
when both players' canonical IDs are known. Do not resolve such ambiguity merely
by fitting aggregate box totals. A current-season development expansion also does
not replace the separate untouched, independently labeled final evaluation.

## Validation

```sh
.venv-app/bin/python -m pytest \
  tests/test_gamebook_cloud.py tests/test_gamebook_identity_map.py \
  tests/test_gamebook_storage.py tests/test_gamebook_assists.py \
  tests/test_gamebook_retrieval.py -q
```

The explicit live harness creates a fresh synthetic-only dataset:

```sh
.venv-app/bin/python tests/gamebook_cloud_scenarios.py \
  --output reports/gamebook-pilot/<new-live-test-run>
```

It verifies initial publication, identical replay, a corrected game, a rejected
replacement, an injected error after publication writes, replay after failure
and a stale publisher. Fault injection is restricted to `nba_gamebook_dev_test_*`.
Real source data and invented corrections never share a dataset. The harness
retains snapshots, query IDs and a success marker only after every assertion
passes. Synthetic datasets/immutable audit objects are retained for review;
only candidate tables expire automatically.
