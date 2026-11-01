# Offline ledger contracts and deterministic source selection

This Phase 2 increment reads a retained Phase 1 observation snapshot and writes
an independent, append-only decision ledger. It neither changes the observation
store nor registers a serving source. Ask/Player readers, source guards, model
limits, warehouse tables and scheduled jobs retain their existing behavior.

## Contracts

The [ledger schema](../contracts/unified_data/ledger.schema.json) defines a bounded
envelope containing a version, kind, sorted dependency IDs, payload and content
hash. Dependency IDs and the payload participate in the hash. There is no replay
or build clock in the record ID.

| Kind | Contents and interpretation |
| --- | --- |
| policy | Exact versioned selection policy; one primary provider per season/phase |
| inventory | Observed source references, exact source-file hash, identity catalog version, fact/observation IDs, original capture/review clocks and unresolved inputs |
| selection | Requested ranges, explicit selection time, policy/inventory pins, selected/excluded references, diagnostic counts, reasons and conservative capabilities |
| pending_check | Inert scope/selection/observation bindings for reconciliation, coverage or PDF work; state is only not_attempted and capability_granted is always false |

The inventory explicitly says `expected_universe: not_established`. An observed
source inventory is not an authoritative expected-game inventory or a participant
completeness attestation. Capture clocks remain original; unknown clocks remain
null. Selection time is a reproducibility input, not acquisition/publication time
or a historical knowledge-time claim.

Pending checks deliberately cannot encode a passed reconciliation, complete
coverage or matched PDF. Evaluated check contracts, authority evidence and their
component dependencies are implemented in the following
[offline reconciliation increment](unified-reconciliation.md). They use separate
versioned records rather than modifying a pending record in place.

## Selection policy

The [local policy](../contracts/unified_data/source-selection-policy.json) implements
the Phase 0 `single_provider_per_season_phase` decision:

- 2025–26 regular season: NBA archive observations.
- 2026–27 regular season: ESPN observations.
- No automatic fallback, mixed-provider game filling, field mixing or provider
  switch based on availability, apparent coverage or a later timestamp.

The schema pins this policy version and its exact permitted entries. The CLI has
no provider override, browser input or environment-selected source. Unsupported
seasons/phases and invalid ranges reject. Administrative season bounds validate
request routing only; they do not declare July 1 the competition opening date.

Selection is deterministic over the pinned inventory, policy, canonical request
scopes and explicit timezone-aware selection time. Requests are sorted and clock
offsets normalized. Capture/review evidence later than selection time refuses
that scope; unknown source clocks never become fresh clocks. These retrospective
facts do not support historical "as known at" reconstruction.

| Outcome | Meaning |
| --- | --- |
| selected_observed | Primary-provider references are selected for offline inspection; coverage has not been evaluated |
| held | Relevant unresolved input or an unlinked overlapping game needs review; references remain diagnostic |
| refused | Required primary-source observations are missing or selected evidence postdates selection time; no references are selected for that scope |

A missing current source refuses a combined historical/current request instead
of silently returning just the historical scope. Scope diagnostics remain in the
record. Any held/refused scope determines the combined status; consumers must
check that status and capabilities. These records authorize no runtime reads.

Potential cross-provider overlaps on identical game scope but different UIDs are
held, never automatically merged. Verified aliases sharing a UID select one
provider once. Duplicate provider/native/canonical game references reject.
Unresolved inputs conservatively affect their season/phase; a rejected input's
possibly incorrect date cannot justify excluding it from a requested range.

`validated_cutoff` stays null. Observed first/last dates and row counts do not
prove contiguous dates, captured no-game dates, missing-player absence, complete
season coverage or freshness. Runtime, league-rank, percentile and PDF flags stay
false. Source selection is separate from reconciliation and claim eligibility.

## Preservation and integrity

`app/data/unified/snapshot.py` opens the source using SQLite `mode=ro`, query-only
mode and a read transaction. It never invokes the writer's initializer, migrates
the source or changes its permissions. Live WAL inputs are refused: first retain
a checkpointed source through a separate workflow. Source-file hashes are checked
before/after the snapshot. Concurrent source changes cause failure.

The reader validates current fact/observation hashes, identity/scope bindings,
native aliases, current player mappings, normalized schemas and quarantine
resolution evidence. Missing or corrupt facts fail before a ledger is created;
another provider is not used to hide the failure. Source-file hashes bind the
retained snapshot, not independent upstream authenticity or completeness.

`app/data/unified/ledger.py` uses a different SQLite database with its own metadata
and rejects foreign databases. Updates/deletes are prohibited for records and
dependency edges. Append batches are transactional. Reads verify record hashes,
dependency integrity and deterministic decision/check bindings, including through
dependencies. Identical replay adds no records. Prior decisions remain available
when a new inventory, identity catalog, policy, request or clock creates a new ID.

There is no active-release pointer, cloud writer, credential loader or runtime
registration. Diagnostic JSON exports occur after commit; recover export failure
by replaying the same inputs/ledger into a new output directory.

## Run

Keep source snapshots, ledgers and reports private. The ledger and a new output
directory must be under ignored `reports/`. The source, ledger and output are
separate paths. Example dates describe an explicit observed range, not certified
season boundaries or a validated current cutoff:

```sh
PYTHON_DOTENV_DISABLED=1 python scripts/select_unified_sources.py \
  --source reports/retained-observations/registry.sqlite \
  --ledger reports/source-decisions/ledger.sqlite \
  --output reports/source-decisions/first \
  --selection-as-of 2026-10-30T04:46:40+00:00 \
  --scope 2025-26 2025-10-01 2026-06-30 \
  --scope 2026-27 2026-10-20 2026-10-27
```

The CLI exports `records.jsonl`, `selection.json` and `result.json`. Exit 0 means
observed selection succeeded, not a certified refresh/publication. Held/refused
selection exits 2. Structural/integrity failures raise an error without falling
back. Each requested scope receives explicit pending reconciliation, coverage and
PDF records; no PDF acquisition or comparison occurs in this increment.
