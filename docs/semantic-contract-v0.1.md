# NBA agent semantic contract and evaluation proposal

Status: **Design reference — first deterministic implementation slice available below.**
Version: `nba_semantics/0.1-draft`
Prepared: September 9, 2026

## Implementation status — September 10, 2026

The contract runner, bounded historical evaluation, and experimental language evaluation are implemented.
See [implementation and validation scope](semantic-contract-implementation.md).
The original proposal below remains the design reference; the complete historical
and LLM evaluation gates are not yet implemented, and Ask behavior has not migrated.

## Decision this proposal supports

Establish a shared definition of what the NBA agent may answer, how it computes the answer, and what evidence it returns. Start by describing and evaluating existing gold models. New physical tables are an option only after evaluation identifies a correctness, coverage, or performance gap.

The original proposal defined semantics and acceptance criteria without changing code or warehouse objects. The implementation linked above now exercises representative synthetic cases; the full evaluation families below remain acceptance criteria, not production accuracy results.

## 1. Scope and current evidence

Reviewed the dbt and agent code, including historical-season support. Source references are relative to the repository root.

| Existing asset | Reuse | Constraint identified in source |
| --- | --- | --- |
| `dbt/models/gold/fct_player_game_stats.sql` | Canonical player-game components | Needs documented appearance/null policy and a semantic key |
| `dbt/models/gold/dim_player.sql` | Identity and reference attributes | Latest profile is not historical team membership; archive physical attributes can be missing |
| `dbt/models/gold/player_category_profile.sql` | Season summaries and qualified-player cohorts | Currently mixes phases; five-game qualification; some composite names disagree with formulas |
| `dbt/models/gold/workbench_compare.sql` | Existing period/window calculations | Uses a weighted fantasy proxy distinct from the simple index; some windows span phases |
| `dbt/models/gold/player_recent_form.sql` | Recent appearance summaries | Calendar windows anchor to each player's latest game |
| `dbt/models/agent/agent_player_search.sql` | Convenient context summary | UI-dependent, qualified players only; does not preserve all evidence timestamps |
| `dbt/models/gold/what_changed_injury_reports.sql` | Dated, sourced injury evidence | Unresolved identities excluded; archive captures one daily report with gaps |
| `app/agent/semantic_catalog.yml` | Metric names and aliases | Missing aggregation contracts and operation capabilities |
| `app/agent/tools.py` | Existing query/tool orchestration | Period summaries average per-game ratios |
| `app/repository/_constants.py` | Existing ranking capabilities | Narrower metric support than the catalog advertises |

The current separate season datasets prevent accidental cross-season window mixing. Preserve that isolation initially. Cross-season comparison should query each explicitly selected season independently and combine compatible aggregates. A future union requires season-aware keys, joins, and windows first.

## 2. Entity and join contract

| Entity/evidence | Logical key | Rules |
| --- | --- | --- |
| Player | `player_id` | Names and aliases resolve to IDs; ambiguity returns candidates; no five-game restriction on discovery |
| Player in a season | `season, player_id` | Presence is established by source evidence; zero results are not proof that a player does not exist |
| Game | `season, game_id` | Preserve game IDs as strings, including leading zeroes |
| Player appearance | `season, game_id, player_id` | Must be unique before aggregation; missing rows are not synthesized as DNP or zero production |
| Team in a game | `season, game_id, team_id` | One row per participating team; prefer stable IDs and official schedule context |
| Injury report row | `season, report_timestamp_utc, game_date, matchup, team_abbr, player_name_source` | Keep unresolved names distinct; do not deduplicate by NULL player ID |

Player-game → game is many-to-one on the complete game key. Player-game → player is many-to-one only against a validated unique identity relation. Never join player-game evidence directly to multiple injury reports and then sum statistics: resolve reports to the requested evidence grain first.

For historical team filters, use the team attached to the appearance, not the player's latest team. Cross-season answers return team context per season. Official neutral-site schedule context takes precedence over ambiguous matchup text.

