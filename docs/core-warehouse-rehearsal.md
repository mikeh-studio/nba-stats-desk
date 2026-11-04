# Isolated core warehouse rehearsal

This opt-in path exercises the core profile in BigQuery using retained inputs.
It does not activate the default app, promote production data or issue source
approval. `core-warehouse-rehearsal/1` is a separate receipt with
`runtime_eligible=false`, not a production `publication-manifest/1`.

Public owns `core_dbt.py` (closed model project), `publication.py` (read-only
resolver), and `publication_application.py` (loopback app composition). Private
operator scripts own loading, dbt execution, sealing and control transactions.
No data package or dependency pin changes are required for this isolated path.

The generated dbt project contains only the 13 core models, literal staging/build
datasets and explicit dependencies. It has no legacy macros, source defaults,
hooks or optional-domain models. The writer audits its parsed manifest and exact
generated SQL before execution. All targets must be inside the caller's isolated
`nba_phase4_…` namespace. Query jobs have a 256 MiB billed-byte limit, dbt has two
threads with no retry, and test tables expire within 48 hours.

The writer claims each build by creating its dataset without `exists_ok`. It
loads five typed inputs with `WRITE_EMPTY`, compares all 13 warehouse models to
the offline expected models, then creates read-only table snapshots in a separate
release dataset. Snapshot rows, primary keys, content digests and metadata are
checked before eligibility. Failed or interrupted builds have no registry entry
and cannot be promoted. Retrying a completed allocation cannot overwrite it.

Normal single-host promotion uses the operator's file lock. The BigQuery control
transaction independently checks exactly one pointer row, expected ID/generation,
one eligible unrevoked release and current expiry. It updates that pointer and
appends the audit event together. A stable operation/job ID plus audit read-back
handles a lost commit response. A stale conflict is never retried with a new
expected pointer. Production multi-host scheduling/IAM belongs to cutover work.

`PublicationApplication` resolves the pointer once per request, or validates an
explicit `X-Publication-ID` from a prior request against its server registry. Its
cache key includes release ID, receipt digest, semantics, scope and input digest.
All Player, Compare, Performance, Ask/search and health reads use that pinned
receipt. Only the profile's asset list and typed deterministic query grammar are
available. Historical scopes are explicitly unsupported in this isolated app;
the existing default app's historical behavior is unchanged. Expired, revoked,
missing or replaced snapshot tables fail unavailable, including warm app caches.
No silent switch to another release or legacy relation occurs inside a flow.

The same source and table-expiration rules apply to rollback; rollback selects
an existing eligible snapshot set without rebuilding tables. Snapshot immutability
prevents row edits; project administrators can still delete objects, which readers
detect. This is not an IAM separation proof for a deployed reader/writer pair.

For direct SQL, first resolve the single pointer and retain the resulting ID:

```sql
SELECT active_id, generation
FROM `example-project.nba_phase4_example_control.pointer`
WHERE slot = 'main';

SELECT manifest, manifest_digest
FROM `example-project.nba_phase4_example_control.releases`
WHERE publication_id = @pinned_publication_id;
```

Validate the receipt, then use its exact immutable relation in subsequent queries:

```sql
SELECT game_id, team_abbr, opponent_team_abbr, team_pts, opponent_team_pts
FROM `example-project.nba_phase4_example_release_0123456789abcdef.fct_team_game_results`
ORDER BY game_id, team_abbr;
```

Independent SQL queries must keep the same ID themselves. Separately replaced
views are not the consistency mechanism. Ask accepts typed operations, not this
arbitrary SQL. No unscoped repository or metadata fallback is enabled.

The implementation follows BigQuery's [transaction constraints](https://docs.cloud.google.com/bigquery/docs/transactions),
[read-only table snapshots](https://docs.cloud.google.com/bigquery/docs/table-snapshots-intro),
and [query cost controls](https://docs.cloud.google.com/bigquery/docs/best-practices-costs).
Physical builds and actual concurrency tests are recorded in the private/local
execution report; synthetic tests alone do not establish warehouse acceptance.
