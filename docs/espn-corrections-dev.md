# ESPN correction-aware dev ingestion

The manual [layer worker](../scripts/espn_layer_ingestion.py) publishes independent
result, box-score, and reconciled PBP revisions in `nba_gamebook_dev_espn_only`.
It creates separate `espn_layer_*` tables and views; original ESPN-only snapshots
remain unchanged. There is no Airflow registration, scheduling, production write,
or Ask integration in this worker.

## Acceptance contract

| Layer | Required checks | Downstream failure |
|---|---|---|
| Result | Completed regular-season game; summary and discovery agree on identity, date, teams, final score | Invalid/missing box and PBP do not block the result |
| Box | Result checks; player identity, required fields, shooting/rebound arithmetic, player points matching the final | Invalid/missing PBP does not block the box |
| PBP | Result/box checks; unique events, period coverage, clocks, scoring totals/final, participants, per-player assist totals | Invalid candidates remain in raw evidence and audit; prior accepted facts remain available |

Results are ESPN-reported, not independently NBA-verified. The worker never
rewrites PBP scores or invents assists to force agreement. Block and possession
relationships are not implemented. Internal reconciliation does not prove every
credited assist is factually correct.

Each box revision references its exact result revision. Each PBP revision references
both result and box revisions. Changed dependencies make prior data `stale`.
Failed replacements retain accepted pointers with `retained_after_failure` status.
Without an accepted revision, result/box status is `unavailable` and PBP status is
`pending_correction`; health and the actual error remain separately visible.

`espn_layer_validated_assists` includes only games with current, healthy, matching
result, box and PBP revisions. Recompute aggregates from this view after changes;
never append corrected pair counts to previous counts. Include all observed games
in coverage reporting. Missing PBP coverage is not zero assists.

## Storage and recovery

- `espn_layer_games`: observed finals and immutable identity/date/team scope.
- `espn_layer_state`: current revision/health per layer, latest capture time,
  latest accepted observation time, latest publication change time.
- `espn_layer_revisions`: immutable normalized inputs/facts with evidence links.
- `espn_layer_attempts`: append-only audit, previous/new revision, capture and
  processing timestamps, errors and field-level correction diffs.
- `espn_layer_current`: retained current snapshots, including unhealthy ones.
- `espn_layer_status`: independent layer statuses and reported final score.
- `espn_layer_player_box_scores`: retained box rows and existing NBA ID overlays;
  filter `box_status='accepted'` for current analytics.
- `espn_layer_readiness`: statuses, mapping coverage, revision-scoped PDF evidence,
  `next_check_at` and correction queue state.

Original bytes and capture/hash metadata are preserved in private GCS. Candidates
are deterministically recomputed before publication. Each BigQuery transaction
atomically writes a revision, pointer and success audit, with expected-current
comparison and store-control locking. Unknown query outcomes retain a durable
checkpoint and stop processing. Use one writer per destination.

Identical replay records a no-op without adding facts or advancing capture time.
Old bytes cannot clear failed health. A genuinely newer successful capture of
identical facts can clear a failure; prior failures remain in audit history.
A confirmed failed transaction can retry the same capture. Older captures cannot
replace newer facts. A fresh provider response reverting to earlier content can
restore it, recording a new audit entry pointing to the existing immutable revision.
Observation time distinguishes fresh provider reversion from historical replay.

NBA mapping remains an independent overlay using existing crosswalks; this worker
does not discover new mappings. Existing PDF matches are reused only for the
exact equivalent legacy PBP snapshot. `matched_for_pbp_revision` describes that
retained PBP, not necessarily the latest box: inspect `pbp_status` too. A changed
revision does not inherit old PDF evidence. PDF fetching is outside this worker.

## Correction policy

The planner uses Eastern league-local dates. A manual correction run discovers
today and the previous seven dates, then adds known games with due milestones.
Milestones are noon Eastern on D+1 through D+7, D+14 and D+30. Unresolved games
also receive D+21 and D+28 checks. Reconciled games are revisited too. Missed
milestones remain due until a fresh capture satisfies them. Original replay
timestamps cannot postpone checks.

After D+30, unresolved games or missed final checks enter `needs_review` and the
plan's exception list; retry with an explicit bounded date run. Completing this
window does not mean the NBA can never revise a game again. These intervals are
our operating policy, not a provider SLA. The plan is bounded to 31 dates and
1,500 known games. Coverage remains observed ESPN coverage, not a certified
league-wide denominator.

Empty scoreboards are valid. Losing an already observed final fails that date
visibly. Source failures retain prior facts and can retry on a subsequent manual
run. No automatic retry process is activated by adding this worker.

## Commands

Use the app virtual environment and existing private `.env` credentials.

```bash
# Fresh explicit ingestion, at most seven dates.
.venv-app/bin/python scripts/espn_layer_ingestion.py \
  --season 2026-27 --start-date 2026-10-20 --end-date 2026-10-21 \
  --output reports/espn-layers-fresh

# Replay original hashed captures without substituting current timestamps.
.venv-app/bin/python scripts/espn_layer_ingestion.py \
  --season 2026-27 --start-date 2026-10-20 --end-date 2026-10-21 \
  --capture reports/pbp-alternatives-1/espn \
  --output reports/espn-layers-replay

# Inspect the plan without fetching sources or publishing facts.
# Initializes/checks dev schemas and saves the private plan locally and in GCS.
.venv-app/bin/python scripts/espn_layer_ingestion.py \
  --season 2026-27 --correction-window --plan-only \
  --output reports/espn-correction-plan

# Execute the correction plan manually. Does not enable scheduling.
.venv-app/bin/python scripts/espn_layer_ingestion.py \
  --season 2026-27 --correction-window \
  --output reports/espn-correction-run
```

Exit 2 indicates discovery failure, rejected layers or retained failed health.
Exit 3 indicates an uncertain checkpoint. Other layers' valid publications are
retained. Inspect `plan.json` for exceptions even when `--plan-only` exits 0.

## Validation

```bash
.venv-app/bin/python -m pytest tests/test_espn_corrections.py \
  tests/test_espn_only.py tests/test_espn_pilot.py tests/test_espn_identity.py \
  tests/test_espn_pbp_probe.py -q

# Creates a separate synthetic dev dataset; never modifies real-game facts.
.venv-app/bin/python tests/espn_layer_cloud_scenarios.py \
  --output reports/espn-layer-cloud-tests
```

Synthetic tests cover delayed assist reassignment, replay, invalid replacement,
transaction rollback/retry, stale-capture rejection, fresh provider reversion,
and a corrected result arriving before its box. Planner tests cover daylight
saving changes, overdue checks and exception routing. Report live warehouse
verification and provider freshness separately from fixture evidence.