## 3. Question and time contract

These are **recommended defaults for review**, not descriptions of all current behavior.

- **Season:** use the selected app season. An explicitly named season overrides it for that answer and is disclosed. Cross-season questions require explicit seasons. Never silently fall back to current-season data.
- **Phase:** an unqualified “season” defaults to **Regular Season**. “Playoffs” selects Playoffs; “both” explicitly combines them. Regular-season/playoff comparisons retain separate rows and sample sizes. Play-in/preseason/other categories are outside this backfill and return unsupported coverage.
- **As-of:** for latest-data questions, choose the latest available game date in the selected season and phase, shared across players. Return that date. Explicit dates remain explicit, including when no games occurred. Do not silently substitute the player's last appearance.
- **Last N games:** select the player's most recent N observed appearances on/before the anchor, after season, phase, team, and opponent filters. Sort by game date and game ID. State the filter semantics in the answer. Fewer than N returns the actual sample, not padded zeroes.
- **Prior N games:** the immediately preceding non-overlapping N appearances under identical filters. A last-five/prior-five comparison uses at most ten appearances.
- **Last N days:** inclusive calendar interval `[as_of - (N - 1) days, as_of]`, shared across players. “Last week/month” means the preceding complete calendar week/month; weeks start Monday.
- **Past/last N months:** `(as_of minus N calendar months, as_of]`, clamping the earlier date to its month's last day when needed. Uses the latest source date unless an as-of date is explicit. Results remain restricted to the selected season and phase; the answer states this restriction and the exact dates.
- **Broad player performance overview:** returns points, rebounds, assists, steals and blocks, with per-game averages, metric-specific league percentiles, changes against the preceding equivalent period, and monthly charts. Explicit trailing calendar windows end today and combine relevant archived seasons; unqualified phase includes regular season and playoffs. Without a calendar window, the selected season is used. The date range and phase appear in the answer. Each metric excludes missing values from averages and withholds percentiles, changes and charts for incomplete player samples. Percentiles require at least five appearances and complete metric data; baseline periods outside available archives are unavailable. Player IDs must resolve uniquely before a headshot or player statistics are shown; ambiguous identities offer choices and accept a typed name, team abbreviation or ID.
- **Timezone:** retain NBA source game dates as schedule dates. Convert timestamp-based report comparisons explicitly using America/New_York. Never reinterpret a game-date-only field as a UTC instant.
- **Future/as-of evidence:** an event-date filter is not a reconstruction of what the warehouse knew historically. Distinguish retrospective corrected statistics from evidence published before a specified cutoff.

Example: “Compare his last five games with his 2024-25 average” uses five appearances and a full regular-season baseline through the same as-of date, unless the user requests playoffs or a baseline excluding the recent window. Baseline membership is returned explicitly.

## 4. Metric contract

Compute at full available precision, then round for display. Each metric records `definition_version`, `unit`, source components, valid aggregations, null policy, supported dimensions, and operation capabilities.

Let `n` be observed appearances with all required components present. Also return total observed appearances and missing-component count. An observed row in the current game-log source counts as an appearance, including recorded zero minutes; do not add a generic `MIN >= 1` filter to all metrics. Source precision/rounding limitations remain visible.

