# Research workbench

`/research`, player pages, comparison pages, and research requests in Ask share
`app/research.py`. Ask plans an allowlisted scope; Python calculates and renders
results. It does not accept generated SQL or model-calculated statistics.

## Breakdown contract

One season, one or two player IDs, regular season/playoffs/both, inclusive dates,
opponent, venue, rest, and an optional reviewed teammate-status filter. Counts use
per-appearance averages or totals; shooting percentages use summed makes and
attempts. Valid/observed samples, missing filter context, underlying games, source
identity and query hash remain inspectable. Missing values are never zero-filled.
The workbench includes PTS, REB, AST, STL, BLK, TOV, 3PM, MIN, shooting volume and
percentages, TS%, eFG%, and descriptive points/assists per 36 minutes.

Rest falls back to the observed team-game ledger, which may be incomplete. Date
filters describe corrected historical statistics; they do not assert what was
known at that historical time. Unsupported scope is rejected rather than silently
approximated. URL filters can be shared. Existing headline cards retain their own
scope; the shared research panel is the consistent detailed view.

## Studies and identification

The fixed focal/exposure pairs are LeBron James/Luka Doncic, Jalen Johnson/Trae
Young, and Jalen Brunson/Josh Hart. Each study shows all eight core outcomes and
eight shooting context measures. Shooting ratios are descriptive. Positive
contrasts mean more of a statistic, including turnovers.

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
Holm p-values cover the predeclared 24 core hypotheses, including unavailable
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
