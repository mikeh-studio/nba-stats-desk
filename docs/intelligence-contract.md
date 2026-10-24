# Intelligence provider contract — review draft

Status: proposed `nba_intelligence/1`; approval required before implementation.
The [Python declarations](../app/agent/intelligence_contract.py) are inert and
unused by the app. This draft adds no loader, settings, planner, execution changes,
package installation, or deployment. [Acceptance criteria](intelligence-acceptance.md)
describe future proof, not results already achieved.

## Boundary

The public app owns discovery, request/context construction, conversation state,
identity resolution, route dispatch, scope validation, metric execution, evidence,
claim validation and deterministic presentation. A provider supplies production
prompt text, intent classification and proposed routes/plans. The built-in reference
provider supplies deterministic planning and no production prompts.

Providers return data, never SQL, formulas, callable names, executable code,
warehouse handles, tool lists, raw statistics or claimed evidence. Only the host
can authorize execution. These are interface constraints for trusted installed
Python code, not a sandbox; installing a provider grants normal process privileges.
No provider is loaded from a request, URL, arbitrary import string or user text.

## Discovery, compatibility and lifetime

Proposed setting: `ASK_INTELLIGENCE_PROVIDER`, default `reference`. That reserved
name selects the built-in provider and must not import external providers, even
if installed. An operator may explicitly select a name in the fixed entry-point
group `nba_stats_desk.intelligence.v1`. Resolve exactly one installed entry point
with that name and call its zero-argument `ProviderFactory` once at startup.
An installed package is inert until selected. No automatic provider preference.

Missing/duplicate names, invalid descriptors, factory errors, incompatible API or
semantic-contract versions fail startup with a sanitized configuration error;
never silently fall back to reference. Require an exact API-version match and
explicit support for the host's current semantic-contract version. Descriptor
route IDs must exist in the host registry. A descriptor with `requires_model` also
fails startup when the host has no configured planning model or credentials; a
later transport outage is a per-request failure. New required fields or meanings require
a versioned contract change and shared tests; do not infer compatibility from a
package version. Record selected provider/version in internal diagnostics.

The instance must be immutable/reentrant across requests; no conversation, model
client or mutable request state is stored on it. Factories, `prompts` and `classify`
perform no I/O. `plan` is asynchronous and cooperates with cancellation. The host
owns deadline enforcement and model transport; a trusted provider that blocks the
event loop violates the contract (timeouts are not hard process isolation).

## Request and result flow

1. Host precedence runs first and never consults the provider. Public code applies
   input limits, then resolves pending clarifications and follow-ups with the
   existing conversation rules, which may read prior question text. A resolved
   follow-up or clarification reply dispatches directly. Public route predicates
   then evaluate host precedence routes in their existing order: scope refusals,
   routes gated by settings or conversation state (appearances, availability,
   research, teammate study, reference) and `legacy_repository`. A match dispatches
   without classification. Only the remaining routes eligible for this request,
   this configuration and the selected provider enter `PlanningContext.routes`.
2. Public code prepares the identity catalog, supported seasons/metrics and context.
   The catalog is the source roster for the selected season plus every season the
   host parses from the question (existing `requested_seasons`), each name once.
   `PlanningContext` contains the current question (1–2,000 characters), selected
   season, ISO calendar date and one host-selected prior successful scope. Prior
   text/answers, evidence rows, secrets, arbitrary settings and repository objects
   do not cross this boundary. Catalog bounds must match existing resolver limits;
   never silently truncate an identity catalog and select a different player.
   Current explicit scope wins.
3. `classify(context)` returns an advisory `IntentSelection`, a `Withheld` result,
   or a `ProviderFailure`. It is deterministic and model-free in v1; model-based
   intent classification needs a versioned contract change. The selected route
   must be in `context.routes` and the descriptor; a known but ineligible route
   is invalid provider output. Continuation is advisory; public conversation rules
   decide whether prior scope may be inherited. A selected `handler` route
   dispatches after the host rechecks eligibility, without calling `plan`.