| Metric identifier | Definition over selected appearances | Display and interpretation |
| --- | --- | --- |
| `pts`, `reb`, `ast`, `stl`, `blk`, `tov`, `fg3m` | Total = SUM(component); per-game = total / n | Count totals; per-game to 1 decimal; aggregation always explicit |
| `min` | SUM(minutes), or SUM(minutes) / n | Minutes; source precision preserved |
| `fg_pct` | SUM(FGM) / SUM(FGA) | Ratio internally; percent to 1 decimal |
| `fg3_pct` | SUM(FG3M) / SUM(FG3A) | Ratio internally; percent to 1 decimal |
| `ft_pct` | SUM(FTM) / SUM(FTA) | Proposed catalog addition; ratio internally |
| `ts_pct` | SUM(PTS) / (2 × (SUM(FGA) + 0.44 × SUM(FTA))) | Estimated efficiency; ratio internally; 0.44 is part of the versioned definition |
| `plus_minus` | SUM(plus_minus), or SUM(plus_minus) / n | Observed on-court point differential, not causal player impact |
| `fantasy_points_simple` | PTS + REB + AST + STL + BLK − TOV | Existing simple box-score index; total or per-game |
| `fantasy_proxy_weighted` | PTS + 1.2×REB + 1.5×AST + 3×STL + 3×BLK + FG3M − TOV | Existing workbench formula under a proposed distinct identifier |
| `points_created` | PTS + 2×AST | Existing attributed-points proxy; not measured possessions or team scoring |

A zero denominator produces NULL/unavailable, not 0%. Do not average per-game percentages for period shooting percentages. Percentage comparisons return percentage-point differences; relative percentage change is a separately named calculation. TS% is not constrained to at most 100% at game grain.

NULL is unknown, not zero. For an explicit player's incomplete sample, expose partial coverage and both row counts. Exclude incomplete required-component samples from default rankings until an approved policy covers them; return excluded-player counts. Reject impossible component relationships rather than quietly correcting source values.

“Fantasy Score,” “fantasy points,” and unspecified fantasy scoring default to `fantasy_proxy_weighted`, as approved by the user. Explicit simple scoring uses `fantasy_points_simple`; external league formulas require separate support. Neither formula implies compatibility with an external fantasy provider. Quarantine `category_score_6cat` and `category_score_7cat` from semantic exposure until their intended components and names are approved.

## 5. Eligibility, rankings, and capabilities

Identity search and individual game facts have no minimum-game threshold. Individual period summaries require at least one valid observation and return sample warnings.

Proposed project ranking default: five appearances in the selected phase/window; this is **not official NBA qualification**. A user-supplied threshold overrides it. If no player qualifies, return an empty cohort with an explanation; do not lower the threshold silently.

Shooting leaderboards require an explicit attempt threshold in v0.1, or a follow-up clarification. Do not invent a single threshold that applies equally to a full season and a five-game window. TS% eligibility uses an explicitly named FGA threshold initially; do not call FGA the TS% denominator.

Rank totals and per-game metrics separately. Ties share rank using `RANK`; use player ID only for stable display ordering. A percentile uses `PERCENT_RANK` over eligible values in ascending order for “higher is better,” with descending order for ball-security TOV. Singleton cohorts return percentile unavailable. Return direction, cohort size, threshold, exclusions, and whether the requested player qualifies. “Most turnovers” sorts descending and does not reuse a ball-security ranking.

Capabilities are declared per metric and operation: `game_value`, `period_total`, `period_average`, `period_ratio`, `rank`, `percentile`, `compare`. Metric discoverability does not imply all operations are implemented. Unsupported operations return a structured explanation, never substitution with another metric. The proposed contract describes the target; current catalog/repository capabilities must be audited before any capability is enabled.

## 6. Injury evidence and coverage

For “what was reported before this game,” select the latest source report for the resolved player and matchup with publication timestamp strictly before verified tipoff. A report date alone cannot establish that ordering. If tipoff or timezone is unavailable, mark pregame ordering unverified. Do not reuse `player_availability_current` for historical evidence.

Source report time and ingestion time are separate. The backfilled archive can answer what the source published historically, within sampled-report coverage; it cannot prove the system had ingested that evidence at the time.

Return the source URL, report time, target game, identity-match status, and daily-snapshot coverage limitation. A missing report is unknown status. An earlier Out report followed by an appearance remains an evidence discrepancy, not an inferred recovery event. A complete box-score backfill does not imply complete injury or quarter-score coverage.

## 7. Agent query and response contract

The proposed flow is: resolve entities → resolve metric and supported operation → bind season/phase/window/cohort → choose governed source and approved joins → execute bounded read → validate results → answer with evidence.

