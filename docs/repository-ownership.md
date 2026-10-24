# Repository ownership map

The public repository is `mikeh-studio/nba-stats-desk`; the private product is
`mikeh-studio/nba-stats-desk-product`. Paths below are relative to their owner.
This map covers development ownership, not deployment or data permissions.

| Paths / capability | Authoritative home | Treatment |
| --- | --- | --- |
| `dags/`, `contracts/`, `dbt/`, `dbt_project.yml`, `season.yml`, ingestion/repair scripts | Public platform | One shared pipeline; no private copies or second schedule. |
| `app/agent/semantics.py`, `semantic_contract.yml`, `semantic_source.py`, `semantic_serving.py`, `serving_*.py`, `formulas.py` | Public platform | Shared arithmetic, qualification, evidence and publication validation. |
| `app/repository/`, `app/seasons.py`, `app/availability.py`, `app/freshness.py`, `app/research*.py`, `app/what_changed.py` | Public platform | Shared read adapters and deterministic analyses. |
| `app/agent/name_matching.py`, `player_resolver.py`, alias/catalog files, claim/evidence validation, execution/capacity/provider/security helpers | Public platform | Reuse pinned implementation and its contracts. |
| `app/agent/performance_overview.py`, `player_comparison.py`, `tools.py`, other unlisted existing `app/` modules | Public platform initially | Mixed calculation and presentation remain shared; extract product concerns deliberately when needed. |
| `app/main.py`, `app/config.py`, `app/model_catalog.py` | Private product | Product app composition, settings and model selection. Public copies remain reference implementations. |
| `app/static/**`, `app/templates/**` | Private product | All product pages, styles, interaction code and bundled assets. Public copies remain reference assets. |
| `app/agent/planner.py`, `semantic_planner.py`, `semantic_answer.py`, `service.py`, `router.py`, `question_intent.py` | Private product | Product prompts, planning, routing and answer orchestration. Shared engine functions are imported, not copied. |
| `app/agent/followup.py`, `conversation.py`, `routes.py`, `route_labels.py`, `metric_presentation.py` | Private product | Conversation workflows and response presentation. |
| `semantic_planner.execute_plan`, `leader_game_floor`, `normalize_duplicate_rank_plan`, `single_period_rank_plan` | Public platform | Private planner delegates to the identical pinned functions for arithmetic and scope validation. |
| Reusable semantic/source fixtures and evaluation harness | Public platform | Remain shared and credential-free. |
| Product regressions, browser/JS tests, synthetic feature cases | Private product | Run against private modules/assets; reuse pinned synthetic shared fixtures where needed. |
| Tuned similarity modeling | Existing private `nba-personal-model` | Single model owner; not copied during this migration. |
| Prop-market research | Existing private `nba-edge-desk` | Separate research owner; not migrated or activated. |
| Actual product deployment config, unreleased designs, experiment definitions | Private product/research | Track source only; deployment remains separately reviewed. |
| Credentials, feedback, traces, raw data, exports, model binaries | Outside Git | Access-controlled storage, even for private development. |

## Migration and compatibility

The initial private product is seeded only from tracked files at public PR #97's
commit `735bf4f0552209ccbc26e164692dbcd4aa1944fd`. The private gitlink pins that
same revision. This is an unmerged development baseline, not a released platform
version. PR #97 remains open and unmerged; no additional product implementation
is pushed to its public branch as part of this migration.

The private repository records every imported product/test path and its original
content digest in `docs/migration-manifest.json`, preserving upstream MIT and
third-party notices. No ignored files, local experiments, credentials or datasets
are imported. Unrelated local work remains untouched.

Private `app` packages explicitly fall back to the pinned public package for
unowned modules. A separate `shared_platform` import accesses the shared engine
functions from the split planner. Product tests verify import origins and function
identity, then exercise JSON/SSE and populated browser flows. The private app keeps
the existing routes and response contracts. This is an interim composition boundary;
a future packaged core can replace it without moving statistical ownership.

Existing public files are retained to preserve a standalone runnable reference.
They are not a second owner for product features and are not kept in automatic
sync. Shared security/correctness fixes still need compatibility checks in both
consumers. Product requests remain bounded and read-only; no endpoint, scheduler,
warehouse identity, serving flag, or deployment is activated by this move.

## Routing new work

Start product changes in the private checkout, outside its `shared/` submodule.
Start public platform fixes on a fresh public branch with synthetic reproduction.
When ownership is unclear, default to private design work and resolve the shared
interface before publishing code. Public promotion is a deliberate reviewed PR,
never a mirror or automatic export of the private tree.