4. For a `queries` route, the host obtains one `PromptBundle` per request and calls
   `plan` with that bundle, a host-owned budget, and an optional model gateway.
   The gateway uses a fixed host-owned plan JSON schema, configured model and
   bounded text; a provider cannot choose endpoints, credentials, tools or budgets.
   Each transport attempt consumes host budget, including retries. Transport usage
   and actual calls are measured by the host, not accepted from provider output.
   The gateway signals failure only by raising `GatewayTimeout`,
   `GatewayBudgetExhausted` or `GatewayUnavailable`; providers must let these
   propagate and the host maps them to failure codes.
5. The host validates the complete result before dispatch. Unknown fields and
   malformed nested data fail closed; frozen Python records/type hints alone do
   not validate values. Recheck objects returned in-process as well as parsed JSON.
   A `QueryPlan` has one query for `query`, exactly two ordered queries for
   `compare`, and a route declared as `queries`. In v1 `compare` is a same-player
   comparison: both queries carry the same player mention, metric and aggregation
   and differ only in scope (window, dates or phase); the first is the current
   side and the second the baseline. Two-player comparisons are the
   `player_comparison` handler route. `plan` may return a `HandlerPlan` naming
   another eligible `handler` route; it carries only that route ID. Public
   handlers receive the original host context and re-resolve scope with public
   parsers; no untyped plugin argument bag.
6. The host then applies the public plan normalizations
   (`normalize_duplicate_rank_plan`, `single_period_rank_plan`) to every
   validated `QueryPlan`, reference plans included. It derives
   `aggregation_resolution` itself through existing public aggregation resolution;
   provider fields never populate it. Public code resolves names and validates
   every scope/query against the existing [semantic contract](semantic-contract.md)
   before execution. Unsupported or ambiguous requests produce no partial
   statistical answer. Public code renders results using the existing evidence
   and claim-binding contract for JSON/SSE.

`Scope` describes query scope, not proof of source coverage. `player_name` is an
untrusted mention; no model-supplied IDs are accepted. Metric keys and operations
must be in the request catalog; phase/window combinations, calendar dates,
qualification, limits (1–100), comparisons and source availability remain publicly
validated. `aggregation_source` is diagnostic, not authority; `unspecified` invokes
existing public aggregation resolution or clarification. It never means guess.
`league_leader` qualification invokes the existing public appearance-floor policy
only after public validation that the request qualifies; it cannot relax a floor.

No generic custom formulas, multi-query batches, new cross-season pooling, or
arbitrary handler payloads are introduced by v1. Existing special routes remain
public handlers with their current contracts and restrictions. A route that needs
additional private planning data must receive a separately reviewed typed extension
before migration; do not smuggle it through question text or a generic dictionary.

## Prompts, failures and observability

`PromptBundle` contains versioned inert system text for planning and optional
model-generated answers. Host-authored immutable safety/evidence instructions and
schemas take precedence over provider text. The host encodes questions/context as
untrusted data; answer generation receives only validated evidence. Reference mode
returns no prompts and uses deterministic public rendering. Answer generation is a
host model call with its own host-owned budget, deadline and usage accounting,
separate from `PlanningBudget` and never exposed to the provider; exhausting it
yields the existing deterministic rendering or sanitized error, never unbound prose.
Raw prompt text and model replies must not appear in responses, normal logs,
public fixtures or errors.

`Withheld` carries a closed reason code, no statistics and no arbitrary message;
the host maps it to existing clarification/unsupported response shapes and safe
wording. Host identity ambiguity options remain host-generated. A timeout,
transport error, malformed output or exhausted budget is a failure, not an
unsupported question. A provider may return only `provider_unavailable` or
`invalid_provider_output`. The host alone assigns `provider_timeout` (its deadline
or a `GatewayTimeout`) and `budget_exhausted` (its accounting or a
`GatewayBudgetExhausted`), and treats a provider-returned host-only code as
invalid output. Sanitize uncaught exceptions into the existing application
error envelope. Preserve existing JSON/SSE error conventions and cancellation;
do not start a second provider, silently use an LLM fallback, execute a partial
plan or fabricate evidence. No automatic application retry after budget exhaustion.

