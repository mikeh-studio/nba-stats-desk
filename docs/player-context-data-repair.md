# Player context: data repair before model evaluation

Validated locally on 2026-09-17 UTC (September 16 Pacific). Production BigQuery
has not been changed. Earlier snapshots and generated answers remain intact.

The raw warehouse, not the new context mart, already lacked shooting counts in
15,696 of 28,308 appearances (55.4%). Every affected row had a derived game ID
and a March 10 ingestion timestamp. The currently checked-in extractor requests
shooting components; repairing the old import requires replaying historical data.

## Recovered evidence

Four official NBA league-game-log responses (player and team, regular season and
playoffs) produced a new snapshot with 28,572 appearances and zero missing
shooting components. It adds 264 appearances, canonicalizes 20,187 game IDs and
retains a complete before/after change ledger. One prior rebound value changes;
297 playoff minute values use the league endpoint's whole-minute rounding rather
than the older CDN's fractional minutes. Regular-season minutes do not change.
Rebounding components and fouls are now included in this snapshot; the original
snapshot did not project those fields, so its absence alone did not prove all raw
warehouse rows lacked them.

Validation covers all 1,230 regular-season games (82 per team) and 85 playoff games
(15 completed series). Player totals reconcile to the separate team endpoint for
points, shooting counts, rebounds, assists, steals and blocks. Team turnovers
include non-player events, so they are not forced to equal player totals. Both
endpoints are NBA sources, not independent providers.

Team minutes can differ from 240 plus overtime by more than two minutes because
individual minutes are rounded. The context coverage diagnostic now uses the
mathematical half-minute-per-player bound, validated against official team logs.
This diagnostic alone is not proof of complete upstream appearances; the repair
validator separately reconciles the full season and team totals.

Opponent context increases from 6,223 to 25,999 full-season appearances. Every
observed appearance in the ten selected cases now has both prior opponent win%
and eFG% allowed context. Early-season games still need five prior games. Tatum's
zero-appearance February baseline correctly remains unavailable.

53 selected prior-day injury PDFs were recovered, adding 5,625 parsed report
rows. Older PDFs use the `05PM` filename, while newer archives also use
`05_00PM`. PDF headers establish source publication times; capture times remain
separate. Source PDF hashes and HTTP checks are saved. Missing reports and
unresolved player identities remain unknown. This is one report sample per date,
not all reports or final pre-tipoff availability.

An official season schedule was also captured to investigate timestamps. Its
scheduled UTC start times have not been integrated or asserted to be actual
tipoff times. Historical roster intervals, exact availability cutoff integration,
and shared court/possession data remain separate requirements for stronger
relational claims. No causal effect is estimated.

## Review and reproducibility

- `reports/ask-evidence/run-05-data-repaired/data-review.html`: six separately
  reviewable repair steps, each with model-token accounting and review notes.
- `reports/ask-evidence/run-05-data-repaired/preview.html`: all ten repaired cases.
- `reports/ask-evidence/data-repair-2025-26/repaired/repair-audit.json`: raw response
  hashes, reconciliation, added appearances and changed values.
- `reports/ask-evidence/data-repair-2025-26/warehouse-diagnosis.json`: bounded live
  raw/gold counts and saved SQL/job identifiers.
- `reports/ask-evidence/data-repair-2025-26/injury-backfill/`: PDF archive, source
  checks and supplemented injury snapshot.

Use a new output directory on every run:

```sh
.venv-airflow/bin/python scripts/repair_context_snapshot.py --capture \
  --source-dir reports/ask-evidence/data-repair-2025-26 \
  --original reports/ask-evidence/inputs/2025-26-snapshot.json \
  --output-dir /tmp/nba-repaired-review
```

Without `--capture`, the command makes no network requests. Neither mode writes
to BigQuery or calls a model. The repair does not infer zero for missing data.
`scripts/capture_context_injuries.py` derives report dates from the selected
teammate cases and uses the existing tested PDF parser, retaining raw source PDFs.

142 focused Python tests pass; Ruff, formatting, dbt parse and whitespace checks
pass. All ten cases pass independent 17-metric arithmetic checks. New regression
checks reject partial seasons, missing components, inconsistent totals, invalid
identities, duplicate keys and missing rotations. The generator now blocks missing
metric inputs before any external call. No production deployment is claimed.

Luna generation and Terra evaluation remain unrun. Data recovery, SQL/Python
calculations and validation used zero generator/evaluator tokens; this excludes
assistant development usage. Earlier automatic approval review blocked sending
enriched injury context to the model service without explicit payload approval.
The repaired run's exact proposed input is saved in `generation-preview.txt`.

## Completed Ask rerun

After the data review, the user authorized generation with the current evidence
and deferred further playing-context work. The local `run-06-current-answers`
contains ten new Luna answers and Terra's evaluation of those actual answers.
Terra marked six pass and four revise (E04, E06, E08, E09). The four revisions
concern mixing statistical evidence into `reported_context` claims; the original
outputs and flags remain visible. This is advisory model review, not human signoff.
Invalid claim structures use the deterministic fallback in Ask-shaped payloads.

Luna's first call returned E01 only. That output was preserved; a second call
supplied the other nine. Recorded generation usage totals 60,827 input and 4,556
output tokens. Terra evaluation used 38,071 input and 1,699 output tokens. Total:
105,153 tokens; estimated API equivalent $0.1141626 using the September 16 recorded
rates. This includes both generation calls and excludes the earlier Terra-only
readiness review and assistant development usage. It is not a CLI invoice.

The earlier approval-blocked status above describes the earlier checkpoint, not
the final rerun. Production tables and public model-generated Ask integration
remain unchanged. Raw packets, model outputs and human notes stay in ignored
local reports; no evaluation input containing injury information is committed.
