# Governed metric semantics

The executable definition is
[`app/agent/semantic_contract.yml`](../app/agent/semantic_contract.yml), version
`nba_semantics/0.1`. The calculation engine validates scope and source evidence
before producing typed results. This guide describes the supported contract;
metric discoverability alone does not imply every question or operation is supported.

## Identity, scope, and evidence

- Player appearances have unique `(season, game_id, player_id)` keys. Game IDs
  remain strings. Names resolve against observed identities; ambiguity requires
  clarification instead of choosing a player.
- Historical team filters use the team on the appearance, not a current profile.
  Season and regular-season/playoff scope remain explicit in results.
- Governed metric queries default to Regular Season. Broad player overviews
  have a distinct scope: unqualified phase includes both regular season and
  playoffs; explicit trailing calendar windows end today and can combine supported
  archives. Answers disclose their dates and phases.
- Appearance windows use recorded appearances; calendar windows preserve their
  dates even when there are no observations. Missing appearances are not zero
  production, and a small sample is not automatically missing source data.
- Corrected historical statistics do not reconstruct what the warehouse knew
  at an earlier instant. Source-through, report, and ingestion times are distinct.

## Aggregation and units

Calculate at full precision and round only for display. Return observed games,
valid games, missing-component counts, and ratio components where applicable.

| Metric family | Rule |
| --- | --- |
| Counting statistics | Sum components for totals; divide by valid recorded appearances for per-game averages |
| Games played (`gp`) | Count recorded appearances at unique season/game/player grain; total only, independent of missing box-score components |
| Minutes | Minutes for totals; minutes per game for averages |
| FG%, 3P%, FT% | Ratio of summed makes to summed attempts |
| eFG% | `(SUM(FGM) + 0.5 * SUM(3PM)) / SUM(FGA)` |
| TS% | `SUM(PTS) / (2 * (SUM(FGA) + 0.44 * SUM(FTA)))` |
| Points/assists per 36 | `36 * SUM(stat) / SUM(minutes)` |
| Fantasy and attributed-points proxies | Explicit versioned formulas in the executable contract; not official league-provider scores |

The engine retains shooting ratios internally. Overview context displays them
on a 0–100 scale, with changes in percentage points. Per-36 rates remain distinct
from per-game averages. A zero denominator is unavailable, not zero percent.
For example, games shooting 1/1 and 0/9 combine to 10%, not 50%.

Null components remain unknown. Partial individual averages disclose their valid
sample; incomplete inputs suppress comparison changes and ranking eligibility.
No missing game, quarter score, or injury status is fabricated.

## Qualification and supported answers

The default count-ranking threshold is five appearances, a project policy rather
than official NBA qualification. Shooting rankings require an explicit attempt
threshold or clarification. Ties share rank; player IDs give stable display order.
Percentiles disclose their eligible cohort; singleton cohorts are unavailable.
Turnover direction distinguishes ball security from a request for most turnovers.

Governed Ask supports metric summaries, rankings, percentiles, comparisons,
and individual metric game logs. Broad overviews and two-player scorecards have
separate presentation contracts described in [Ask workspace](ask-workspace.md).
Unsupported scopes require clarification or an unsupported result. Similarity
and legacy analytical tool paths must not be mistaken for universal governed
semantic coverage. The planner cannot submit arbitrary SQL.

## Source and evaluation boundaries

Warehouse capture uses fixed bounded queries, snapshot identity and completeness
validation. Frozen snapshot completeness establishes retrieval of its warehouse
rows, not independent completeness of NBA source observations. Historical injury
ordering cannot be claimed from ambiguous game times; see the explicit cutoff
in [Player context](player-context.md).

Public synthetic fixtures exercise identity, dates, ratios, missingness, ranking,
and evidence boundaries. Historical evaluations compare against frozen reference
results. Run instructions are in [Evaluation](evaluation.md). Passing fixtures
establishes tested behavior, not universal model accuracy or production deployment.

See [Players and research breakdowns](research-workbench.md) for shared detailed queries, multi-stat teammate studies, and versioned pregame context.

Dynamic availability comparisons preserve focal/exposure roles and attach the
executed request and per-group game IDs to every metric. A teammate predicate
cannot be represented by the ordinary `Query` contract and must never be reduced
to an unfiltered summary. See [dynamic teammate availability](ask-workspace.md#dynamic-teammate-availability)
for source membership, absence, missingness, rate and attribution rules.

Availability policy `availability/2` uses final positive minutes as participation
truth. Sample qualification is separate: the default 50% prior-ten median filter
excludes limited appearances from comparisons, not games-played totals. Five
prior appearances are required; no baseline uses the current or future game.
Both participating players are checked, and every scoped record is reconciled
once. Zero-minute verified membership and final DNP evidence can establish
non-participation; null/missing minutes cannot. See the Ask guide for adjustable
thresholds, inclusion overrides, source verification, and policy versioning.

Explicit phase requests require source coverage for every requested phase.
Default availability comparisons may use the available phase only when the
answer and chart label it and disclose missing phases; follow-up scope carries
the covered phases. Classification provenance identifies either the supporting
statistics snapshot/game/player key or the actual final-box-score/Out-report
source. Injury bulletins are not evidence for statistics-based classifications.
