# Players and research breakdowns

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

## Breakdown contract

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

## Studies and identification

The fixed focal/exposure pairs are LeBron James/Luka Doncic, Jalen Johnson/Trae
Young, and Jalen Brunson/Josh Hart. Each study shows all nine core outcomes and
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

## Configuration and pregame capture

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

## Validation

`tests/test_research*.py`, `tests/test_causal_estimation.py` and the JavaScript suite
cover scope, numerical parity through JSON/SSE endpoints, missingness, temporal
isolation, immutable concurrent writes, and synthetic causal recovery/refusal.
Use the [validation guide](validation.md) for the full application and pipeline
checks. Fixture/API/browser success does not establish live warehouse coverage,
prospective capture reliability, or real-world causal identification.

Plus-minus is team points minus opponent points during the focal player’s court
time. It is a signed outcome in every study, including adjusted and gated causal
estimation. Negative values are valid; missing values remain unavailable. It is
not possession-adjusted net rating or an isolated measure of individual impact.
Catalog version `multi_metric_studies/v3` requires plus-minus and the expanded
27-hypothesis family; rebuild older catalogs rather than reusing their correction.

## Ask takeaways and uncertainty

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
