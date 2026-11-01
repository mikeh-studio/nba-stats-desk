# Offline observed PTS snapshots

`app.serving` is an explicit local artifact reader. Importing it does not select a
source or change application factories, warehouse policy, caches, routing or UI.
A validated artifact is **not runtime eligible** and grants **no cohort claim**.
This version supports only an exact, explicitly declared 2025–26 Regular Season
date range, PTS total and PTS average. Rankings, percentiles, qualification,
other metrics, seasons, phases and derived windows are unavailable.

## Wire and identity

The public schema is
[`snapshot-v1.schema.json`](../contracts/shared-serving/snapshot-v1.schema.json).
The reader enforces its closed shapes and the additional cross-field rules below
without requiring schema resources in an installed package. A conformance test
checks the schema and reader against the same literal synthetic wire fixture.
The installed reader also requires the existing `app.agent.semantics` Python
modules and `semantic_contract.yml` resource; its arithmetic remains authoritative.

An artifact directory contains `manifest.json` and one or more
`facts/NNNNNN.json` shards. Files are canonical JSON encoded as UTF-8 using
`json.dumps(sort_keys=True, separators=(',', ':'), ensure_ascii=False,
allow_nan=False)`, without a trailing newline. Duplicate object keys,
noncanonical bytes and nonfinite numbers reject. SHA-256 digests are lowercase
64-character hexadecimal strings. `snapshot_id` hashes the canonical manifest
excluding **only** `snapshot_id`. Shard digests hash the exact canonical bytes.
The manifest includes:

- `schema: nba_shared_serving_snapshot/1`,
  `projection_version: affirmative-played-pts/2`;
- the exact PTS semantic block and its `metric_semantics_id` digest;
- `scope`: season, phase, start/end dates and explicit timezone-aware `as_of`
  and `cutoff`; the selected dates must lie within 2025-10-01 through 2026-06-30,
  end on or before `as_of`, and `as_of` must be on or before `cutoff`;
  `as_of` is the query anchor and `cutoff` is the selection/evidence knowledge
  cutoff, never a certified data-through date or completeness assertion;
- `dependency_ids`: sorted unique full final dependency closure (at most 512
  digests), including every lineage root and nested source/identity/selection/
  coverage/PDF capability dependency;
- six pinned lineage digests: source inventory, identity, selection, policy,
  coverage and final capability;
- original source, discovery, revalidation and review clocks, optional hard
  expiry and `freshness_policy_id: historical-integrity/1`;
- exact observed-only capabilities, shard byte/row counts, exact per-shard
  date bounds and overall row count.

The source inventory and policy digests bind the reviewed versioned source and
participation normalization contracts. Final capability binds the final
post-check dependency closure; a pre-check coverage digest alone is insufficient.
The caller supplies those trust roots independently. Hash equality proves bytes,
not the truth or completeness of source, selection or identity evidence.

Attempt timestamps, output paths, package/code hashes and whole-catalog
provenance belong in separate build receipts. They are not manifest fields.
Refactoring code while preserving the same reviewed projection and canonical
content preserves snapshot identity. Changed facts, bounds, scope, semantics or
lineage change identity. Historical artifacts and receipts remain immutable.

Each shard is an array of closed row objects with `season`, `phase`, `game_id`,
`player_id`, `team_id`, `game_date`, `played`, `pts`, `minutes`. Player IDs are
positive canonical integer NBA IDs; game and game-time team IDs are bounded
strings. Rows are globally sorted by `(season, phase, game_id, player_id)` and
that key is unique across shards. Source aliases must already be resolved.
Shards are ordered by unique path. Duplicates, nonaffirmative participation,
inconsistent counts, hashes, bounds or scopes reject the entire read.

`played` must be exactly `true`. A known played zero-minute appearance counts.
Minutes never prove participation. PTS is null or a finite nonnegative integer
at most 1,000,000; booleans, fractions, strings and negative values reject.
Minutes are nullable metadata: unknown, unverified or held minutes must be
exported as null. Known values are finite nonnegative numbers at most 1,000.
Projection version 2 preserves these metadata meanings; earlier unselected
projection version 1 is unsupported by this reader. Neither changes the PTS
semantic definition or infers participation from minutes. DNPs are absent
from facts and remain accounted for by producer membership audits. Unknown or
conflicting participation must be held before export; filtering it out does not
prove a complete input set.

## Semantics and arithmetic

