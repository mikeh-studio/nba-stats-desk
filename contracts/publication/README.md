# Core statistics publication contracts

## Rolling v2

`publication-manifest-v2.schema.json`, `publication-binding-v2.schema.json`, and
`publication-status-v2.schema.json` introduce `core_stats_weekly_v1`. The shared
validator also checks exact 2026–27 Regular Season bounds and cutoff consistency.
V1 remains unchanged. Matching manifest/binding versions are mandatory. Profiles
have identical fields/metrics/dependencies but different explicitly bound scopes.
A structurally valid document is not source authority or release approval; see
the [publication reader guide](../../docs/core-production-preparation.md) for the
separate production admission path.

## Fixed-window v1 rehearsal

The isolated BigQuery protocol has a separate `core-warehouse-rehearsal/1`
receipt, described in [the warehouse rehearsal guide](../../docs/core-warehouse-rehearsal.md).
It carries `runtime_eligible=false` and cannot substitute for production source
authority. It supports actual warehouse consistency tests without relabeling
retained model validation as production source approval.

The v1 shared contracts define a bounded October 20–31, 2026 Regular Season rehearsal profile. They do not activate a publisher, modify current app routing or establish source authority.

- `core-stats-v1.json`: fixed scope, required asset fields/dependencies, enabled/disabled capabilities and offline freshness policy.
- `publication-manifest.schema.json`: sealed operator manifest structure. Real instances can contain private evidence references and must stay outside Git and browser responses.
- `publication-binding.schema.json`: immutable relation/capability binding with all offered season/phase routes. The current profile owns only 2026–27 Regular Season; other scopes retain explicit legacy or unsupported routing.
- `publication-status.schema.json`: narrow public allowlist, excluding evidence/storage references and internal versions.
- `metric-record.schema.json`: nested metric fields; validator checks exact metric/aggregation slots, units, samples, components and missing/zero-denominator states.
- `synthetic-*.json`: deliberately invented one-game examples. Counts, hashes, identities and receipts are placeholders; they are not evidence of October coverage or eligible warehouse assets.

Run offline consistency checks from the repository root:

```sh
python -m app.serving.publication_contract contracts/publication/synthetic-manifest.json --binding contracts/publication/synthetic-binding.json
python -m pytest -q tests/test_publication_contract.py
```

The validator checks schema, exact asset/field/dependency sets, identity/source input declarations, coverage cardinalities, input fingerprints, scope, clocks and binding consistency. It performs no provider or warehouse calls. It does **not** authenticate receipts, prove actual discovery membership/row values, validate SQL, grant source permission, check current revocation or perform promotion. Trusted producer/reader composition must verify those before use. A passed receipt declaration is not a passed authority check.

One immutable dataset is named `release_<publication suffix>`. All candidate models must be explicitly targeted there, verified and frozen before activation. Server composition allowlists the project/dataset and resolves the binding once; users/models cannot supply relations. Legacy target tokens resolve only through reviewed server configuration, never arbitrary SQL. Cross-scope release/legacy mixing is unsupported in v1.

The profile supports core Player, Compare, Regular Season Performance, identity search, bounded player-stat Ask and direct team-result SQL. It excludes rankings, recommendations, injuries, similarity, team Ask and full team boxes. New core models require implementation; existing full-page dependencies cannot be declared optional without changing the readers. Team totals come from accepted final results, not player-sum fallback. DNP stays separate from played-player facts. Existing governed units, arithmetic, nulls and identity rules remain authoritative.

The manifest is immutable once sealed. Mutable building/validating/held/failed/eligible/active/superseded state belongs to separate run/promotion records. Source/identity/code/profile changes create a new candidate; retries cannot overwrite a sealed release. Same-input/different-output is a failure. Rollback never renews source validity.

Offline replay uses an explicit evaluation clock pinned to each run through its allocation key. The profile's example clock is for the synthetic fixture only; retained input reviews must precede the run clock. Original capture and source verification times remain unchanged. The 8-day warning and 10-day maximum are additionally capped by cutoff age and the trusted producer's earlier authority/milestone deadline. A production reader supplies trusted wall time and checks current authority; replay acceptance cannot make an expired release fresh. These contracts are not a replacement for the existing scoped source guards or full runtime acceptance tests.

Candidate allocation binds canonical source inputs to one persisted publication ID and materialization clock. The input fingerprint includes that clock; all row ingestion timestamps reuse it across retries. Actual load attempt times remain separate audit. The producer registry must serialize allocation and reject conflicting reuse; this validator only checks a single declaration.
