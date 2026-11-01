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

## Historical PTS shadow adapters (Phase 2)

These explicitly constructed adapters remain offline and unregistered. They are
not installed application endpoints or a replacement WarehouseRepository. Import
`ReadContext` and `PTSShadowRequest` from `app.serving.context`,
`HistoricalPTSAskShadow` from `app.agent.snapshot_source`, and `PlayerPTSShadow`
from `app.serving.player`. Keeping the narrow Player adapter here avoids importing
the warehouse repository package and its composition dependencies.

Bind a handle with an independently expected snapshot ID and its exact reviewed
scope. The frozen context pins projection/metric semantics, all lineage roots,
full dependency closure, observed-only capabilities and an adapter version in a
content-derived context ID. Construct an explicit request with
`PTSShadowRequest.create(canonical_player_id, 'average', scope=scope)`. This is a
deterministic plan interface; it makes no natural-language planning claim.

`answer()`, `answer_json()` and terminal-only `answer_sse()` emit the Ask PTS
table; `get_pts()` emits the Player PTS section and game log. Both include total
and average arithmetic, components, numerator/denominator, observed/valid/missing
counts and game membership in their bound evidence. Display averages follow
public rounding. A missing canonical ID refuses with `player_not_observed`; it
never becomes a zero-game season. Null PTS and valid zero remain distinct.
Names, full profiles, league context and unsupported metrics/windows are absent.
The selected historical range describes observed facts, not a complete-through
assertion. No legacy SQL, warehouse, model, provider or cache is consulted.

Each operation checks current read authority and immutable content; adapters
check again after claim validation and before JSON/SSE emission. The lazy SSE
iterator emits one `final` event only, with no unverified intermediate prose.
This envelope is an offline protocol test, not existing-route/browser acceptance.
Content may stay pinned while status IDs and check times advance. Revocation,
expiry or artifact replacement prevents output from an already-bound context.
Already-returned payloads are inert historical results, not reusable permissions.

The new `observed_pts_shadow` presentation contract extends the existing public
claim validator through its context registry. It binds exact destinations and
fixed labels to the observed result, identity and full context. Existing complete
`Evidence`, `run_query`, source policies, trusted runtime boundaries and claim
rollout defaults are unchanged. No incomplete observation is relabeled complete.
Claim validation establishes presentation fidelity; it does not independently
certify source selection, identity, cohort completeness or current authority.
The private installed harness supplies the independent arithmetic/membership
check relative to its explicitly retained trust inputs.

The shadow path obtains both aggregates and selected game rows in one verified
read (`observed_pts_bundle`). `authorized_snapshot()` returns a manifest and the
permission receipt from one verification pass; it never caches permission.
Regression checks cap full verification work per emitted consumer result while
preserving checks after formatting/serialization and before SSE emission.
Original evidence clocks travel in the hashed evidence; their exact ordered
limitations are derived from those clocks, never from build time.

A shadow `authority` receipt records the verification pass immediately before
formatting, not the final emission instant. Each later permission check must
retain that same status ID and checked-at lease; an allowed status rotation
refuses with `authority_changed` so a caller can explicitly retry. A returned
receipt is inert and does not grant future access. Malformed shadow presentation
fails with the bounded `claim_binding_mismatch` code.

## Historical PTS comparison shadow (Phase 3 first increment)

`app.serving.comparison.HistoricalPTSComparisonShadow` takes one existing
`ReadContext` and a `PTSComparisonRequest` for two distinct canonical player IDs,
`total` or `average`, and the artifact's exact scope. `answer()`, `answer_json()`
and terminal-only `answer_sse()` emit one closed side-by-side table. The adapter
is offline and unregistered; existing routes, UI, factories and guards are intact.

The explicit presentation policy is `observed_pts_side_by_side/1`. It reuses the
public `aggregate()` arithmetic and the existing comparison presentation rule:
partial averages retain valid/observed/missing counts, while unsupported changes,
edges, winner statements and league context are withheld. No difference or edge
is emitted even for complete inputs; existing sample/edge qualification rules
are not replaced by a new threshold. Each side retains its own canonical game
membership and game-time team IDs. An all-null observed sample remains a null
value with its counts; a known zero remains zero. An absent requested player
refuses the whole comparison with `player_not_observed`, never a one-sided table.

Both sides bind the same snapshot, scope, semantic identity, dependency closure,
original evidence clocks and current authority receipt. Public claim validation
binds every table value and label to those sides under a separate closed
`observed_pts_comparison_shadow` context. It checks presentation fidelity, not
independent source truth. Permission is rechecked after rendering, serialization
and before terminal SSE emission. Output is bounded to 1 MiB; overflow refuses
without dropping rows. An already-returned result grants no later permission.

Only the historical artifact contract is supported. Current-season readiness
assessment does not activate current-season serving. New metrics, arbitrary
windows, names, complete profiles, pooled seasons, league rankings and runtime
integration remain separately reviewed work. Synthetic tests prove this narrow
protocol, not endpoint/browser or complete-source acceptance.


## Explicit current-season offline profile

`CurrentSnapshotHandle` / `CurrentReadPolicy` / `CurrentReadContext` opt into
`nba_current_pts_snapshot/1` for exactly 2026–27 Regular Season. The historical
schema, contexts and adapters continue to refuse it. No application factory,
source guard, route or runtime mode is changed. This profile does not combine
seasons or grant complete-cohort, ranking or percentile claims.

Trusted composition independently supplies the allowed snapshot/status plus the
approved freshness-policy fingerprint and source/identity authority ID. Manifest
hashes do not approve their own authority. Lineage also binds the exact primary
coverage, mapping, conflict-history and freshness-evidence roots. The policy
fingerprint identifies the supported producer's frozen evaluator environment;
the reader does not implement a competing freshness formula.

The artifact carries original capture, discovery and verified-revalidation clocks,
selection-time evaluation, an inclusive `fresh_through` and the first stale
microsecond. The producer derives that transition by querying the existing frozen
evaluator with fixed evidence. At every read, cached access and emission, external
permission and immutable expiry are checked again. Status renewal cannot extend
source freshness. Discovery's eight-day warning remains nonblocking; the ten-day
hard limit and correction deadlines remain effective. Bounds use elapsed UTC time
across DST, preserving strict greater-than comparisons in the frozen rules.

`CurrentPTSAskShadow`, `CurrentPlayerPTSShadow` and
`CurrentPTSComparisonShadow` share the same bound scope and public PTS arithmetic,
null/zero/appearance rules and claim validator. Payloads have explicit current
schema IDs; none is registered with normal application routes. Comparison remains
side-by-side observed samples with all-or-refuse membership and no inferred edge.
PDF corroboration is optional and explicitly uncertified; recorded applicable
conflicts remain holds relative to the caller-pinned ledger. A known legacy
same-batch PDF supersession interpretation remains deferred and does not represent
independent historical-conflict completeness.
