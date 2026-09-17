# Player context milestone

Implemented 2026-09-16. Descriptive context and review tooling; no deployment or
causal estimator. The existing public request path still has no CLI model calls.

## Implementation

- Ask's deterministic overview keeps its original five comparison metrics and
  adds a second table for minutes, FGA/3PA/FTA, FG%/3P%/FT%, eFG%, TS%, turnovers,
  points/36 and assists/36. The semantic contract governs the added formulas.
- Rates aggregate counts, never game percentages. Percentage values in overview
  evidence are 0–100 and differences are percentage points. Core contract query
  ratios remain 0–1 internally. Partial data retains coverage and suppresses
  changes; empty and zero-denominator groups stay unavailable.
- Four dbt views separate team-game aggregates, prior opponent context, dated
  status reports, and the final player-game mart. There are no player-specific
  schema columns or universal all-pairs expansion.
- Availability uses reports strictly before midnight UTC of game date because
  stored game-time strings omit AM/PM. This is intentionally labeled
  `before_game_date_utc`, not final tipoff status. Same-time contradictory
  reports become Unknown. Retrospective ingestion timestamps remain visible.
- Focused teammate comparisons distinguish observed co-team participation,
  documented Out with no appearance, unknown, and contradictory evidence. They
  do not estimate shared court time, roster intervals, or causal effects.
- The evaluation packet contains 17 metrics, opponent coverage, and selected
  teammate splits. SQL/Python computes them; only compact summaries are planned
  for Luna/Terra. Full precision, counts, source links, and model SQL stay in the
  review artifacts. New runs preserve all original responses and review notes.

## Live coverage findings

Source: the frozen 2025–26 snapshot plus a bounded read-only capture of existing
`nba_silver.stg_player_injury_reports_clean` records. Code in the repository is
not proof of a deployed relation: `what_changed_injury_reports` was absent in the
live warehouse, so this milestone uses the existing silver report source.

- 28,308 player-game rows; 15,696 (55.4%) lack shooting count components. Earlier
  evaluation windows cannot support shooting comparisons. Harden and Tatum's
  March windows have partial counts. No backfill or guessed values were added.
- 12,417 injury-report records, starting December 22, 2025; 356 unresolved player
  IDs are excluded from player joins. Coverage is not a complete roster census.
- 1,859 player/team/game statuses survive the conservative time screen.
- Opponent win-rate context is available for 6,223 player appearances. A minimum
  of five prior games and complete diagnostic coverage across prior games is
  required. Opponent shooting context is separately gated. This is not a
  possession-based defensive rating or a schedule-adjusted causal estimate.

## Validation

`tests/test_player_context.py` executes the actual dbt SELECTs in SQLite over
fictional fixtures: weighted rates, incomplete denominators, duplicate joins,
future/same-game leakage, phase isolation, conflicting reports, and unknown
teammate status. The local snapshot runner uses the same model SQL.

The read-only BigQuery validation of those SELECTs returned 28,308 rows and
28,308 unique keys, zero future opponent joins, zero late status joins, and
6,223 available opponent-context rows, matching SQLite. It billed 20 MiB; the
separate report capture billed 10 MiB. No warehouse objects were published.

Focused validation: 129 Python tests passed; Ruff lint/format passed. dbt parse
passed. A full repository suite and deployed UI verification are separate
checks; neither is implied by these results.

## Reproduce the local review

Use an environment with repository dependencies. In this checkout,
`.venv-airflow/bin/python` supports the context runner; the Anaconda environment
supports the broader API tests with unrelated pytest plugin autoload disabled.

```bash
python scripts/build_player_context.py \
  --snapshot reports/ask-evidence/inputs/2025-26-snapshot.json \
  --injury-snapshot reports/ask-evidence/inputs/2025-26/injury-context-snapshot.json \
  --output-dir reports/ask-evidence/context-NEW

python scripts/evaluate_reporting_evidence.py prepare \
  --snapshot reports/ask-evidence/inputs/2025-26-snapshot.json \
  --corpus reports/ask-evidence/inputs/2025-26/corpus.json \
  --cases reports/ask-evidence/inputs/2025-26/cases-context.json \
  --context-dir reports/ask-evidence/context-NEW \
  --run-dir reports/ask-evidence/run-NEW

python scripts/evaluate_reporting_evidence.py preview --run-dir reports/ask-evidence/run-NEW
```

`preview.html` is explicitly deterministic, and `generation-preview.txt` is the
exact planned generator prompt. No simulated Luna or Terra results are created.
Context databases and audit packets are hashed before use. `run-03-context`
contains ten prepared cases; the generator/evaluator calls are pending explicit
payload authorization after automatic approval review rejected the first call.
No new model tokens or measured model costs exist yet.

After authorization, run `generate`, `evaluate`, then `report` using the same
runner. Actual usage is captured per model stage; four context steps show zero
LLM tokens and measured local runtime. Model API-equivalent estimates are
separate from CLI subscription accounting and warehouse compute.

## Data-first follow-up

The older import's missing shooting counts and omitted appearances have now been
recovered in a separate local evaluation snapshot. See
[player-context-data-repair.md](player-context-data-repair.md) for reconciliation,
coverage improvements, remaining availability gaps and the new review artifacts.
Production tables and earlier runs remain unchanged.

The current local Ask rerun is now complete: ten Luna answers, evaluated by Terra
(six pass, four revise for claim-kind structure). See the completed-rerun section
of the data-repair report. Historical roster intervals, intraday availability,
lineup/possession context and causal analysis remain deferred.
