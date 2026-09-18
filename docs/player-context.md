# Player context

Ask's deterministic performance overview adds minutes, attempts, shooting rates,
turnovers, and points/assists per 36 minutes to its five core production metrics.
The [semantic contract](semantic-contract.md) governs the formulas. Period
percentages use summed makes and attempts; missing inputs remain unavailable.
Two-player scorecards retain their five core comparison metrics.

## Context relations

The checked-in dbt models separate reusable context from the player-game fact.

| Relation | Grain | Contents |
| --- | --- | --- |
| `team_game_context` | Season, phase, game, team | Box-score totals and separate score/minutes and shooting coverage diagnostics |
| `team_defense_before_game` | Season, phase, game, team | Win percentage and eFG% allowed using strictly earlier game dates in the same season and phase |
| `player_game_reported_status` | Season, game, team, player | Latest qualifying historical report, source URL, report/ingestion times, conflict indicator |
| `player_game_context` | Season, game, player | Original facts with prior opponent context and scoped historical reported status |

The final mart preserves the fact grain. It does not store every possible
player pair or turn relationships into player-specific columns. Opponent measures
require at least five prior games and complete diagnostic coverage across prior
games. Box-score coverage diagnostics do not independently prove source completeness.

Status reports must precede midnight UTC on the game's schedule date. This
conservative `before_game_date_utc` policy is **not** final pre-tipoff status.
Conflicting statuses at the selected report time become Unknown. Report time and
later ingestion time remain separate; missing reports do not establish health.

## Interpretation and integration

The offline evaluation can include opponent summaries and selected teammate
splits. Teammate groups distinguish observed co-team participation, reported Out
with no appearance, unknown, and conflicting evidence. An unobserved teammate
is not automatically an injury absence or a roster member. Co-participation does
not establish shared court time.

These outputs are descriptive. Opponent win% and eFG% allowed are not
possession-based defensive ratings. Historical roster intervals, lineup stints,
and causal effect estimates are not implemented by these models. Same-game
box scores remain outcomes, not pregame information.

The public overview uses the richer statistical metrics. The opponent/teammate
bundle and generated reporting narrative are exercised through the offline
[evaluation workflow](evaluation.md); the public Ask handler does not invoke
that CLI. Checked-in models and local validation do not establish that warehouse
objects or repaired data have been deployed.

## Validation

```bash
python -m pytest tests/test_player_context.py tests/test_context_data_repair.py -q
dbt parse --project-dir . --profiles-dir dbt/profiles --target dev
```

Fixtures execute the actual context SELECTs in SQLite to check grain, earlier-game
joins, phase isolation, missingness, and report conflicts. BigQuery execution is
a separate validation boundary. See [Validation](validation.md) for warehouse
checks and [Evaluation](evaluation.md) for frozen local context builds.
