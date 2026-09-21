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

## Roster-scoped offline studies

`build_teammate_study.py` accepts reviewed membership windows, a frozen official
schedule, injury reports, statistics, and a matching context database. It emits
one row per scheduled team game, a descriptive summary, an input/code hash
manifest, and a local Markdown report. Output belongs under ignored `reports/`.

Membership inputs are JSON arrays with `season`, `player_id`, `team_abbr`,
`valid_from` (inclusive), `valid_to` (exclusive), `source_urls`, matching
`source_published_at` values (ISO dates or zoned timestamps), a zoned
`reviewed_at`, and a documented `basis`.
Overlapping intervals are rejected. These are reviewed study windows, not an
automatically maintained league-wide transaction history; uncovered dates remain
unknown. Later retrospective review must not be represented as pregame knowledge.

A study spec contains `name`, `season`, `team_abbr`, `player_id`, `teammate_id`,
`start`, `end` (both inclusive), and `max_report_age_hours` (at most 48).
Only regular-season games are supported. Select the pair/window before inspecting
outcome differences and retain the spec with the evidence.

```bash
python scripts/capture_teammate_reports.py --help
python scripts/build_teammate_study.py --help
python -m pytest tests/test_teammate_readiness.py -q
```

The optional capture CLI uses bounded public PDF reads only with `--capture`;
otherwise it reparses cached PDFs. Raw hashes and capture timestamps are retained.
It samples one report per game, which does not guarantee complete intraday coverage.
The study selects the latest captured report strictly before **scheduled** start,
within the configured age limit and matching season, date, team, and matchup.
Scheduled start is not verified actual tipoff. Equal-time conflicting reports and
Out/participation contradictions are excluded from comparison. Neither unknown
status nor a zero-minute row establishes an injury absence.
Availability uses the latest eligible team/game bulletin. A player omitted from
that bulletin remains unknown; an older Out listing is not carried forward.

Focal nonparticipation stays visible in the panel but is excluded from performance
averages. Summaries show sample sizes, episode counts, shooting ratios, prior
opponent coverage, rest and home-game counts. Standard deviations describe game
variation; leave-one-episode-out ranges measure sensitivity, not confidence
intervals. This workflow estimates no causal effect, performs no warehouse writes,
and makes no LLM calls. Its token/API cost ledger excludes the assistant session
and previously captured evidence. Public Ask integration remains separate.

### Exploratory association adjustment

Install `requirements-analysis.txt` in an analysis environment, then run
`python scripts/evaluate_teammate_association.py --help`. The CLI takes a frozen
panel, injury snapshot, an explicit analysis plan, and a new local output directory.
The plan must declare primary outcome `ast` and covariates
`opponent_prior_win_pct`, `rest_days`, `home`, `other_reported_injury_out`, and
`calendar_month_fixed_effects`, in that order. These are a fixed exploratory
specification, not configurable model-selection candidates.

The analysis uses complete cases, reports exclusions and calendar support, and
compares the same-sample raw difference with OLS adjustment. Other-injury burden
counts Injury/Illness Out listings in the latest captured team/game bulletin;
it is not a complete roster-absence measure. Unresolved IDs, conflicting listings,
or missing bulletin evidence remain missing. Capture combines the NBA player
reference with observed appearances so players who never appeared can resolve.

HC3 and episode-clustered uncertainty are nominal diagnostics. Few, uneven,
non-random exposure stretches do not justify a validated significance badge.
Outputs always retain the exploratory claim level and a null
`validated_significance`. Sensitivity checks restrict to months containing both
exposures and omit each exposure episode. A secondary outcome is the **mean of
individual-game assists per 36 minutes**, distinct from the descriptive panel's
pooled assists/minutes ratio. Same-game minutes are not a primary control.
Rank-deficient or insufficient-data models explicitly return `not_estimable`.

This workflow is offline, makes no model calls, and changes neither warehouse
objects nor public Ask responses. Detailed plans, outputs and p-values belong in
ignored local reports. The implemented specification does not establish causality.

### Ask integration

In the analysis environment, export a reviewed study with
`scripts/export_teammate_study.py --help`, then set
`AGENT_TEAMMATE_STUDY_PATH` to the resulting local JSON and restart the app. The
bundle contains compact scope, statistics and limitations; no fitting or warehouse
writes occur during a request. Keep private bundles under ignored `reports/`.
Export verifies input hashes and recomputes the descriptive summary and adjusted
analysis before accepting the supplied outputs. Mixed or stale outputs are
rejected. Regenerate the study and analysis with the current code and analysis
environment before exporting older artifacts.

Ask recognizes “teammate study/analysis/impact” and questions naming both study
players with absence, significance or causal wording. In-session significance and
uncertainty follow-ups retain the study context. The route is shared by JSON and
streaming Ask endpoints. The selected provider/model writes the narrative; scope
checks gate the response and the server supplies the numeric table and mandatory
uncertainty notice. Unsupported windows, metrics or reversed roles return a
coverage explanation rather than applying the study's estimates.

`evaluate_teammate_ask.py` exercises the actual HTTP handler with a local Codex
CLI model adapter, using Luna for narration and Terra for independent review.
It preserves prompts, raw responses, evaluator verdicts and per-call token usage,
and writes a side-by-side HTML review. `--case` allows rerunning a failed sample.
This validates the local route and response handling, not live provider HTTP
transport or deployment. CLI token counts include its own context overhead.
