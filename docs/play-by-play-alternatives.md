# Manual ESPN play-by-play probe

Use ESPN as the next dev source to evaluate while official PDF downloads are
unavailable. The existing [ESPN pipeline](espn-dev-pilot.md) already extracts
assister-to-scorer relationships. Its mapped-game PDF gate remains unchanged.
The new [manual probe](../scripts/espn_pbp_probe.py) evaluates ESPN PBP without
depending on PDF retrieval or NBA identity resolution. It writes only private
local artifacts, not BigQuery, GCS, Airflow metadata, or serving tables.

## Run the isolated experiment

```bash
.venv-app/bin/python scripts/espn_pbp_probe.py \
  --season 2026-27 --start-date 2026-10-20 --end-date 2026-10-21 \
  --output reports/espn-pbp-unique-run
```

Use a new directory under ignored `reports/`. The script accepts at most seven
league-local dates; scoreboard parsing bounds each date at 30 events. It fetches
one summary per observed final regular-season game, without automatic retries.
The existing capture helper limits responses to 10 MiB, disables redirects, uses
10-second connect/20-second read timeouts, and checks elapsed time between body
chunks. This is not a strict whole-run deadline. A fresh invocation creates a
separate observation suitable for comparing corrections. Manual runs intentionally
do not apply the PDF publication lag; the combined scheduled pilot is unchanged.

Outputs include original response bytes and hashes, retrieval metadata, a result
for each observed final, validated `candidate.json` snapshots, and per-game
`pairs.json` aggregations with source event IDs. Candidate snapshots include PBP
and assist edges. Raw responses preserve fields outside the normalized projection.

Every accepted candidate must pass existing ESPN checks: scope/final status,
unique event IDs, period coverage, shooting arithmetic, player/team points,
final PBP score, scoring-event total, participant identity/team, and each active
player's assist total. Roles come from explicit text and are checked against
participant IDs; array order does not define roles. Unknown formats reject the
whole candidate. Source capture failures and validation failures remain in
`result.json`; exit 2 denotes any failure or no observed finals.

Candidate status is **ESPN internally reconciled only**. NBA IDs remain null;
ESPN IDs are never put into NBA columns. PDF and cross-provider verification are
explicitly absent. Matching the same provider's box score is not independent
proof of event correctness, nor does observed scoreboard coverage certify the
league-wide expected-game denominator.

Semantic hashes exclude retrieval times and unrelated media, preserve meaningful
PBP order, and detect normalized changes. Replaying captured bytes tests hash
stability, not live-source stability. This script has no current-pointer store,
atomic replacement transaction, or warehouse correction test; it cannot overwrite
an existing output directory or replace prior accepted facts.

## Cloud publication and analytical scope

The [ESPN-only dev publisher](espn-only-dev.md) supports independent source,
identity and PDF statuses. The [correction-aware worker](espn-corrections-dev.md)
independently publishes result, box and PBP revisions with bounded rechecks.
Neither manual worker enables scheduling or connects these facts to Ask.

Assists support directed pair counts, assisted points and time/score context.
Blocked-shot and turnover-steal relationships need their own role parser and
box-score reconciliation. Possession duration requires explicit possession
boundaries and handling of free throws, offensive rebounds and corrections;
differences between adjacent event clocks are not possession duration. PBP does
not reveal all passes, potential assists, screens or defensive assignments.

```bash
.venv-app/bin/python -m pytest tests/test_espn_pbp_probe.py tests/test_espn_pilot.py -q
```

Tests use synthetic fixtures for role ordering, semantic replay/correction,
invalid scores/assists/participants/duplicates, failure visibility, bounded dates
and overwrite refusal. Real provider outputs and comparison reports stay private.