The exact supported semantic definition is exposed as `pts_semantics()` in
`app.serving.contract` and as a schema constant. Its rules mean:

- `affirmative_played_canonical_player_game/1`: one resolved affirmative
  appearance per canonical key, independent of minutes;
- `missing_component_is_unknown/1`: null contributes to observed count but
  excludes the appearance from valid PTS count;
- `finite_nonnegative_integer_count/1`: zero is valid; invalid counts reject;
- `selected_pts_sum_or_valid_appearance_average/1`: total sums valid PTS;
  average divides that sum by valid PTS appearances.

The reader requires exact semantic block and fingerprint agreement with its
local reviewed definition, then checks the installed public PTS metric before
calling existing `aggregate()` and `load_contract()`. There is no executable
formula supplied by the artifact and no complete `Evidence` wrapper. Display
rounding follows public `aggregate()` (one decimal). `[0, 10, null]` produces
observed=3, valid=2, missing=1, numerator=10, total=10, average=5. Total has no
denominator; average has denominator=2. With no valid values, value, numerator
and denominator are null. No minimum-game or league completeness follows.

## Trusted permission and clocks

Compose `OfflineReadPolicy(authority_provider=..., clock=...)` explicitly.
`authority_provider()` returns a trusted `FactReadStatus` with:

- schema `shared-serving-fact-status/1`, opaque reviewed `status_id` digest;
- aware `checked_at` and `valid_until` datetimes;
- explicit `allowed_snapshot_ids` as a `frozenset` of digest strings and
  `dependency_states` as a mapping of digest strings to state strings keyed by every
  required `dependency_ids` entry, including nested dependencies.

Only `active` or explicitly retained `superseded` dependencies allow a read.
Missing, unknown and revoked dependencies deny. Removing a snapshot from the
current allowlist denies it. Provider exceptions fail closed. The reader never
loads status from snapshot bytes, discovers a registry or reads environment
configuration. Status IDs are trusted references, not self-approval digests.
The default status maximum age is one day, independently of its valid-until
lease; a trusted caller may tighten budgets or change this duration explicitly.

The trusted clock must be aware. On open and **every operation**, including
metadata, repeated reads and diagnostic emission, the reader rechecks current
status, allowlist, dependency states and hard expiry. Diagnostic
`freshness_checked_at` records the exact UTC instant used for the final permission
decision, without another clock read. A pinned handle retains
identity, not permission. Historical `as_of` filters never move this clock.
Unknown (`null`) original source capture time adds
`source_capture_clock_unknown`; unknown discovery or revalidation time adds
`discovery_or_revalidation_clock_unknown`. These limitations distinguish missing
capture evidence from missing review metadata. Review time is required; known
evidence clocks and the requested `as_of`/knowledge `cutoff` cannot be in the
future relative to the trusted access clock. Historical-integrity validation
makes no current-season freshness claim and imposes no fabricated short capture
TTL. A hard expiry, when present, must follow review and denies at the deadline.
This mechanism depends on the caller providing current reviewed status; it does
not independently discover upstream corrections.

## API and bounds

`SnapshotHandle.open(absolute_root, policy)` validates the artifact before
returning a handle. `describe()`, `capabilities()` and `read()` return fresh
copies. `diagnostic(player_id, aggregation, *, scope=None, metric_id='pts')`
returns public aggregate fields plus exact identity, scope, status and explicit
observed-only limitations. An omitted scope means the manifest's exact scope;
a supplied scope must match all its fields. Unsupported requested semantics
raise `ServingError` with `code='capability_not_evaluated'`. Other denials have
bounded codes; there is no fallback to another source or a subset of shards.

Default limits are 1 MiB manifest, 50 MiB per shard, 100 MiB total shard bytes,
16 shards and 100,000 rows. All path components are opened relative to directory
file descriptors with symlink following disabled, including artifact ancestors.
Use a physical absolute path (`Path.resolve()` for a trusted root on systems
where `/var` or `/tmp` is a symlink). Only regular files are accepted; named
pipes cannot block a read. Bounded bytes are read once per operation and
verified before parsing/consumption. Each subsequent operation verifies bytes
again, including the manifest pinned at open. Concurrent mutation or incomplete
publication yields refusal; producers must publish complete immutable directories
atomically. No registry, background watcher, active pointer or new service exists.

Synthetic conformance exercises integrity and refusal behavior. It establishes
neither retained-source completeness nor real selection/identity/cohort,
installed application, browser, provider or deployment acceptance.
