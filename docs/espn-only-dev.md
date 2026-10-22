# ESPN-only dev ingestion

The manual [worker](../scripts/espn_only_ingestion.py) publishes ESPN-reconciled
PBP, player box scores and assist relationships to `nba_gamebook_dev_espn_only`.
It uses the [revision publisher](../scripts/espn_only_store.py) and the existing
private GCS dev prefix. It never publishes to production or changes Airflow
metadata. The existing ESPN/PDF DAG and its PDF acceptance gate are unchanged.

The [correction-aware layer worker](espn-corrections-dev.md) adds independent
result, box and PBP acceptance plus bounded correction-window discovery in
separate `espn_layer_*` tables in the same dev dataset. The original worker and
views described below retain their existing contract.

## Independent status contract

`espn_only_games` includes every observed final, even rejected games with no
current revision. Its fields answer separate questions:

| Field | Values and meaning |
|---|---|
| `reconciliation_status` | `reconciled` for the current accepted ESPN snapshot; `not_reconciled` if no candidate has been accepted. |
| `health` | Latest fact-ingestion health. A failed replacement retains the previous reconciled snapshot but sets health to `failed`. Identical replay does not clear that failure. |
| `nba_game_mapping_status` | `resolved`, `unresolved`, or `conflict`; a mapping must still agree with the current date, teams and final score. |
| `nba_player_mapping_status` | `resolved` if every active player has an NBA ID; `partial`, `unresolved`, or `not_attempted` otherwise. |
| `pdf_verification_status` | `not_attempted`, `matched`, `unavailable`, or `mismatch`, scoped to the exact fact revision. |

Latest reconciliation, game-mapping, player-mapping and PDF attempts expose
their status/error separately. A failed PDF recheck cannot erase a previous
match for the **same revision**, but remains visible in `latest_pdf_attempt_*`.
A corrected fact revision does not inherit an old PDF match. A failed enrichment
does not change reconciled facts to failed facts or falsely mark them verified.
Consumers must inspect both status and latest attempts; `health=healthy` alone
does not certify mappings or PDF verification. `nba_game_id` is a retained mapping
candidate when mapping status is `conflict`; use it as resolved only when status
is `resolved`.

NBA IDs are overlaid from insert-only crosswalks. Original source facts retain
ESPN IDs and null NBA fields, so adding a mapping cannot change their revision.
Name matching is not a provider-certified crosswalk. The worker checks active
players against the pinned catalog with the existing ambiguity/conflict rules.
It reuses only matching, evidence-backed game mappings from the original
`nba_gamebook_dev_espn` dev dataset; unresolved games do not need invented IDs.

## Tables, views and publication

The existing `espn_coverage`, `espn_revisions`, `espn_attempts`, game/player mapping
tables and identity audit tables are instantiated in the separate destination.
`espn_only_checks` is an append-only enrichment-attempt table keyed by ESPN game,
fact revision and check kind, with evidence URI and completion time.

- `espn_only_games`: coverage, current revision, health and independent statuses.
- `espn_only_current_plays`: one current PBP event per game/revision/source event,
  preserving source sequence, period, clock, team, text, scores and participants.
- `espn_only_assists`: current assist edges, resolved player IDs and game statuses.
- `espn_resolved_player_box_scores`: current box scores with identity overlays.
- `espn_only_latest_checks`: latest enrichment attempt per game/revision/kind.

The `espn-only/1` snapshot retains only the source fields needed to reproduce
reconciliation. At publication, the validator recomputes all normalized players,
assist edges and counts from those inputs and compares the entire candidate.
Caller-supplied flags cannot bypass this check. Raw bytes and original capture
metadata are archived separately in private GCS; retrieval time and unrelated
root-level media are excluded from the semantic revision hash.

The existing BigQuery transaction atomically publishes a new revision, updates
the current pointer and records success. It checks the expected previous
revision, refuses historical rollback and protects accepted identity mappings.
Failed candidates/confirmed transaction failures are audited and retain prior
valid data. Unknown query outcomes stop with a durable job checkpoint; inspect
them before retrying. Rejected source games remain in the coverage denominator.
Use one writer per destination; no new scheduler or concurrency lease is added.

The original `ESPNStore` continues to require its previous schema and PDF rules.
Only `ESPNOnlyStore`, restricted to this destination or synthetic dev test
datasets, accepts `espn-only/1` and treats reconciliation as sufficient for fact
publication. Fault injection remains restricted to synthetic test datasets.

## Manual execution

Use the existing app environment and private `.env` GCP credentials. No new API
keys or IAM grants are needed. Default destination is fixed to the isolated
ESPN-only dev dataset; alternate destinations are restricted to the synthetic
test prefix. New output directories must be inside ignored `reports/`.

```bash
# Fresh bounded source capture and dev publication (maximum seven dates).
.venv-app/bin/python scripts/espn_only_ingestion.py \
  --season 2026-27 --start-date 2026-10-20 --end-date 2026-10-21 \
  --output reports/espn-only-fresh

# Explicit replay of original hashed probe captures, preserving their timestamps.
.venv-app/bin/python scripts/espn_only_ingestion.py \
  --season 2026-27 --start-date 2026-10-20 --end-date 2026-10-21 \
  --capture reports/espn-pbp-original \
  --output reports/espn-only-replay
```

Optional `--pdf-captures <private-json-file>` accepts an object mapping ESPN game
IDs to local frozen PDF capture directories. It validates source URL, scope and
hash, reparses the PDF, checks the full PDF storage contract, and compares each
assist event and active player's scoring/assist totals. Comparison artifacts and
original capture times are archived. This never claims a new live PDF download.
Without this flag, PDF verification is `not_attempted`; blocked URLs are not
repeatedly requested and are not mislabeled as successfully checked.

Exit 0 means the requested facts and attempted enrichments completed without
failure. Unresolved mappings and an unattempted PDF do not make a reconciled fact
fail. Exit 2 means at least one rejection, retained failed health, or attempted
enrichment failure. Exit 3 means an uncertain query outcome. Other exceptions
save a private failure report and stop. Always inspect per-game results: partial
publication is intentional and a failed batch does not roll back unrelated
valid games. Missing previously registered finals within the requested window
fails discovery before new fact publication. Coverage is still observed ESPN
coverage, not an independently certified league-wide game list.

## Verification

```bash
.venv-app/bin/python -m pytest tests/test_espn_only.py tests/test_espn_pilot.py \
  tests/test_espn_identity.py tests/test_espn_pbp_probe.py -q

# Opt-in real cloud writes to a newly named synthetic dev dataset.
.venv-app/bin/python tests/espn_only_cloud_scenarios.py \
  --output reports/espn-only-cloud-tests
```

The cloud scenarios test initial publish, no-op replay, replay after identity
enrichment, a valid event correction, invalid replacement, an injected failure
after transactional writes, retained failed health, historical rollback rejection
and recovery through a new valid revision. They also check that an old PDF match
does not attach to a corrected revision and that a failed PDF retry remains
visible. Synthetic corrections and PDF statuses are test inputs, not assertions
that ESPN/NBA actually changed a game. Test datasets and evidence are retained
for inspection; no existing dataset is dropped.

This worker is not connected to an Airflow DAG or Ask. Before scheduling, require
a reviewed expected-game denominator, correction/retry windows, provider-use
terms and a manual DAG validation. The shared scheduler remains untouched.
