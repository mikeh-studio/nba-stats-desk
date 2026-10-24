# Intelligence contract acceptance criteria — review draft

All items below are **pending implementation**. The current draft supplies only
[types](../app/agent/intelligence_contract.py) and a
[contract](intelligence-contract.md); syntax/lint checks are not integration proof.
Use synthetic source-roster names and complete/incomplete evidence fixtures.

## Shared provider conformance

| ID | Required evidence |
| --- | --- |
| C01 | Unset/`reference` selects reference without loading installed providers; installing a wheel alone changes nothing. |
| C02 | An explicitly selected compatible provider loads exactly once. Missing/duplicate names, bad factory/descriptor, unknown routes, incompatible API/semantic versions and `requires_model` without a configured model fail startup without fallback. |
| C03 | Factory, prompt lookup and classification perform no I/O; instance reuse across concurrent requests leaks no state. Plans honor cancellation/deadlines and actual gateway retries consume the same host budget. Gateway timeout/budget/unavailable exceptions reach the host, which assigns `provider_timeout`/`budget_exhausted`; a provider returning either code is invalid output. |
| C04 | The same bounded context, selected season and source roster reach both implementations; the roster covers the selected season plus seasons parsed from the question (a player only in a requested earlier season resolves). Prior scope cannot override a new explicit request; ambiguous name replies preserve public clarification behavior. |
| C05 | Unknown fields/routes/metrics, routes known but ineligible for this request or configuration, SQL/formulas/tool names, model IDs, invalid dates/windows, mismatched plan kind, incorrect query counts and two-player `compare` plans are rejected before any execution. Test hostile dictionaries and malformed typed instances. |
| C11 | Host precedence wins over a hostile provider: refused scope (for example an unavailable playoff round), settings-disabled routes and pending clarifications/follow-ups dispatch publicly without calling `classify`, even when the provider would select an allowed route. Handler routes dispatch without `plan`. |
| C12 | Host plan normalizations run on every validated `QueryPlan`, reference included; `aggregation_resolution` is host-derived and provider fields cannot set it. Answer generation uses its own host budget and accounting, never `PlanningBudget`. |
| C06 | Forged `aggregation_source`/leader qualification cannot bypass public policy. Explicit aggregation, phase, filters, denominator thresholds and comparative scope remain intact. |
| C07 | Existing evidence tests retain source coverage, null/zero distinction, ratio-of-sums, percentage-point differences, cohort qualification and claim bindings. Provider output alone never supplies statistical evidence. |
| C08 | Clarification/unsupported results execute nothing. Model/timeout/budget/output failures use sanitized error envelopes with no partial answer, second provider or hidden model fallback. |
| C09 | Prompt revision and host-measured usage are traceable; prompts, raw model replies and secrets are absent from public responses/logs/errors. Provider prose cannot add unsupported claims. |
| C10 | The same request through JSON and SSE has equivalent final status, scope, statistics, evidence and clarification; transient progress frames may differ. |

## Minimum deterministic reference scope

Use a fixture with players named Alex Example and Blair Sample, explicit 2024–25
coverage, and an otherwise identical fixture with incomplete coverage. Full names
are unique; the identity-ambiguity case adds Casey Example so the surname matches
two players. Every other case runs without Casey Example.
These are acceptance examples, not permission for substring-only recognition.

| Request | Required result with complete evidence |
| --- | --- |
| “Show Alex Example's overview for the 2024-25 regular season.” | Existing governed overview, correct player/season/phase and public presentation; not an invented one-metric substitute. |
| “Compare Alex Example and Blair Sample for the 2024-25 regular season.” | Existing two-player scorecard/evidence contract with matching scope. |
| “Compare Alex Example's points per game in the last 5 games versus the prior 5 games in the 2024-25 regular season.” | Two ordered disjoint windows, same player/metric/aggregation, correct public comparison. |
| “Top 5 players by points per game in the 2024-25 regular season, minimum 10 games.” | Governed ranking with explicit minimum, deterministic ties and disclosed cohort. |
| “Top 5 players by field goal percentage in the 2024-25 regular season.” | Clarification for attempt threshold; no guessed qualification. |
| “Show Example's overview for the 2024-25 regular season.” with Casey Example added | Identity clarification offering both matches, no guessed match or statistics. |
| “Show Alex Example's overview for the 2024-25 regular season, only fourth quarters.” | Unsupported scope; never ignore the modifier and show a season overview. |
| A successful overview followed by “What about the playoffs?” | Public follow-up resolution preserves player/season and changes phase, or clarifies if required evidence is absent. |

For every supported example: assert **zero model calls**, no model-key requirement,
and the normal evidence-bound result. With incomplete/missing evidence: apply the
existing coverage behavior without fabricated zeros or claims. Reference may
clarify/decline other language; its supported grammar must be documented. Do not
require broad parity with production heuristics, only these cases plus conformance.

## Installation and ownership proof

- [ ] Public-only environment contains no private package/checkouts/import paths;
      representative HTTP JSON/SSE and browser flows pass without model credentials.
- [ ] A static import-graph check, including function-level imports, finds no
      public module importing `question_intent`, `semantic_planner`, `planner` or
      `router` other than the one guarded optional legacy loader. Moved scope
      parsers keep their existing tests in public.
- [ ] In the public-only environment a repository without a governed adapter gets
      the sanitized `legacy_repository` unavailable response, never a governed-query
      fallback or import error; with the legacy tree present, existing legacy tests
      still pass.
- [ ] Build and install a private wheel in a separate environment with the exact
      public candidate. Select its registered name; prove imports originate from
      the installed wheel and public application, never the old private `app` tree.
- [ ] Exercise the private provider through actual app endpoints with a stubbed
      model transport and synthetic evidence. Label this fixture integration;
      live model/warehouse testing is separate and not required by this draft.
- [ ] Assert one public owner for execution helpers; migrate direct production
      imports and split mixed tests without deleting their behavioral coverage.
- [ ] Update ownership-map/checker coverage for new package paths. Do not widen
      frozen UI inventories or change the existing dependency pin to pass tests.
- [ ] Installed provider does not activate deployment, flags, schedulers or new
      data reads. Reference/provider configuration and model credentials remain
      separate from source availability and application-disable controls.

## Required validation at implementation completion

Public: `python -m pytest -q`, `node --test tests_js/*.test.js`,
`npx playwright test`, `python -m ruff check .`,
`python -m ruff format --check .`, policy/boundary checks and relevant type checks.
Private: `make check`, `make test-product`, `make test-browser`, `make lint`, and
the shared provider-conformance/installation tests. Run evaluation fixtures when
planner behavior changes. Record counts, skips and exact candidate commits.

Use the task's designated `.venv-app/bin/python`; pass it as `PYTHON` to private
make and put its bin directory first on PATH for browser tests. Do not load `.env`
files for validation. Keep generated wheels/reports outside tracked source.

Contract approval permits subsequent implementation only. Public publication
requires separate exact-diff approval; passing tests never grants that approval.