Conceptual request, not an implemented API:

```json
{
  "contract_version": "nba_semantics/0.1-draft",
  "metric": "ts_pct",
  "operation": "rank",
  "season": "2024-25",
  "season_type": "Regular Season",
  "window": {"type": "season_to_date", "as_of": "2025-04-13"},
  "eligibility": {"min_games": 5, "min_fga": 200},
  "limit": 10
}
```

Every answer includes resolved scope, actual sample, metric definition/version, qualification, sources/query ID, source-through date, and warnings. Ratio evidence includes numerator and denominator. Separate `data_through`, `source_published_at`, `ingested_at`, and `model_built_at`; use only fields available for that evidence type.

The primary path uses governed metric definitions. A question outside coverage may use documented read-only analytical exploration, clearly labeled with its source and validation limits. Permission failures and unsupported coverage must not be represented as “no matching players.” Nothing here authorizes unrestricted SQL, publishing, or warehouse writes.

## 8. Evaluation proposal

### Independent oracle and frozen evidence

Before evaluating an agent, a reviewer approves each metric definition and independently authored reference query. Use small synthetic fixtures for exact edge cases and frozen historical inputs/results for realistic questions. Record the data snapshot or version, source relation, reference SQL, expected typed result, applicable contract version, and review date. A query rerun on mutable data is not a frozen expected answer.

Do not generate expected answers using the same production aggregation helpers being tested. Have reference SQL produce unrounded values, metric components, selected keys, and cohort metadata. Compare result semantics, not SQL text. No numerical NBA answers are asserted by this proposal.

### Initial suite

| ID | Question or fixture | Expected behavior / acceptance assertion |
| --- | --- | --- |
| E01 | Find an observed player with three appearances | Resolve identity; allow facts; explain exclusion only when ranking |
| E02 | Two players share a normalized name/alias | Return disambiguation candidates; no guessed ID |
| E03 | 2024-25 PPG, then playoff PPG | Correct phase rows and denominators; no silent combined season |
| E04 | FG games 1/1 and 0/9 | Period FG% = 10%; reject 50% |
| E05 | One game with FGA=0, FTA=0, PTS=0 | TS% unavailable; no divide-by-zero or fabricated 0% |
| E06 | TS sample totals PTS=30, FGA=20, FTA=10 | Ratio = 30/48.8; display 61.5%; preserve numerator/denominator |
| E07 | Last five vs prior five; only seven appearances | Five vs two, no overlap, partial prior-window warning |
| E08 | Last 14 days for a player absent for a month | Empty calendar window; do not substitute their previous appearances |
| E09 | Traded player, filter previous team | Include only that team's appearances; latest team does not rewrite history |
| E10 | Rank TS% with five games and at least 200 FGA | Exact eligible cohort and sum-based ratios; disclosed thresholds |
| E11 | Ask for TS% leaders without attempt qualification | Request threshold; no silently invented eligibility |
| E12 | TOV: best ball security vs most turnovers | Correct opposing directions, shared tie ranks, explicit sample |
| E13 | All tied players and a singleton percentile cohort | Ties agree; singleton percentile unavailable |
| E14 | Earlier Out report; later report after tipoff | Use only pre-tipoff evidence; source URL/time present; no claim of later status knowledge |
| E15 | Missing injury report or unresolved identity | Unknown/ambiguous, never “healthy”; preserve coverage warning |
| E16 | Same player across 2023-24 and 2024-25 | Aggregate each season separately; no blended last-N window |
| E17 | “Fantasy points” | Default to `fantasy_proxy_weighted`; honor explicit simple scoring |
| E18 | Discovered metric with unsupported ranking operation | Structured unsupported operation, no tool exception or metric substitution |
| E19 | Duplicate player-game or one-to-many injury join | Validation failure before sums are presented |
| E20 | NULL component or missing quarter scoring | Partial/unavailable evidence; no zero fill or fabricated period breakdown |
| E21 | Explicit as-of before later appearances | Exclude later games; disclose retrospective-vs-known-at-time semantics |
| E22 | Rebuild timestamp fresh; underlying observations old | Answer freshness follows evidence date, not build timestamp |
| E23 | Cross-season/phase request unavailable or access denied | Clear unsupported/access status, no fallback to a different scope |
| E24 | “Season average” versus explicit combined-phase request | Apply proposed default only to the first; label combined scope on the second |

