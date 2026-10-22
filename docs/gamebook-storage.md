# Local gamebook storage

[`scripts/gamebook_store.py`](../scripts/gamebook_store.py) converts frozen
[parser outputs](gamebook-retrieval.md#offline-assist-extraction-and-pair-report)
to a local SQLite reference store and versioned JSONL exports. It makes no network
requests and does not connect to Airflow, GCS, BigQuery, dbt or Ask. The executable
schema is [`contracts/gamebook_storage.sql`](../contracts/gamebook_storage.sql).
SQLite exercises the publication contract locally; its locking, foreign keys and
transaction tests do not establish BigQuery publication behavior.

## Tables and grain

| Table | Grain and role |
| --- | --- |
| `gamebook_documents` | Exact PDF byte hash; immutable artifact path and byte count |
| `gamebook_revisions` | Game/extraction revision; source, parser, identity and payload hashes, counts and validation scope |
| `gamebook_extraction_runs` | Processing attempt; expected previous revision, input evidence, timestamps, outcome and failure detail |
| `gamebook_events` | Game/revision/event; scoring action, scorer, points and PDF evidence locator |
| `event_relationships` | Game/revision/event/type; directed passer-to-scorer assist |
| `gamebook_coverage` | Expected game/source/domain; one current revision, publication time, latest attempt and persistent failure state |
| `gamebook_publication_history` | Committed revision transition, previous/new revision, reason, attempt and publication time |

An extraction revision is not a processing attempt. Identical replay adds an
attempt, but no document, revision, event, relationship or publication-history
rows. An existing revision with changed output is rejected. Old revisions remain
queryable and cannot silently replace a newer revision; an explicit rollback
workflow is not implemented.

`assist_pair_game` derives one row per game/revision/team/passer/scorer. Its
`assists` counts directed links; `assisted_field_goal_points` sums the linked
baskets' points. `current_assist_pair_game`, `current_gamebook_events` and
`current_event_relationships` join to coverage's sole current revision.
Historical base tables must not be summed across revisions to answer current
questions. Pair rows with zero relationships are absent; coverage distinguishes
a complete-game zero from an unavailable game.

Game/player IDs are strings. Team abbreviations describe the event-time team;
canonical NBA team-ID mapping remains a warehouse integration task. Phase is
explicitly Regular Season. Clocks retain their original text and numeric seconds
remaining in the quarter, including fractional seconds. They are not timestamps
or possession durations. Evidence uses one-based PDF pages, zero-based source
rows and top-left PDF-point bounding boxes.

Version 1 accepts complete four-quarter games and the existing parser's made
field goals/free throws. Only assists are published as relationships. Overtime,
other phases, blocks, steals and complete possession reconstruction are not
supported. All retained PBP rows remain in the immutable extraction payloads
for future parser work.

## Execution

Use the frozen manifest independently of extracted games: games without a result
are registered with a null current revision and pending health.

```sh
.venv-app/bin/python scripts/gamebook_store.py \
  --snapshot reports/gamebook-pilot/<pairs-run>/snapshot.json \
  --expected-games reports/gamebook-pilot/development-manifest.json \
  --identity-dir reports/gamebook-pilot/identity-snapshots \
  --store reports/gamebook-pilot/<local-store>
```

Repeat the same command to exercise replay. The store must be inside ignored
`reports/`; the CLI never overwrites an export directory. Exit code 0 means all
attempts published or were no-ops and all registered games are healthy. Rejected,
failed, conflicting or pending coverage returns a nonzero exit. A no-op after a
failure keeps the failure visible and also returns nonzero. Invalid batch scope
or unreadable input aborts rather than claiming successful conversion.

The store contains:

```text
store.sqlite3
documents/<hash>.pdf
identities/<hash>.json
inputs/<hash>.json          # complete per-game payloads, including rejected results
snapshots/<hash>.json       # exact original CLI snapshot
manifests/<hash>.json       # exact expected-game manifests
exports/<unique-run>/
  <table-or-pair-view>.jsonl
  schema.sql
  schemas.json
  pair_report.json
  manifest.json
```

Document and identity bytes are hash-verified before acceptance. Artifact files
are staged then atomically installed under create-only content-hash names; an
existing file with different bytes fails closed. Failed attempts may leave
unreferenced immutable artifacts; no automatic deletion policy is enabled.
Keep the store directory with its exports: evidence paths in JSONL are relative
to the store root, not the export directory. This is private local storage, not
a remote backup or a configured access-control system.

`schema.sql` is SQLite DDL. `schemas.json` is SQLite column metadata, not a
BigQuery schema API payload. JSONL preserves string IDs and explicit nulls;
`bbox_json` and `errors_json` are JSON-encoded string columns. Production BigQuery
DDL, type mapping and dbt serving models remain to be implemented and validated.

## Validation and publication

The converter checks source hashes and extraction-revision identity, scope,
event keys, evidence bounds, clock units, period boundaries, the assist subset,
running scores, and player/team box totals. It re-derives scoring actions from
the retained PBP rows and rejects missing or extra events. It does not independently
re-extract the PDF or establish labeled precision/recall. Revision metadata says
`same_document_reconciled` and `independent_review_status=not_run` explicitly.

Publication uses one SQLite write transaction to insert candidate facts, the
successful attempt, publication history and the coverage pointer. The expected
previous revision is checked while holding the write lock. A stale publisher
cannot replace a newer revision. An exception rolls back the entire candidate
transaction; a separate failed attempt is then recorded without changing current
facts. Artifact/audit-storage failure itself can abort the operation; no serving
success is claimed when the audit cannot be written.

Health and availability are separate: a game can retain an accepted revision
while `health=failed`. `last_failure_attempt_id` remains an audit reference after
recovery; only a successfully published new revision restores healthy status.
Replaying accepted data does not prove the latest failed replacement was fixed.

Export uses one read transaction for table data, pair results and coverage.
`pair_report.json` freezes the exact game revisions and pair rows, with a content
hash identifying that report. `manifest.json` is written last and records table
row counts and file hashes; a directory without this marker is incomplete. Saved
reports do not change when a later revision is published.

Attempt timestamps record local conversion, not original retrieval, extraction,
game completion or NBA publication time. Original retrieval metadata remains in
the retrieval capture; unknown source timestamps must not be inferred from these
local timestamps when integrating the warehouse.

## Tests

```sh
.venv-app/bin/python -m pytest \
  tests/test_gamebook_storage.py tests/test_gamebook_assists.py \
  tests/test_gamebook_retrieval.py -q
.venv-app/bin/python tests/gamebook_storage_scenarios.py \
  --output reports/gamebook-pilot/<new-synthetic-scenario-run>
```

The standalone exercise writes synthetic PDFs, a separate database, step-by-step
JSONL exports and a report. An original assisted three is corrected to an
unassisted three. A replacement with inconsistent box assists is rejected. The
original report remains pinned, and replay after failure preserves failure
visibility. These are simulated corrections, not changes to actual NBA data.

Additional tests exercise rollback after pointer/history writes, retry after
rollback, stale publishers, changed parser revisions against the same PDF,
invalid candidates, missing-game recovery, evidence hashes and clock units.
The existing core pipeline is unchanged. These local checks do not establish
cloud runtime access, warehouse transactions, independent labels or actual
Ask/report integration. For the separately implemented dev cloud publisher and
its live test harness, see [GCS and BigQuery gamebook storage](gamebook-cloud.md).
