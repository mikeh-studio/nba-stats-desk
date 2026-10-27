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
  All Ask spelling matches use identities from the route's source roster only.
  Clear minor full-name typos may resolve under `player_name_match/1`, with the
  name used disclosed in the answer. Partial names and competing identities
  require clarification; no credible match returns unavailable without statistics.
- Historical team filters use the team on the appearance, not a current profile.
  Season and regular-season/playoff scope remain explicit in results.
- Governed metric queries default to Regular Season. The bounded correction of
  a provably descriptive study-kind plan applies `ResearchQuery`'s Regular Season
  default instead of the inherited study Both default. Correctly classified
  model-authored breakdown phases remain unchanged, including Both.
  Broad player overviews
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

Two-player comparisons retain the same definitions for shooting efficiency,
turnovers and points/assists per 36 minutes. Additional comparison sections bind
both sides to the scorecard's dates and phases, retain metric components and
valid/observed appearances, and withhold differences for partial inputs or zero
denominators. Percentage differences are percentage points. Lower turnover
averages are described as lower rather than an overall playmaking advantage.

## Qualification and supported answers

The default count-ranking threshold is five appearances, a project policy rather
than official NBA qualification. A question that is exactly “who led/leads the
league in <counting stat> [season] [phase]” is planned without the model as a
per-game ranking; regular-season leaders require appearances in at least 70% of
the most games any team has in the scoped evidence (58 of 82), the NBA's
per-game leader standard. Facts and aggregate serving use the same qualification;
serving derives the game count from its validated publication appearance metadata.
A model plan that compares two seasons' leaderboards for a one-period question
without comparison wording keeps the selected season only when it is the sole
requested season. Explicit multiple seasons are preserved for validation, including
when joined by “and”; unsupported comparisons return no statistics.
Shooting rankings require an explicit attempt
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
in [Player context](pages.md#player-context).

`BigQuerySemanticSource.capture(seasons)` keeps the ordinary all-phase capture
contract. Call `capture_scoped(seasons, phases)` for explicit canonical phases, or
wrap the source in `ScopedSemanticSource` for the warehouse loader protocol. Each
UNION branch constrains season and phase with typed parameters before retrieval.
Coverage records only observed scopes; a regular-season snapshot cannot certify
playoff coverage. Cache identity includes source mode, relation configuration,
phase scope and trusted policy, as well as seasons. Retrieval timestamp, count
checks, checksum, null validation and truncation rejection still apply.

Trusted server composition may install an immutable `SourcePolicy` before app
lifespan with `configure_source_policy`, or propagate it through
`source_policy_scope`. It binds season/relation pairs, canonical phases, snapshot
timestamp, explicit identity IDs, source mode, row/byte caps and timeouts. No
request payload configures it. It applies without Ask request tracing, including
to startup and health reads. Conflicting process, context and repository policies
fail closed. A restricted repository denies ordinary SQL and legacy identity
fallbacks; only complete generated capture and fixed ID-bounded identity query
shapes with matching typed parameters may dispatch. This comparison is not a
general SQL parser or an arbitrary-SQL rewrite. Destination jobs, scripts, unknown
job options and reads above the configured byte cap are rejected. Optional
position-context artifacts are not approved by this source policy and fail closed.

The explicit `retained_local_review` mode constructs `RetainedReviewSource`
directly from approved relation bindings. It never probes an archive or falls
back to ESPN, another season, or the serving cache. Its validated snapshot/output
carries the retained relations, a local-review note and
`archive_status: unavailable`; selecting this mode asserts the operator-reviewed
archive limitation and does not repair archive publication. Ordinary archive and
optional ESPN routing remain separate. Source policy alone does not enable a
local-only HTTP ingress requirement; application composition must enforce that
boundary when exposing a retained review session.

Identity lookup reuses an already loaded complete scoped snapshot when available;
otherwise it sends one fixed query to the approved relation, season, phases and
IDs with the same snapshot timestamp and limits. A plain overview may add a
prior period as optional context. Under a source policy, unapproved seasons used
only by that optional context are omitted before retrieval when a complete, simple
scoped-overview form is recognized. Additional or ambiguous modifiers retain the
prior dependency and fail closed when it is unauthorized. Explicit year-over-year
comparisons cannot become optional context. The requested dates,
phases and required seasons remain unchanged. Previous-period metadata is retained
so missing evidence withholds changes and baseline insights. Explicit baseline
requests remain subject to source authorization. Conflicting names and truncated
results fail without fallback. Duplicate names with distinct IDs remain ambiguous.
Name-resolution rosters contain only approved identity IDs; statistical cohort
rows remain complete. A single approved identity season is independent of the UI
shell's default season. For multiple approved seasons, callers must choose an
approved season explicitly or through the repository's selected season. Synthetic
IDs exercise these contracts; real identities and source access require separate
source verification.

Public synthetic fixtures exercise identity, dates, ratios, missingness, ranking,
and evidence boundaries. Historical evaluations compare against frozen reference
results. Run instructions are in [Evaluation](evaluation.md). Passing fixtures
establishes tested behavior, not universal model accuracy or production deployment.

See [Players and research breakdowns](pages.md#players-and-research-breakdowns) for shared detailed queries, multi-stat teammate studies, and versioned pregame context.

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

Player split evidence uses `player_splits/1`. The ordinary `Query` also accepts
`home_away=HOME|AWAY`; unknown venue values in the requested player/date/team/
opponent scope block a venue-filtered result. `Both` does not erase a phase-versus-
phase comparison. Explicit split groups execute independently under the same
metric contract. Incomplete components suppress differences even when a partial
individual average can be displayed. Percentage changes remain percentage points.

Warehouse home/away follows official schedule designation when available. A
neutral-site game can have a designated home side; this field does not establish
home-court advantage or physical venue. Repaired schedule assignments retain
source hashes and a change ledger.

Award lookup is separate reference evidence, not a metric calculation. The
versioned award catalog supplies aliases, scope, season, NBA player ID and source
provenance through one shared resolver. Winner-only answers require no warehouse
coverage. Performance composition verifies identity and preserves award season;
missing statistics never change the winner. MVP variants are distinct awards;
unknown awards and unsupported conditions must not become player clarifications.
Finals-only performance is withheld until round scope is connected. Tests in
`tests/test_award_lookup.py` cover every catalog award and season, aliases,
lookup-only and composed answers, follow-up scope, stale context, provenance,
unsupported variants and JSON/SSE parity.

Broad player performance questions (including “how did he perform/play/do” and
“how has he been doing”) use the full overview when no specific metric or
unsupported condition is requested. The generic metric planner cannot silently
replace basketball performance with a fantasy proxy. Fantasy queries require
explicit intent or a metric-preserving continuation of an existing fantasy
analysis; otherwise Ask requests clarification before executing the query.

Overview prose is deterministic and shared between the API answer and the UI
through `semantic_evidence.summary_paragraphs`. Broad responses lead with findings;
`summary_context` identifies a final scope paragraph (including baseline game
count and distinct peer qualifications) for compact UI rendering without changing
the canonical answer. Summary comparisons use the
already-validated metric changes, never differences between rounded display
values. Partial metrics cannot support headline, efficiency, or minutes claims.
Percentages and per-36 rates are not appended to a per-game sentence; the full
values and their units remain available in the detailed evidence and tables.

Overview comparison scope includes `previous_phases`, alongside explicit date
bounds and a display label. Full playoffs use same-season regular-season
appearances as the baseline; source coverage is checked independently for both
phases. `semantic_evidence.game_insights` records the scoring distribution and
peak game identity used in focused scoring prose. Broad prose selects supported relationships across scoring opportunity, efficiency,
creation and non-scoring activity. It does not require every value in the prose.
`semantic_evidence.insights` records selected claim IDs and supporting metric keys.
Peer prose requires versioned qualification and matching cohort distribution data. Summary and
takeaways share comparison eligibility and the display-change threshold; raw
changes cannot bypass an explicitly suppressed `display_change`. No new metric
or composite impact calculation is introduced. Distribution claims require complete
scoring coverage. Round-specific requests fail closed before querying evidence;
season-wide observations cannot stand in for a requested Finals or playoff round.

## League reference and similarity contracts

`league_baseline/2` compares a governed individual summary with pooled player
appearances in the explicitly disclosed shared calendar scope. It includes the
focal player, uses a one-appearance minimum, aggregates the complete cohort without
top-N truncation, and withholds differences if either sample has incomplete
components. Last-N calendar bounds are the first and last selected player appearance.
Version 2 replaces the full peer membership and repeated league game IDs with a
count and deterministic membership digest, avoiding season-sized browser payloads. See [Ask workspace](ask-workspace.md#governed-similarity-and-league-references)
for supported language and weighting. Dashboard baseline values are not reused.

`similarity_reference/1` validates a separate published feature-profile result;
it is not an ordinary metric query. The profile must match the selected identity
and season. Self/duplicate neighbors and missing, nonfinite or out-of-range scores
invalidate the answer. No model-authored similarity numbers or filter substitutes
are accepted. Publication hashes identify returned evidence, not an independently
verified training run or model build version.

### Recent statistical similarity and verified nonparticipation

`recent_statistical_similarity/1` compares complete last-N appearances using
PTS/REB/AST by default, equal standardized metric distances, explicit cohort
membership and per-player date bounds. It is separate from published season
profiles and does not imply playing-style similarity. Its returned score is not
interchangeable with the season-profile score.

`event_participation/1` carries explicit sourced reference records. Verified
nonparticipation yields `not_applicable`, not an observed zero and not generic
missing data. Catalog absence means unknown; round-specific statistics remain
unsupported. See [Ask workspace](ask-workspace.md#governed-similarity-and-league-references).

Availability questions accept “how many points … average”; award lookups accept
“Name the MVP winner” and “Who received MVP”. These wording variants preserve
the same bounded scope checks; unsupported additional constraints still refuse.

Overview presentation retains full aggregates but withholds displayed changes
when either comparison sample has fewer than five valid appearances or partial
components. Ratio sample notes disclose pooled attempts; the games floor does
not establish shooting ranking eligibility or statistical precision. See
[Ask overview sample policy](pages.md#ask-overview-metrics-and-samples).


The planner contract `semantic_intent/3` makes aggregation provenance server-owned.
The planner's `aggregation_source` is bounded advisory metadata; missing or
incorrect labels cannot authorize or veto execution. The server derives
`aggregation_resolution` from current explicit intent, compatible previous summary
scope, the approved omitted default, or an existing metric/operation convention.
Concrete values that contradict explicit or inherited aggregation still fail.
Planner-only fields are stripped before constructing `Query`. Noneligible
model-selected semantics remain identified as `planner_selected`; this is not a
new default for other metrics. GP, game logs and ratios retain their constraints.

An explicitly named player's single dated PTS summary with omitted aggregation
uses **points per game**. Canonical identity, metric aliases and calendar/date
scope determine eligibility independently of proposed query fields. One shared
literal interval resolver accepts ISO date-to/through-date and
between-date-and-date forms, regardless of introductory from/over/during words.
Recognized unresolved or competing date anchors require clarification; reversed
or invalid literal intervals refuse with `invalid_scope`, rather than executing
an invented total. Existing calendar resolution is reused. Contradictory
identity, metric, operation, dates, season, phase or extra filters fail before
literal scope replacement. Explicit total/average cues cannot be erased by model
provenance. A partial follow-up retains a compatible previous aggregation;
a complete named metric/time request (including a season) may start fresh.
Supported explicit totals and averages retain their ordinary filters. Recognized
negated aggregation/phase phrases require affirmative clarification; they never
establish the opposite intent. Percentage and GP conventions
are not replaced by an incompatible inherited count aggregation. A prior GP or
game-log total is an operation convention, not transferable user aggregation
intent. Per-game count charts retain game-log total values; percentage rankings
retain ratios when a threshold describes total attempts. “Total rebounds per
game” names the combined rebound statistic at a per-game rate.

The existing open-ended “since [month year]” path is outside the closed-interval
default. Its per-game plan must match the independently resolved month start;
model-selected totals or unspecified aggregation clarify. Existing source-anchor
and model-proposed end-date handling remain in effect: this legacy path does not
independently establish the upper bound. It reports `legacy_average_compatibility`
resolution, not an unconstrained planner choice. Additional date anchors or
numeric windows cannot use this path to bypass interval checks. Ambiguous terse
clarification replies cannot execute an uncued guessed total; a complete named
dated request or explicit aggregation reply can resolve that ambiguity. Existing resolved rolling windows likewise retain
their separate semantics without acquiring the dated-PTS default.

Recognized “how many points” requests clarify total versus per-game unless the
aggregation is affirmative. Omitted dated PTS with a recognized opponent filter
also clarifies, including when a planner drops that filter. Explicit supported
totals/averages retain their filters. Road and away games share the canonical
AWAY scope. Mixed regular-season/playoff language requires affirmative combined
intent rather than silently selecting Playoffs.

Recognized unsupported predictions, causal calculations, rolling calculations and
additional game filters are withheld. These literal guards are not a complete
natural-language parser: arbitrary unfamiliar wording still depends on model
interpretation. There is no whole-question word whitelist or universal language
accuracy claim.

The unchanged arithmetic divides valid points by valid recorded appearances:
zero is valid, missing points remain unknown, and valid/observed counts are
visible. Defaulted answers disclose the choice through assumptions and
`query_plan.aggregation_policy` (`dated_points_per_game_v3`), including the actual
model `original_aggregation`, chosen average, unit and validated scope. A validated
omitted request resolves total, average or unspecified model proposals to average;
a ratio proposal remains invalid for PTS. Diagnostic model labels remain separate
from authoritative `aggregation_resolution` provenance.
Explicit averages receive no default disclosure.

Broad overview windows up to 31 inclusive days use game-date chart buckets; longer
windows use monthly averages. Grain is explicit in scope, charts and metric trend
evidence. Named month/year and dated week scopes resolve deterministically; a
partial or conflicting calendar phrase requires clarification. A month without a
year uses the documented conversational default, disclosed in the answer. Calendar-month
comparisons use the preceding full calendar month. Table abbreviations and change
colors do not alter canonical metric values, sample rules or full-name evidence.
See [Ask overview presentation](pages.md#performance-over-time).

Overview presentation has a separate, versioned percentile qualification policy:
five complete appearances, at least two qualified players including the focal player,
and provisional attempt floors of 100 FGA for FG%/TS%/eFG%, 50 FTA for FT%, and
50 3PA for 3P%. TOV ranks lower values favorably; MIN ranks exposure. The general
overview shows eleven metrics with FG%/FT%/TS% last. Direct governed ranking
queries retain their explicit attempt-threshold contract. See
[overview policy](pages.md#samples-and-comparisons).


## Metric distributions

Overview ranked metrics carry `ask_metric_distribution/1` evidence. Among players
uses exactly the same qualified cohort as the percentile, with pooled percentages
or per-game averages. Peer histogram counts must sum to cohort size. A finite
unqualified focal value is shown as a dashed marker outside the cohort, without
assigning a percentile. Median, quartiles and extrema use linear interpolation at
`(n - 1) * p`; quartiles describe observed spread, not confidence intervals.

Across games uses individual valid recorded appearances in the same scope.
Missing components and zero denominators are omitted and counted separately;
recorded zero-event counts remain observations. Shooting game medians are distinct
from pooled period percentages; per-game attempts accompany the observations.
TS% observations expose both FGA and FTA. A compact game selector supports touch
and keyboard inspection alongside the chart dots.
Dots are bounded to the latest 300 valid appearances, with truncation disclosed;
summary statistics and histograms always retain all valid observations. Integer
count distributions retain discrete bins when the range has at most 41 values;
otherwise histograms use 12 equal-width bins. Membership follows the emitted
left-inclusive boundaries; the final bin includes its maximum.
Constant samples use one bin and empty distributions remain empty.

The percentile cell opens a native modal metric drawer without expanding table
rows. It is a 440px side panel on desktop and a bottom sheet capped at 78dvh/640px
on phones. The initial view is Among players; Across games is a second toggle.
Escape/close return focus to the originating metric. The TS% calculation tooltip
remains separate. Saved responses without distribution evidence retain their
existing controls and values; the browser never reconstructs distributions.


## Calendar metric archive binding

A fresh resolved metric date range binds all overlapping supported seasons to
`Query.seasons`, with the same explicit dates in the evidence and renderer.
Required archives are loaded before calculation; an unavailable archive cannot
be replaced by a partial-period value. Explicit named seasons constrain dates,
and inherited season ordering/identity remain intact. Resolved rolling-year
phrases use request/as-of anchored dates; existing source-anchored numeric-month
queries keep their established window rules. Month comparisons retain separate
validated per-query periods and load their required archive union. Source loads
remain bounded to the supported archive allowlist.

### Position Overview references

Overview position references are additive under `ask_position_peers/3`; existing
league percentile fields retain their arithmetic and distribution. Membership
requires independently reviewed historical listed positions, metric-specific
eligibility and explicit cohort scope. See [position policy](overview-insights.md#position-comparisons-and-focused-profiles-v3)
for mapping, provenance, coverage, ordinal/percentile gates and private tracking.


### Overview variation and requested-peer fulfillment

See [question-first comparisons and variation](overview-insights.md#question-first-comparisons-and-variation-v4)
and [request fidelity](overview-insights.md#request-fidelity-and-review-regressions)
for the shared contract. An unavailable explicitly requested cohort is partial,
never an answered league substitute. Game spread and dated highs use the requested
metric and full scoped sample; all displayed comparisons retain their reference.


Frozen v1 teammate-study answers preserve imported original-sample, complete-case
raw and adjusted assists estimates as distinct claims. All differences are reported
Out with no appearance minus participated, in assists per game. Nominal cluster
intervals and p-values remain diagnostics, never validated significance or causal
evidence. `teammate_study_claims/1` binds displayed imported values to their frozen
source paths and scope; it does not recompute the offline model. Metric units and
methodology are v1 exporter invariants. `contrast_basis` distinguishes an explicit
validated source declaration from the assumed v1 contrast when omitted. See the
[serving contract and remaining compatibility boundary](ask-workspace.md#factual-output-reachability).

## Presentation provenance

[Exact presentation bindings](ask-claim-bindings.md) preserve raw aggregate values
and declare scale/rounding separately. A recorded zero is a valid observation;
null is missing, and games played remains independent of metric completeness.
Two played games with 0 and 10 points average 5; a null and 10 points average 10
over one valid game out of two observed games. Zero field-goal attempts do not
imply zero points. Pooled zero attempts produce unavailable shooting efficiency.

The opt-in [local ESPN review adapter](ask-espn-local-review.md) supplies mapped,
accepted dev box-score evidence with explicit partial coverage. It disables
league cohort claims and preserves provider-specific game keys.

## Agent serving preparation

See the [Ask serving contract](ask-serving-contract.md) for source-certified
publications, the metric inventory, opt-in Phase 2 routing, request telemetry,
and parity checks. Routing and deterministic planning remain off by default.
Activation requires the contract's fresh measurements and explicit rollout gates;
local and fixture validation do not establish production readiness.