Record provider/API/version, route, outcome/reason, latency and host-measured model
usage in existing diagnostics. Prompt revision may be recorded; text may not.
Keep user/session identifiers and private provider details out of newly exposed
public response fields. Numeric results and provenance remain host-owned.

## Reference behavior and migration order

Reference mode must answer the bounded overview/comparison/ranking examples in the
acceptance matrix with zero model calls and no model key. Source credentials and
coverage are separate: standalone does not promise live warehouse data without
access. Explicit application-disable controls still apply. The current early
credential checks in `StatsAgent` must become provider-aware during implementation.

After approval, extract `execute_plan`, `leader_game_floor`,
`normalize_duplicate_rank_plan`, `single_period_rank_plan` and the host plan
schema (`plan_schema`, `response_schema`) into public execution code. Keep
calculations and scope policy unchanged, and prove function identity across
provider integration.

Literal scope extraction and scope-refusal detection are public scope validation,
not planning intelligence. Before provider integration, move the private symbols
that public handlers and routes call into public scope-policy code with their tests:

| Symbol (current private module) | Public consumers |
| --- | --- |
| `split_kind`, `question_intent` (`question_intent`) | `player_splits`, `routes`, `semantic_answer`, `research_ask` |
| `venue_mentions`, `round_scope_message`, `has_specific_metric` (`question_intent`) | `overview_calendar`, `performance_overview`, `player_comparison`, `service` |
| `explicit_aggregation`, `task_terms` (`question_intent`) | `semantic_answer`, `semantic_intent` |
| `name_reply`, `name_tokens`, `names_other_player`, `starts_new_question` (`question_intent`) | `name_matching`, `routes` |
| `explicit_scope` (`semantic_planner`) | `research_ask`, `semantic_intent`, `serving_warehouse` |
| `explicit_metric_mentions` (`planner`) | `research_ask`, `semantic_answer`, `semantic_intent`, `serving_warehouse` |
| `AgentRoute` labels (`router`) | `route_labels` |

`semantic_answer` calls private `plan_question`; that call site becomes
`provider.plan`. Planner prompts, model-backed planning and production planning
heuristics stay private. A symbol that turns out to need private planning data
gets a separately reviewed typed extension instead. The ownership map and its JSON
rules change in the same implementation diff. After the move no public module
imports `question_intent`, `semantic_planner`, `planner` or `router`, apart from
one guarded optional legacy loader (below). Separate mixed execution/reference
tests from private intelligence cases; retain all test intent.

`legacy_repository` is a host precedence route and never goes through the
provider. It depends on private `build_agent_plan` and the production `planner`
query plans (`build_query_plan`, `deterministic_query_plan`,
`detect_opponent_breakdown`), which `service` reaches only through one guarded
optional legacy loader. In v1, when that private code is absent, the route returns
the existing sanitized unavailable response for repositories without a governed
adapter, never a governed-query fallback. Production keeps legacy behavior through
the legacy compatibility tree until a separately reviewed migration.

Build the private provider in its own namespace, not an installed top-level `app`
package. Test a built wheel against this public candidate in isolated environments;
record exact source commits and import origins. During transition, retain the
existing pinned dependency and legacy compatibility tree for their own checks.
The new integration environment runs the public app plus provider, not that legacy
tree via `PYTHONPATH`. Extending package paths in ownership checks is required.
Released dependency pinning and removal of the legacy tree are later work.

## Review decision

Approve or amend: explicit named entry-point selection; exact version matching;
the three-method provider interface; host-owned execution/model budgets; safe
withholding/error behavior; and the reference acceptance scope. This revision
also needs explicit decisions on: host precedence and follow-up resolution ahead
of `classify`; moving literal scope parsers public; same-player-only `compare`;
standalone `legacy_repository` behavior; and host-only failure codes. Approval of this
design is separate from approval to publish any public diff. Implementation and
publication remain pending after this draft.
