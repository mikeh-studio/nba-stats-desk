# Game mapping recovery

`scripts/espn_game_mapping_recovery.py` enriches accepted ESPN result and box
revisions with NBA game aliases in the fixed `nba_gamebook_dev_espn_only`
dataset. It does not republish statistics, change source selection, certify
league coverage, clear failed health, validate PBP, or upgrade PDF results.
The existing weekly box collector and schedule configuration remain unchanged.

The command defaults to a dry run. Pass explicit project, private bucket,
input bundle and a new output directory under ignored `reports/`:

```sh
PYTHON_DOTENV_DISABLED=1 python scripts/espn_game_mapping_recovery.py \
  --project YOUR_PROJECT --bucket YOUR_PRIVATE_BUCKET \
  --bundle reports/game-mapping-bundle --output reports/game-mapping-dry-run
```

After reviewing `plan.json`, use the same arguments with another new output
directory and `--publish-dev` to archive evidence and publish verified aliases.
Configuration never loads dotenv. Existing application-default credentials,
dataset ownership/location and non-public bucket checks apply.

## Evidence contract

The private bundle has `manifest.json` with `season`, ISO `first` and `last`
dates (at most 31 dates), a `pbpstats` object containing relative `body` and
`capture` paths, optional `nba_pages`, and optional explicitly declared
`attachments`. Only declared bounded files are archived; path traversal and
captures exceeding the size bounds reject the run.

The PBP Stats capture receipt must preserve the original `started_at`, URL,
HTTP 200, `status: captured` and SHA-256 of the raw JSON. Its URL must name
the exact season and regular-season game feed. The existing
`espn_sources.game_mapping` function checks unique date/home/away identity,
native NBA season/phase ID, exact final score and unique destination. This
method is recorded as `pbpstats_final_scope_v1` with PBP Stats provenance.
Reusing an old capture does not assert fresh source availability.

Each optional NBA page entry declares `source_game_id`, `body` and `capture`.
The body is retained visible text from an operator-reviewed official NBA page.
Its receipt supplies `method: operator_reviewed_cua_dom`, original timezone-aware
`observed_at`, body `sha256`, exact `url` and `title`. The separate
`reviewed_nba_visible_final_v1` verifier requires the correct official matchup
URL and native ID, exact dated title, matching final scoreboard, and one labelled
four-quarter linescore whose period sums and final scores agree with ESPN.
Overtime or unsupported layouts reject this verifier. A URL, HTTP 200 or score
from another game's ticker is insufficient.

This is reviewed identity evidence, not an independently authenticated automatic
NBA HTTP capture. It does not weaken or replace the existing
`reviewed_nba_page_pdf_final_box_v1` verifier's PDF requirement. A conflicting
PBP Stats score stays a mismatch even when the NBA page verifies the identity.

## Publication and audit

The publisher reads accepted current result/box revisions, binds each decision
to those exact revisions, and writes an offline plan. Publication uploads the
declared evidence and review receipts before writing aliases. A durable query
checkpoint preserves uncertain BigQuery outcomes for inspection.

One transaction serializes on the existing store control row, rejects changed
result/box revisions or scope, enforces both ID directions, inserts only absent
aliases and records source-specific checks. Existing aliases are never silently
reassigned. Repeated publication inserts no duplicate aliases. A later review
may append another evidence-bound audit; prior checks remain inspectable.

`espn_game_mapping_reviews` stores `review_id`, ESPN event ID, result/box
revisions, source, status, detail, evidence URI and completion time. PBP Stats
checks use `matched`, `unresolved` or `mismatch`; NBA page checks use
`verified_identity`. Mismatch details retain the accepted ESPN scope and the
conflicting provider records. Consumers must match the recorded revisions to
their current facts before reusing any check. This audit is separate from
ESPN PBP acceptance and PDF certification.

After publication, readback verifies resolved aliases and unchanged result,
box and PBP revisions, layer status, scores and PDF status. This remains a bounded
manual recovery command. The separate weekly discovery stage reuses its strict
planning and publication guards; see [weekly refresh](weekly-refresh.md) for
acquisition limits and Jobs reporting. It never performs manual NBA page review
automatically or converts a PBP Stats conflict into a successful verification.

Focused validation: `python -m pytest tests/test_espn_game_mapping_recovery.py
tests/test_espn_pilot.py -q`.