For integration, select and freeze at least one real historical case per identity, phase, trade, ratio, comparison, and injury family. Synthetic and historical results must be reported separately.

### Scoring and release gate

- Evaluate entity resolution, source choice, season/phase/window, metric math, cohort membership, and evidence presentation separately. An answer with correct arithmetic but the wrong population fails.
- Exact match for entity/game sets, counts, source scope, and required warnings; absolute tolerance 1e-6 for unrounded fixture ratios/averages. Display precision follows the contract. Verify rank tie groups without demanding arbitrary tie ordering.
- Every release-blocking fixture and reviewed historical reference case must pass. Evaluate each natural-language case with three phrasing variants; require all critical semantics to pass each variant. Include repeated runs for ambiguous/tool-routing cases to expose nondeterminism.
- Unsupported and clarification cases pass by correctly withholding an answer, not by producing a number.
- Present coverage denominators and every failure category. A perfect initial suite means only that this bounded suite passed; it is not a production accuracy claim.
- Record latency, query count, scanned bytes, and governed-path usage as baselines before setting performance gates. Do not trade correctness for a higher governed-path percentage.
- On future changes to models, catalog, tools, or model provider, rerun affected cases plus the core gate. Reviewed user corrections become new fixtures and reference changes.

## 9. Implementation decisions deferred until evidence

No new-table decision is made here. Start with documentation/catalog contracts and an evaluation harness after review. Reuse gold facts for reference queries; do not route the agent to unrestricted fact querying by default.

Create a physical entity index if existing sources cannot provide complete discovery without UI qualification. Create period aggregates if approved query semantics are correct but costly/repetitive, or if a stable governed query interface needs them. Introduce metadata retrieval only when table discovery evaluation demonstrates a need; embeddings are optional for this small catalog.

Cross-layer checks should eventually verify metric-to-column mappings, supported operations, source grain, join cardinality, phase isolation, ratio recomputation, qualification boundaries, and temporal evidence ordering. Keep semantic definitions close to dbt code so changes can be reviewed together.

## 10. Review decisions

These decisions are proposed, not approved by starting this draft:

| Decision | Recommendation | Consequence |
| --- | --- | --- |
| Default season phase | Regular Season | Changes current combined-phase summaries; disclose on every answer |
| Ranking policy | Five appearances for count metrics; explicit attempt thresholds for shooting | Project cohort, not official NBA qualification; may ask a short clarification |
| Fantasy terminology | Separate identifiers; default unspecified scoring to `fantasy_proxy_weighted` | Prevents inconsistent “fantasy points” across agent/UI surfaces |
| Canonical period shooting percentage | Ratio of summed components | Changes existing averages of game percentages; needs compatibility review |
| Physical tables | Decide after evaluation | Keeps initial work focused on definitions and measured gaps |

## References

External architecture references inform this proposal; they do not establish that either company uses this NBA schema or these defaults.

- [OpenAI — Inside our in-house data agent, January 29, 2026](https://openai.com/index/inside-our-in-house-data-agent/): context from code, metadata and lineage; reference-query evaluation.
- [Anthropic — Self-service data analytics, June 3, 2026](https://claude.com/blog/how-anthropic-enables-self-service-data-analytics-with-claude): human-owned semantics, documented analytical conventions, and evaluation.
- [Anthropic — Analytics in Slack, August 13, 2026](https://claude.com/blog/self-service-data-analytics-in-slack-how-anthropic-deploys-claude-tag-for-ad-hoc-questions): governed serving data and production feedback.
