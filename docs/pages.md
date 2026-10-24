# App pages and service

Routes, page behavior, and the contracts behind Players, Performance, Trending, Profiles, and Ask's overview presentation. Ask planning and evidence rules live in the [Ask workspace](ask-workspace.md).

- [Public Service](#public-service)
- [Players and research breakdowns](#players-and-research-breakdowns)
- [Player context](#player-context)
- [Ask overview metrics and samples](#ask-overview-metrics-and-samples)
- [Trending](#trending)
- [Statistical Profile Similarity](#statistical-profile-similarity)

## Public Service

The FastAPI service is intended for Cloud Run and serves HTML plus JSON from the
same process.

### HTML Routes

- `/`
- `/players/{player_id}`
- `/ask`
- `/performance`
- `/what-changed`
- `/similarity-map`
- `/compare`

`/` redirects to `/ask`. `/visualize` redirects to `/performance` because the
old Visualize page has been removed.

### JSON Routes

- `/api/what-changed` (`period=four_games|week`, `season_type=Regular Season|Playoffs`, optional `as_of=YYYY-MM-DD`)
- `/api/rankings`
- `/api/players/search`
- `/api/players/{player_id}`
- `/api/compare`
- `/api/performance/dates`
- `/api/performance/games`
- `/api/performance/initial`
- `/api/performance/players`
- `/api/performance/players/{player_id}`
- `/api/similarity-map`
- `/api/similarity-map/neighbors/{player_id}`
- `/api/agent/ask`
- `/api/agent/ask/stream`
- `/api/agent/history` (local-only history, empty unless enabled)
- `/api/health`

The retired `/api/leaderboard`, `/api/trends`, `/api/analysis/latest`, and
`/api/recommendations` endpoints return 404. Clients should use the retained
endpoints above; no stale compatibility responses are served.

### Data Access

The service reads only from gold, agent, and metadata datasets. It is public
read-only for v1 and does not include auth.

`/api/health` reports stats, injuries, and similarity independently from
`nba_metadata.pipeline_run_log`. Each asset includes its last successful
publication time, latest source data date, and last attempted publication status.
Skipped/no-change runs do not reset publication time or clear a previous failure.
Historical logs are supported; unavailable historical source dates remain unknown.

An optional asset failure produces `partially_updated` while valid core stats
remain available. The expandable data-status panel appears on every app page;
it identifies retained previous versions and their last successful refresh.

Freshness uses an explicit offseason window in `America/New_York`:

- `NBA_FRESHNESS_OFFSEASON_START` defaults to `2026-06-20`, after the last
  scheduled possible Finals game.
- `NBA_NEXT_REGULAR_SEASON_START` defaults to `2026-10-20`, the next opening day.
  Calendar source: [NBA key dates](https://gleague.nba.com/key-dates).
- During that window, existing data is an `offseason` archive, with
  `updates_expected=false` and `is_fresh=null`. Actual failed refresh attempts
  remain visible. Preseason and Summer League do not end the pause.
- Starting on opening day, `awaiting_refresh` allows the configured freshness
  threshold (default 36 hours) for the first ingestion. After that, old source
  data is overdue even if a recent pipeline run completed successfully.

This policy does not change the supported ingestion season or restart the DAG:
the current DAG still ends on July 1, 2026, and the SQL/app still serve 2025-26.
Season rollover must update those contracts separately. Refreshing the calendar
alone cannot make old-season data current. Update the two calendar settings
from the official schedule when rolling the pipeline forward.

### Player and Compare Experience

Player pages render game logs, recent trend context, percentile summaries,
archetype context, and similar-player panels when the sample is stable.

The compare page supports direct `player_a_id` links and first-player search
when no initial player is provided. Compare responses can include a similarity
block with pair score, shared traits, and contrasting traits. Supported windows
include last-game presets plus regular season, first-half regular season,
second-half regular season, and playoffs.

The performance page shows 2025-26 playoff player game rows against each
player's own season baseline, excluding players below one minute. It exposes
date and game filters, status sorting, signed P-Rating, minutes, FG%, FT%, and
3PM. Clicking a row opens a lightweight Player View modal from the loaded row
data; the full player page remains the detail destination. The default
performance payload is prewarmed on app startup when
`PERFORMANCE_CACHE_PREWARM_ENABLED` is true.

### Provider-Selectable LLM Stats Agent

`/ask` is an LLM-backed stats agent over curated warehouse outputs. Set
`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, or `OPENROUTER_API_KEY` for the corresponding
provider. OpenRouter setup and its current validation limits are documented in
[Ask workspace](ask-workspace.md#openrouter). Use `/api/agent/ask` for blocking JSON responses and
`/api/agent/ask/stream` for SSE streaming.

The request path is intentionally bounded:

1. Build a query plan, with deterministic routing as fallback.
2. Resolve player names from `BQ_DATASET_AGENT.agent_player_search`.
3. Gather evidence through allowlisted application tools.
4. Render governed answers deterministically, or ask the selected provider/model
   to compose the final answer for other supported tool flows.
5. Select a chart from verified candidates where available; chart values and
   comparison calculations remain application-owned.

Allowed tools cover player resolution, game logs, trends, opponent splits,
percentiles, rankings, similarity, and metric leaderboards. The agent does not
receive BigQuery credentials and cannot run arbitrary SQL.

Trend questions preserve the user's requested window. `last 30 days` and
`past 10 weeks` anchor to the latest available game date in the supported
season, choose a game/week/month breakdown from the question, and compare
against the previous equivalent period unless the user asks for league baseline.
Structured trend evidence includes period summaries, bucketed rows, comparison
deltas, and chart payloads.

Operational controls:

- `OPENAI_AGENT_MODEL`, `ANTHROPIC_AGENT_MODEL`, and `OPENROUTER_AGENT_MODEL`
  set provider defaults. The OpenAI default is `gpt-5.6-luna`; see the
  [model comparison](evaluation.md#openai-default-model-comparison).
- `AGENT_MAX_TOOL_CALLS` bounds one request's evidence loop.
- `AGENT_RATE_LIMIT_PER_MINUTE`, `AGENT_RATE_LIMIT_DAILY`, and
  `AGENT_QUESTION_MAX_CHARS` protect public endpoints.
- `AGENT_RATE_LIMIT_REDIS_URL` enables Redis-backed limits for scaled Cloud Run;
  local and test runs use an in-memory fallback.
- Browser history stays in `localStorage`; optional server JSONL history is
  disabled by default and should stay under ignored `local_notes/`.
- Ask generates its own request ID, returned in `X-Request-ID`; incoming IDs are
  not reused. Service summaries log operational metadata, not questions,
  conversation IDs, tool arguments/results, or raw provider exception bodies.
  Rich tool details remain in the response and optional local history.

Governed metric queries use `app/agent/semantic_contract.yml`; see
[Metric semantics](semantic-contract.md). Legacy analytical tools retain
`app/agent/semantic_catalog.yml` presentation and alias metadata. Base metrics map
to curated gold fields. Derived metrics use safe arithmetic formulas over
approved stat keys, such as:

```yaml
formula: "pts + ast * 2"
```

Shooting efficiency is exposed as derived percentage metrics scaled to 0-100,
so their deltas read as percentage points: `fg_pct` (`fg_pct * 100`), `fg3_pct`
(`fg3m / fg3a * 100`), and `ts_pct` (`pts / (2 * (fga + 0.44 * fta)) * 100`).
A metric's `unit` (`count` or `percent`) drives formatting and keeps percentage
lines off the counting-stat chart axis. `plus_minus` records observed on-court point differential;
it is not isolated
player impact.

Each metric has a `tier` (1-4). A vague "stats" question resolves to the
default cohort — tiers 1-2, the traditional box score
(`catalog.default_metric_keys()`) — used for game logs and charts. The
"how have their stats changed" and "who did they struggle against" tools
instead default to a curated impact set (`catalog.analysis_metric_keys()`:
scoring, rebounds, assists, blocks, shooting efficiency, and plus-minus).

"Struggled against" is intentionally not a raw-volume sort: the toughest
opponent is the lowest composite **struggle score**, a weighted blend of
shooting efficiency and plus-minus expressed as z-scores against the player's
own window average. A team can therefore grade as the toughest matchup on
efficiency and impact even when raw points look fine, and the tool returns a
per-game drill-down (shooting line, TS%, plus-minus) for that opponent.

#### Debugging Ask failures

Use the returned `X-Request-ID` to find the matching service summary and inspect
its outcome, error type, model, and timing metadata. Failed Ask executions do not
save a turn to optional local history; their generic error responses and service
logs do not preserve the failed payload or traceback.

For deeper diagnosis, reproduce locally with a sanitized or synthetic question.
Inspect the failure with a local debugger; keep any diagnostic artifacts in
ignored private storage. Do not restore raw exception or request logging in the
public service or copy private diagnostics into issues, PRs, or fixtures.

## Players and research breakdowns

Player detail pages, comparison pages, and research requests in Ask share
`app/research.py`. Ask plans an allowlisted scope; Python calculates and renders
results. It does not accept generated SQL or model-calculated statistics.

The **Players** navigation entry (`/players`, with `/research` retained as an
alias) opens a searchable player directory. A starting selection uses the existing
season fantasy recommendation rankings (up to 12 players); name searches use the
player search API. Cards link to season-preserving profiles and label their
observed sample; missing samples are not filled in. Search submits explicitly,
with loading, empty, retry, and clear-search states. The landing page does not display breakdown
controls or the three pilot study cards; those studies support comparisons in Ask.
Player profiles return an immediate loading shell, then fetch a season-scoped
HTML content fragment. Unknown players and failed loads show explicit states,
with retry for connection failures. Profile warehouse reads run concurrently in
a shared pool capped at eight workers; identity and dependent similarity reads
retain their ordering. The existing player cache is shared by fragments and the
JSON API. Detailed research initializes only when its disclosure is opened.
Player profiles retain their detailed research panel; causal questions are
answered in **Ask**. Compare remains accessible by direct links without a
separate navigation tab. Internal research API and configuration names remain
unchanged. The shared navigation omits a season selector. Historical page URLs
redirect to the latest view; explicit historical Ask questions and API reads
remain supported.

### Breakdown contract

One season, one or two player IDs, regular season/playoffs/both, inclusive dates,
opponent, venue, rest, and an optional reviewed teammate-status filter. Counts use
per-appearance averages or totals; shooting percentages use summed makes and
attempts. Valid/observed samples, missing filter context, underlying games, source
identity and query hash remain inspectable. Missing values are never zero-filled.
The workbench includes PTS, REB, AST, STL, BLK, TOV, 3PM, MIN, +/−, shooting volume and
percentages, TS%, eFG%, and descriptive points/assists per 36 minutes.

Rest falls back to the observed team-game ledger, which may be incomplete. Date
filters describe corrected historical statistics; they do not assert what was
known at that historical time. Unsupported scope is rejected rather than silently
approximated. URL filters can be shared. Existing headline cards retain their own
scope; the shared research panel is the consistent detailed view.

### Studies and identification

The historical pilot catalog contains LeBron James/Luka Doncic, Jalen Johnson/Trae
Young, and Jalen Brunson/Josh Hart. These fixed research artifacts do not restrict
the [dynamic availability path](ask-workspace.md#dynamic-teammate-availability),
which calculates descriptive comparisons for source-resolved pairs on demand.
Each pilot study shows all nine core outcomes and
eight shooting context measures. Shooting ratios are descriptive. Positive
contrasts mean more of a statistic, including turnovers.

Unqualified teammate questions default to the entire selected season, including
regular season and playoffs. A partial pilot window must not answer a full-season
question; it is reusable only when the requested dates match. Explicit phase and
date requests remain exact and require a matching published study. Historical
season fallback preserves this requirement.

Study specifications accept `phase` (`Regular Season`, `Playoffs`, or `Both`)
and `window` (`bounded`, the legacy default, or `full_season`). A full-season build
checks the supplied complete team schedule against its dates and requires team
box scores for every scheduled game. The capture must include the full season;
this check cannot establish that an upstream schedule itself is complete. Roster
intervals and pregame report requirements still apply through the postseason;
traded-away players are not classified as absent teammates. Missing status games
are excluded and disclosed in plain language. Rebuild immutable artifacts rather
than extending the dates on old estimates. Legacy catalogs without phase metadata
remain regular-season, bounded-window analyses.

Descriptive/adjusted contrasts compare reported Out with no appearance against
participated, conditional on focal appearances. Missing roster/report evidence is
unknown. Adjusted OLS associations never receive a causal label.

The offline estimator accepts a separately reviewed eligible scheduled-game panel
with verified pre-exposure covariate timing, treatment and outcome completeness.
The specification must bind the exact pair, window, source hashes and panel hash.
It declares `eligible_game_ids`, a pre-exposure `eligibility_rule`,
`treatment_definition: unavailable_minus_available`, and
`estimand: eligible_scheduled_game_ATE`. Observed outcomes must match frozen facts;
verified nonparticipation requires explicit source references.
It documents consistency, exchangeability, positivity, interference, and selection
assumptions. Episode-grouped cross-fitting, propensity overlap, effective sample,
balance, and episode-support gates precede AIPW estimates and approximate clustered
uncertainty. No missingness model is implemented: incomplete eligible panels fail.
Holm p-values cover the predeclared 27 core hypotheses, including unavailable
slots; displayed intervals are pointwise. Leave-episode-out score sensitivity does
not refit nuisance models. These diagnostics cannot verify unmeasured confounding.
Real causal estimates require review beyond passing synthetic recovery tests.

Build a private catalog with repeatable descriptors:

```sh
python scripts/build_research_studies.py \
  --input reports/research/pair-descriptor.json \
  --output reports/research/unique-run/catalog.json
```

Repeat `--input` for each pair. Add `--context-output reports/research/unique-run/context.json` to export reviewed postgame status filters. Each JSON descriptor supplies `pair_id`, `spec`,
`snapshot`, `schedule`, `injuries`, `memberships`, optional `context` SQLite, and
optional `causal_rows`/`causal_spec` paths. Formats follow the existing
[Ask workspace](ask-workspace.md) frozen-input workflow. Catalog publication is
create-only and checksum-verified. Unpublished pairs remain visible as unavailable.
Membership continuity assumptions must be recorded; source review is not equivalent
to human identification review. Do not tune a study window after inspecting effects.

### Configuration and pregame capture

- `RESEARCH_SNAPSHOT_PATH`: optional validated stats snapshot; otherwise the existing
  bounded warehouse adapter supplies evidence.
- `RESEARCH_CONTEXT_PATH`: optional immutable context snapshot for status filters.
- `RESEARCH_STUDIES_PATH`: multi-metric catalog. Raw panel rows remain private.
- `RESEARCH_CAPTURE_DIRECTORY`, `RESEARCH_MEMBERSHIPS_PATH`, and
  `RESEARCH_CAPTURE_SEASON`: the separate, opt-in Airflow context capture job.

`nba_research_context` is paused on creation. It polls every 30 minutes when
activated and supports 2025–26/2026–27 independently of successful game-log refresh.
It needs durable private filesystem storage; ephemeral container disks are
insufficient. No cloud bucket, deployment, scheduler activation or paid call is
part of local setup. New-season capture does not publish a new season in the app.

```sh
python scripts/capture_research_context.py \
  --season 2026-27 --memberships reports/research/memberships.json \
  --output /private/durable/research-captures
```

Captures retain parsed extracts, validated rows, full contract failure counts,
quarantine, original acquisition times and hashes. Failures remain failures, while
prior valid input versions from the latest 96 captures can be retained as stale.
Game tipoff is separate from capture time. The schedule contract accepts missing
legacy tipoff values, which cannot support pregame replay.

Per-game cutoffs are no later than 30 minutes before scheduled tipoff. Captured
inputs and membership review/publication must precede that cutoff; retrospectively
reconstructed evidence is labeled separately. A pregame Out report alone never
becomes a claim that a player did not appear. This release stores immutable
versions; a warehouse-wide knowledge-as-of replay and outcome reconciliation
service are not implemented. Configure status-filter context from reviewed
postgame panels, not from missing pregame appearances.

### Validation

`tests/test_research*.py`, `tests/test_causal_estimation.py` and the JavaScript suite
cover scope, numerical parity through JSON/SSE endpoints, missingness, temporal
isolation, immutable concurrent writes, and synthetic causal recovery/refusal.
Use the [validation guide](operations.md#validation) for the full application and pipeline
checks. Fixture/API/browser success does not establish live warehouse coverage,
prospective capture reliability, or real-world causal identification.

Plus-minus is team points minus opponent points during the focal player’s court
time. It is a signed outcome in every study, including adjusted and gated causal
estimation. Negative values are valid; missing values remain unavailable. It is
not possession-adjusted net rating or an isolated measure of individual impact.
Catalog version `multi_metric_studies/v3` requires plus-minus and the expanded
27-hypothesis family; rebuild older catalogs rather than reusing their correction.

### Ask takeaways and uncertainty

When only `AGENT_TEAMMATE_STUDY_PATH` is configured, matching requests retain
the legacy frozen-study route and its scope checks. A configured research catalog
takes precedence for research requests; active research follow-ups never fall back
to legacy evidence.

Ask is the primary conversational surface. It resolves the registered pair and
scope, reads a matching immutable analysis, and assesses and ranks its metrics on
every request. Model fitting remains in the offline builder: no arbitrary SQL,
unreviewed window, or new identification assumption is accepted from chat. A
missing matching analysis produces an explicit unavailable result. Rebuild the
catalog to populate the new observed-uncertainty fields; older v3 catalogs remain
readable but cannot receive observed significance labels.

Up to three requested metrics are highlighted by available estimates, assessable
uncertainty, stability, and native effect size relative to product thresholds,
never by smallest p-value. When uncertainty is unavailable, a fixed basketball
metric priority replaces magnitude ranking so noisy rare stats do not dominate.
Ask leads with a short basketball takeaway, up to three observed changes, and
one shared pair of game counts when the available metrics agree. Group labels
are “Both played” and the named teammate “out”; participation does not establish
shared court minutes. Thin samples receive a short plain-language caveat, and
every answer keeps its season and dates. Missing values stay unavailable.

All requested metrics remain in a compact expandable stat comparison. Internal
significance, adjustment, causal, episode, and product-threshold diagnostics stay
in the immutable study; per-metric assessment statuses and the catalog hash are
logged on the server without question text or raw game rows. Ask payloads omit
those diagnostics and the raw study. User-facing differences always describe the
observed groups, even when a separate causal estimate exists. Explicit metric
follow-ups retain the pair and date scope, including plus-minus. Significance,
representativeness, and absence-episode follow-ups preserve the study route.

Observed contrasts use a game-weighted mean difference and episode-cluster score
variance with a t interval. At least eight exposure episodes and three per arm
are required; incomplete outcomes or degenerate uncertainty withhold inference.
Episodes are consecutive exposure runs, not verified independent experiments;
inference is exploratory and assumes independence between those runs. Games from
one absence do not count as multiple independent episodes. Pointwise 95% intervals
are not simultaneous confidence intervals. Separate Holm families cover the 27
observed and 27 causal core hypotheses, retaining unavailable slots. Nominal OLS
p-values never become validated significance labels. Shooting ratios remain
descriptive without significance tests.

Evidence includes arm-specific games and episodes, missing outcomes, excluded
scheduled games, largest within-arm episode share, and descriptive chronological
half differences. Chronological halves split the included games by count; they
are sensitivity checks, not independent validation or season halves. Existing
adjusted-model omission refits and same-sample direction reversals inform stability.
The builder also performs full leave-episode-out causal refits; failed gates remain
visible and prevent a stable-refit claim. These checks do not establish sample
representativeness beyond the stated window or remove unmeasured confounding.

Practical-size thresholds are versioned product heuristics, not validated fantasy
scoring rules: PTS 2, REB/AST 1, STL/BLK 0.3, TOV/3PM 0.5, MIN 3, and +/- 3 in
native per-game units. Point-estimate size and statistical support remain separate.
A positive turnover difference is more turnovers, not an improvement. Unavailable
causality is never rendered as no effect; observed and causal samples and contrasts
remain separately labeled.

## Player context

Ask's deterministic performance overview adds minutes, attempts, shooting rates,
turnovers, and points/assists per 36 minutes to its five core production metrics.
The [semantic contract](semantic-contract.md) governs the formulas. Period
percentages use summed makes and attempts; missing inputs remain unavailable.
Two-player scorecards retain their five core comparison metrics.

### Context relations

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

### Interpretation and integration

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

### Validation

```bash
python -m pytest tests/test_player_context.py tests/test_context_data_repair.py -q
dbt parse --project-dir . --profiles-dir dbt/profiles --target dev
```

Fixtures execute the actual context SELECTs in SQLite to check grain, earlier-game
joins, phase isolation, missingness, and report conflicts. BigQuery execution is
a separate validation boundary. See [Validation](operations.md#validation) for warehouse
checks and [Evaluation](evaluation.md) for frozen local context builds.

### Roster-scoped offline studies

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

#### Exploratory association adjustment

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

#### Ask integration

In the analysis environment, export a reviewed study with
`scripts/export_teammate_study.py --help`, then set
`AGENT_TEAMMATE_STUDY_PATH` to the resulting local JSON and restart the app. The
bundle contains compact scope, statistics and limitations; no fitting or warehouse
writes occur during a request. Keep private bundles under ignored `reports/`.
Export verifies input hashes and recomputes the descriptive summary and adjusted
analysis before accepting the supplied outputs. Mixed or stale outputs are
rejected. Regenerate the study and analysis with the current code and analysis
environment before exporting older artifacts.
Panels retain the study's report-age limit for analysis and export. Exported
player names come from ID-matched frozen statistics, not the study title; missing
or conflicting names require rebuilding the evidence before publication.

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

## Ask overview metrics and samples

The server owns `semantic_evidence.presentation`, version
`ask_metric_presentation/5`, including selected metric keys, sample notes,
question focus and ordered takeaways. The browser renders those
facts without selecting the largest percentile as the headline.

### Visible metrics

| Focus | Primary metrics |
| --- | --- |
| General | PTS, REB, AST, 3PM, STL, BLK, TOV, MIN, FG%, FT%, TS% |
| Scoring | PTS, 3PM, FGA, 3PA, MIN, FG%, 3P%, FT%, TS% |
| Playmaking | AST, TOV, PTS, REB, MIN, TS% |
| Rebounding | REB, MIN, AST, TOV, STL, BLK |
| Defensive activity | STL, BLK, MIN, REB, AST, TOV |
| Contribution beyond scoring | AST, TOV, REB, STL, BLK, MIN |

General overviews show eleven metrics, with shooting percentages last. Focused broad performance/profile questions
show six or nine; remaining calculated metrics are expandable. The main table uses four compact
columns with inline %/minutes and percentage-point changes. Sample details remain expandable.
Key Takeaways use up to three visible icon/headline/explanation rows; limited-data
labels remain visible beside affected values. Explicit metric,
venue, opponent, ranking and unsupported grain requests retain their existing
routes and scope guards. A focused overview is a presentation of whole-game
observations, not permission to substitute them for unavailable tracking data.
Current overview scopes support season and calendar windows, plus the documented
last-ten-appearance default for recent performance. Other unsupported appearance
windows never become a calendar window silently.

Rebounding and defensive activity have separate takeaways and explicit coverage
limits. Supporting metrics are labeled as context, not measures of the focus.
No offensive/defensive rebound split, screening, foul, contest, chance, assignment
or passing-tracking statistics are inferred from the basic box score.

### Samples and comparisons

Every metric retains observed games, valid games, missing component counts and
its aggregate components. Makes/attempts accompany FG%, 3P% and FT%; FGA and FTA
accompany TS%, whose denominator uses the published 0.44 approximation. Ratios
pool components across valid appearances. Zero attempts produce an unavailable
percentage, while a recorded zero count remains zero.

Presentation states are `unavailable`, `limited` and `descriptive`. Partial
components or fewer than five valid appearances are limited; descriptive is not
an assertion of statistical precision. The five-appearance floor reuses the
project count-comparison policy. Changes are withheld if either period falls
below the floor or has partial components; original aggregate components remain
inspectable. Baseline coverage and zero-denominator guards still apply.
Overview percentile policy `ask_overview_percentiles/1` ranks PTS/REB/AST/STL/BLK,
3PM, TOV and MIN with the existing five-valid-appearance floor, complete metric
components and at least two qualified players. TOV reverses the direction: higher
percentiles mean fewer turnovers, with role/minutes context. MIN describes exposure
and uses a neutral bar. Other context rates remain unranked in this overview.

Shooting ranks pool unrounded aggregate components and use provisional project
minimums in the exact requested window: 100 FGA for FG%/TS%/eFG%, 50 FTA for FT%,
and 50 3PA for 3P%. These are not NBA official leader qualification, significance
or precision guarantees. Eligible peer cohorts are metric-specific, in the same
dates and phases, and exclude incomplete or insufficient samples. Ties share the
percentage of other qualified players strictly worse, divided by cohort size
minus one. Zero denominators never become zero-percent ranks.

Evidence retains `percentile_status`, label, cohort size, explanation and the
versioned qualification policy. Missing ranks show No data, Incomplete data,
Low sample, Low attempts, Few peers or Not ranked. Details disclose thresholds and
cohort sizes. Attempt eligibility does not change raw averages or existing
comparison suppression. Short windows may show percentages without percentile
ranks. Threshold coverage is inspected against frozen snapshots; no claim of
population-wide live-source validation is implied.

### Takeaways

Key Takeaways retain the icon, headline and short explanation structure. Broad
profiles share up to three selected findings with Overall: scoring opportunity
and efficiency, assists with ball security, then a material or notable non-scoring
observation when supported. No category quota forces ordinary values into prose.
Table values and sample methodology remain available for inspection. Generic
unavailable-capability lists are omitted; private backend tracking records gaps.
Focused scoring can still describe game-level variation. Thin samples retain
values without peer or baseline claims and include a short sample note.

Saved results retain their own server-authored takeaways, with at most three
visible rows. Missing canonical metadata receives the saved-answer notice.

Older saved results preserve their original metrics and receive a saved-answer
notice when presentation metadata is absent. Ask again to apply the new policy.


### Compact labels and changes

Table rows, sample rows and chart controls use metric abbreviations. Full names
remain in evidence/prose and accessible row labels. TS% has an on-demand tooltip
available by hover, keyboard focus or tap; Escape, a second tap or moving focus
closes it. It shows `100 × SUM(PTS) / [2 × (SUM(FGA) + 0.44 × SUM(FTA))]`, using
summed components from valid appearances rather than averaging game percentages.

Change colors use the visible signed delta: green for increases, red for decreases,
with TOV reversed. MIN, values rounding to zero and unavailable comparisons remain
neutral. Signs and units remain visible without relying on color. Colors describe
the metric direction, not an individual impact or defensive grade. Existing sample
and baseline rules continue to withhold unsupported comparisons.

### Performance over time

The server emits `scope.chart_grain`, chart `grain` and each basic metric's
`trend: {grain, points}`. Inclusive windows of at most 31 days use `daily` game-date
buckets; longer windows use `monthly` averages. Monthly results also retain the
legacy `monthly` points. Saved charts without grain metadata remain monthly.

Named months use an explicit or disclosed inferred year; their comparison is
the preceding full calendar month. A `week of/starting/beginning YYYY-MM-DD` is
seven inclusive days starting on that date. This/last week use Monday boundaries;
this month runs through the anchor date and compares with the preceding full
calendar month; last month is the preceding full month.
Past week/month are rolling windows; numeric rolling windows retain their rules.
Relative calendar windows honor a supplied as-of date. Partial
month/week phrasing and competing windows require clarification. Future ranges
remain invalid. No date request falls back to another season.

The UI title is **Performance over time**, with **Game-by-game** or **Monthly
average** below it. Points use only recorded game dates or months with appearances;
no off-day zeroes are inserted. Missing metric components still withhold the trend.
Date/sample tooltips preserve full dates, while axis labels are thinned to avoid
crowding. Phone overview charts fit their container and hide dense value labels;
each value remains available on hover or focus. The eleven-row, four-column table and collapsed context remain unchanged.


### Follow-up panel

The continuation composer uses a subtly tinted surface, orange left edge,
regular Barlow heading **Ask your next question**, and **Continue the conversation**
eyebrow. Player names and governed date/phase scope remain in the context line.
Suggested questions still fill the composer without submitting; Enter submits,
Shift+Enter inserts a newline, and scope changes/restored chats retain the existing
server/browser context behavior. The textarea starts at two rows/60px, with manual
vertical resizing available. This styling also applies to comparison follow-ups.


### Metric distributions

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

## Trending

The **Trending** page (`/what-changed`) supports basketball analysis and fantasy research through
league-wide Top Performers and Surging lists. It describes observed box scores;
it does not predict future performance or implement league-specific scoring.

### Comparison contract

- Default: each team's latest four games versus its previous four, anchored to
  the latest published game in the selected season phase. Missing appearances
  remain visible in availability counts, rather than extending the window.
- Alternative: latest complete Monday–Sunday week versus the preceding week,
  relative to that source date. A partial week is never compared with a full week.
- Regular season and playoffs are separate. An optional through date limits
  game dates. It is not a historical warehouse snapshot: later source corrections
  to earlier games can still appear.
- Player cards show exact dates. During the offseason, completed-season results
  remain dated historical comparisons. No games are synthesized.
- Team assignment uses the player's last observed team. Teams inactive for more
  than six days relative to the comparison anchor are excluded. Recorded game
  logs cannot prove roster membership on days without a player appearance.

### Ranking contract

The existing project production proxy is `PTS + REB + AST + STL + BLK - TOV`
per recorded appearance. Top Performers sorts recent production; Surging sorts
positive recent-minus-prior changes. Ties resolve by player ID.

Four-game rankings require a complete four-team-game window, at least three
appearances, and at least ten minutes per appearance. Surging also needs a
complete prior window with three appearances and excludes team changes across
the comparison. Weekly rankings require two recent appearances; Surging also
requires two prior appearances. Players without sufficient baselines can rank
as top performers but not as surging players.

Availability expands into minutes, personal fouls, appearances, missing
appearances, and dated injury-report coverage. Offense expands into points,
assists, threes, offensive rebounds, turnovers, FG%, FT%, and TS%. Defense
expands into defensive rebounds, steals, and blocks. Counting stats include
per-appearance values and totals, allowing fantasy researchers to inspect volume.

Percentages use total makes and attempts, not averages of game percentages;
changes are percentage points. TS% is `100 * PTS / (2 * (FGA + 0.44 * FTA))`,
following the [NBA glossary](https://www.nba.com/stats/help/glossary).
Zero attempts and missing fields remain unknown. Missing appearances never
become zero-stat games in per-appearance averages.

### Evidence and limitations

Every card includes underlying games. Injury evidence uses dated reports,
matched to player, team, and game date; reports dated after the game or requested
cutoff are excluded. An Out report is a reported status, not a confirmed absence.
Missing reports cannot establish health. Current ingestion omits non-playing
roster entries, so DNP-CD is explicitly unavailable and is never inferred.

Defense measures box-score events, not total defensive impact. Four games are a
small descriptive sample. The production proxy is not ESPN/Yahoo scoring, an
all-in-one basketball rating, or a personalized fantasy recommendation.

### Data rollout and serving

The API reads a bounded 60-day slice of `fct_player_game_stats` (maximum 20,000
rows). Exceeding the cap or failing the core query returns 503 instead of a
partial league ranking. Missing historical injury data remains optional.

The dbt changes forward raw PF, OREB, and DREB through the silver models into
the gold fact table. Existing deployed tables remain readable, with these new
fields shown as unknown until the normal core dbt build publishes them. Raw
columns absent from a source schema also remain null.

`what_changed_injury_reports` publishes through the existing injury candidate
branch and atomic promotion transaction. Run the normal pipeline to build and
validate these assets; this feature's local validation does not deploy or rebuild
the live warehouse. Historical injury coverage depends on reports previously
collected and does not imply a full-season backfill.

The API caches successful comparisons for 15 minutes, keyed by repository,
period, phase, and cutoff. Browser QA uses explicitly fictional fixture players;
it verifies interaction and layout, not live warehouse completeness.

### Sports Journal presentation

The shared UI uses locally hosted Barlow and Barlow Condensed, the original
charcoal/orange palette, square controls, and underlined navigation. The opportunity
map plots minutes change against production change per appearance. Axis ranges
adapt to the loaded data and include zero; positive and negative changes remain
visible. Only the selected point is labeled to avoid a wall of overlapping names.

A player requires a sufficient prior window and no team change to appear on the
map. Top performers without a comparable baseline remain in the ranked list.
The list provides individual selection for overlapping points and the map supports
keyboard selection. Player details retain all metric clusters, totals, injury
coverage, and individual game evidence.

## Statistical Profile Similarity

The public implementation provides reproducible similarity and archetype
comparisons through `nba_gold.player_similarity_features` and
`nba_gold.player_archetypes`. It is a reference baseline; personal tuning,
labels, experiments, and model artifacts belong outside the public repository.

### Inputs and training

`player_similarity_feature_input` has one row per season and player. Its
features cover production, shooting, recent form, team contribution, and available
physical/career attributes. Missing optional attributes remain visible in diagnostics.
Players marked `insufficient_sample` are excluded from training and publication.

`player_similarity_model.train_player_similarity_model` trains the public KMeans,
Gaussian mixture, hierarchical, and HDBSCAN comparison set on shared normalized
features. The Airflow wrapper is `nba_pipeline.build_player_similarity_outputs`.
Outputs include normalized vectors, descriptive archetypes, comparison diagnostics,
and a deterministic three-dimensional PCA projection.

The application uses equal-weight serving-time similarity. PCA coordinates are
an approximate visualization; nearest-neighbor relationships use Euclidean distance between unit-normalized
feature vectors and display `1 / (1 + distance)`. For nonzero vectors this ranks
neighbors in the same order as cosine similarity; the score is not raw cosine. A visually distant point can therefore be a close
feature-space neighbor. Axis metadata describes the projection's feature drivers.
Archetypes are descriptive categories, not authoritative scouting grades or
measures of player value.

### Publication and serving

Validated feature and archetype candidates are published together through the
pipeline's atomic publication path. Failures preserve previously served data.
The `/similarity-map` page and its JSON endpoints read the published outputs;
switching candidate-model groupings retains the same player coordinates.

The repository includes metadata table-creation support for a similarity
lifecycle. An end-to-end versioned feature store, registry promotion workflow,
and drift-monitoring system should not be inferred from those schemas alone.
See [Architecture](architecture.md) for implemented publication behavior and
[Public/private boundary](public-private-boundary.md) for ownership boundaries.

### Run and validate

```bash
python -m pytest tests/test_player_similarity_model.py tests/test_incremental_pipeline.py -q
dbt parse --project-dir . --profiles-dir dbt/profiles --target dev
python scripts/backfill_similarity_projection.py          # read-only dry run
python scripts/backfill_similarity_projection.py --write  # publishes to BigQuery
```

The backfill commands require configured warehouse access. With the appropriate
warehouse target, validate the feature dependencies using:

```bash
dbt build --project-dir . --profiles-dir dbt/profiles --target dev \
  --select fct_player_game_stats fct_player_scoring_contribution \
    player_recent_form player_category_profile player_similarity_feature_input
```

Local test success does not establish live feature coverage or successful
warehouse publication. Keep detailed model runs and diagnostics in local reports.

### User-facing methodology and recent comparisons

The public `/similarity-methodology` page documents all 48 configured fields,
standardization, per-feature equal weights, imputation limitations, and the
selection score (55% normalized silhouette, 25% balance, 20% coverage). These are
internal clustering diagnostics, not validated basketball roles or player value.

Ask also supports a separate recent-appearance comparison using PTS/REB/AST by
default; it does not retrain the published model. See [Ask workspace](ask-workspace.md#governed-similarity-and-league-references)
for its window, qualification and score contract.
