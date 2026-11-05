# Core statistics offline replay

Phase 3 implements the `core_stats_v1` calculation and reader path in isolation.
It does not change `app.main`, the current site, scheduled jobs, or warehouse routing.

`app/serving/core_models.py` builds the 13 profile relations from typed accepted
games, appearances, participants and frozen player/team references. It reuses the
existing governed arithmetic. DNP participants remain outside played facts; player
sums validate accepted final scores rather than substitute for them. Ratios use
summed components, missing minutes stay null, per-36 uses only valid minute samples,
and incomplete comparisons/trends withhold changes. Core windows are scope,
last-five, prior-five and last-ten; every empty window still has its metric slots.

`app/serving/core_sql.py` compiles the corresponding BigQuery SELECTs using explicit,
distinct staging/candidate datasets and a fixed materialization clock. Staging
inputs are `games`, `appearances`, `participants`, `players`, and `teams`, with the
exact accepted-model fields. No query reads a mutable current view or the legacy
raw tables. The compiler has no client or executor. BigQuery loading, input/output
verification, immutable allocation, promotion and runtime integration are Phase 4+
work. Offline parsing and SQLite-transpiled scalar arithmetic are not BigQuery
materialization acceptance.

The isolated app requires a pinned bundle digest and per-request authority callback.
It reconciles manifest selections/receipts/counts/missingness with actual rows and
rebuilds all models before serving. Only loopback hosts/clients are accepted;
forwarded requests, expired/revoked authority, changed publication tokens, unsupported
scopes and unknown query filters are rejected. Responses expose an allowlisted
status, not internal manifests or evidence paths. It uses no warehouse or model client.

Supported routes exercise Player, Compare (including distributions), Regular Season
Performance, search, and deterministic Ask JSON/SSE. Ask deliberately uses a small
exact grammar, for example `Test Player A total points`, `Test Player A ratio fg%`,
or `compare Test Player A vs Test Player B per game assists`. Optional explicit
`from YYYY-MM-DD to YYYY-MM-DD` bounds must fit the profile. Unsupported intents
return 422; there is no legacy fallback. This is a workflow proof, not a replacement
for the production language planner. Other query conventions are explicitly rejected.

Run the synthetic local demonstration from this checkout:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHON_DOTENV_DISABLED=1 \
  python scripts/serve_core_replay.py
```

Open `http://127.0.0.1:8033/ask`. All screens prominently label invented synthetic
evidence, the fixed historical evaluation clock, data-through date and freshness.
The demonstration uses two invented games and a fixed synthetic publication ID.
Its authority callback proves only that exact local synthetic fixture; it must
never be reused as an authority issuer for retained/production evidence.

The original Phase 2 `synthetic-manifest.json` is a one-game declaration example.
`synthetic-accepted.json` is a separate two-game executable fixture;
`scripts/build_synthetic_core_replay.py` builds its matching manifest/binding/models.
Neither fixture represents seasonal completeness or authentic NBA evidence.

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHON_DOTENV_DISABLED=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  python -m pytest -q \
  tests/test_core_replay.py tests/test_core_sql.py tests/test_publication_contract.py
```

The private exporter preserves provenance and explicitly distinguishes reported
source fields, reviewed identities and downstream derived fields. The retained
October replay can build all 13 models for local validation after a pinned identity
review resolves the missing NBA IDs and a per-run clock follows the reviews.
That export explicitly carries `offline_model_validation_only` and
`runtime_eligible=false`, including inside its accepted input object. The retained
source captures and verification times are preserved, and freshness is checked at
the run clock. An unavailable identity or review clock is never filled in from the
fixture. This typed model validation does not issue the full-field source approval
required by a publication manifest, or activate the app/warehouse reader.
