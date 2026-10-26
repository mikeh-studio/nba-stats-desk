# Intelligence provider contract

Status: `nba_intelligence/2` review revision of the contract approved on 2026-10-24. The local
candidate implements the public host and reference provider. Publication remains
subject to exact-diff review. See [runtime behavior](intelligence-runtime.md) and
[the ownership audit](intelligence-ownership-audit.md); acceptance evidence is
recorded separately from this normative contract.

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

Setting: `ASK_INTELLIGENCE_PROVIDER`, default `reference`. That reserved
name selects the built-in provider and must not import external providers, even
if installed. An operator may explicitly select a name in the fixed entry-point
group `nba_stats_desk.intelligence.v2`. Resolve exactly one installed entry point
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
   existing conversation rules, which may read prior question text. Direct dispatch
   is allowed only when the host has fully resolved the request for an eligible
   public handler, or has a complete public plan that passes the same validation
   pipeline below. Resolving conversation state alone does not finish planning:
   a reply such as “What about rebounds instead?” may retain the player and scope
   but still require a new plan. Such requests continue through classification
   and planning with the current question and host-authorized prior context.
   Public route predicates
   then evaluate host precedence routes in their existing order: scope refusals,
   routes gated by settings or conversation state (appearances, availability,
   research, teammate study, reference) and `legacy_repository`. A match dispatches
   without classification. Only the remaining routes eligible for this request,
   this configuration and the selected provider enter `PlanningContext.routes`.
2. Public code prepares the identity catalog, supported seasons/metrics and context.
   The catalog is the source roster for the selected season plus every season the
   host parses from the question (existing `requested_seasons`), plus prior seasons
   the host explicitly authorizes for this continuation, each name once.
   `PlanningContext` contains the current question (1–2,000 characters), selected
   season, ISO calendar date and one host-selected prior successful scope. Prior
   text/answers, evidence rows, secrets, arbitrary settings and repository objects
   do not cross this boundary. Catalog bounds must match existing resolver limits;
   never silently truncate an identity catalog and select a different player.
   Current explicit scope wins.
3. `classify(context)` returns an advisory `IntentSelection`, a `Withheld` result,
   or a `ProviderFailure`. It is deterministic and model-free in v2; model-based
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
5. The host validates result **structure first**: allowed result variant, fields,
   nested types, enum values, eligible route/plan kind and query count. Unknown
   fields or malformed objects fail closed, including in-process typed instances.
   A raw `QueryPlan` has one query for `query` or two for `compare`. This stage
   does not impose final comparison identity/scope semantics: a structurally valid
   duplicate or accidental cross-season ranking pair can reach normalization.
   It does not authorize execution. `HandlerPlan` names only an eligible handler;
   public handlers re-resolve scope from the original host context.
6. Apply only the existing, narrowly specified public normalizations
   (`normalize_duplicate_rank_plan`, `single_period_rank_plan`) to structurally
   valid plans, reference plans included. Collapse identical ranking queries or an
   accidental second season only when the existing predicates prove it is safe.
   Never discard an explicitly requested season or repair arbitrary invalid scope.
   Record which normalization ran and recheck the resulting structure.
7. Validate **final semantics** before execution. The host derives
   `aggregation_resolution`, resolves identity and validates metric, aggregation,
   dates, phase, filters, qualification and evidence against the existing
   [semantic contract](semantic-contract.md). Provider fields never populate
   authoritative aggregation resolution. A remaining `compare` has two queries
   for the same publicly resolved player identity, canonical metric and resolved
   aggregation, with current side first and baseline second. Different name
   spellings may resolve to that same identity under existing public rules;
   ambiguous mentions require clarification. Matching strings alone do not prove
   identity. Two-player comparisons use the `player_comparison` public handler.
   Supported same-player season-to-season comparisons remain valid: each query
   retains its own explicit season and coverage, alongside supported window/date/
   phase differences. This permits comparison of separately validated scopes,
   not cross-season pooling or new operations. Final league-ranking comparison
   pairs that were not safely normalized are not same-player comparisons and
   cannot execute through this plan kind. Unsupported or ambiguous requests
   produce no partial statistical answer. Public code then executes and renders
   through the existing evidence/claim-binding contract for JSON/SSE.

The mandatory order is **structural validation → narrow normalization → final
semantic/evidence validation → execution**. Neither normalization nor resolving a
follow-up bypasses final validation.

`Scope` describes query scope, not proof of source coverage. `player_name` is an
untrusted mention; no model-supplied IDs are accepted. Metric keys and operations
must be in the request catalog; phase/window combinations, calendar dates,
qualification, limits (1–100), comparisons and source availability remain publicly
validated. `aggregation_source` is diagnostic, not authority; `unspecified` invokes
existing public aggregation resolution or clarification. It never means guess.
`league_leader` qualification invokes the existing public appearance-floor policy
only after public validation that the request qualifies; it cannot relax a floor.

No generic custom formulas, multi-query batches, new cross-season pooling, or
arbitrary handler payloads are introduced by v2. Existing special routes remain
public handlers with their current contracts and restrictions. A route that needs
additional private planning data must receive a separately reviewed typed extension
before migration; do not smuggle it through question text or a generic dictionary.

## Prompts, failures and observability

`PromptBundle` contains a revision and inert planning-system text only. Public
rendering remains deterministic; v2 exposes no answer-generation prompt or call.
Host safety instructions and schema take precedence over provider text. Questions
and prior context are data, never instructions. Reference mode returns no prompt.
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
credential checks in `StatsAgent` are provider-aware.

After approval, extract `execute_plan`, `leader_game_floor`,
`normalize_duplicate_rank_plan`, `single_period_rank_plan` and the host plan
schema (`plan_schema`, `response_schema`) into public execution code. Keep
calculations and scope policy unchanged, and prove function identity across
provider integration.

The following is an **audit list**, not authorization to move whole functions or
modules. Before provider integration, inspect each symbol and its callers, then
record which responsibilities are public scope validation/conversation policy and
which are private planning/routing heuristics. Extract only public responsibilities
into public scope-policy code, with their existing tests. Split mixed functions
(such as `question_intent`) rather than publishing production heuristics wholesale;
retain private responsibilities behind the approved provider boundary. A symbol
may remain private if no public responsibility needs extraction.

| Symbol to audit (current private module) | Public consumers |
| --- | --- |
| `split_kind`, `question_intent` (`question_intent`) | `player_splits`, `routes`, `semantic_answer`, `research_ask` |
| `venue_mentions`, `round_scope_message`, `has_specific_metric` (`question_intent`) | `overview_calendar`, `performance_overview`, `player_comparison`, `service` |
| `explicit_aggregation`, `task_terms` (`question_intent`) | `semantic_answer`, `semantic_intent` |
| `name_reply`, `name_tokens`, `names_other_player`, `starts_new_question` (`question_intent`) | `name_matching`, `routes` |
| `explicit_scope` (`semantic_planner`) | `research_ask`, `semantic_intent`, `serving_warehouse` |
| `explicit_metric_mentions` (`planner`) | `research_ask`, `semantic_answer`, `semantic_intent`, `serving_warehouse` |
| `AgentRoute` labels (`router`) | `route_labels` |

`semantic_answer` uses the public intelligence host to reach `provider.plan`.
Planner prompts, model-backed planning and production planning
heuristics stay private. A symbol that turns out to need private planning data
gets a separately reviewed typed extension instead. Record the per-symbol
keep/extract/split decision and preserved test coverage. The ownership map and its
JSON rules change with the resulting implementation diff, not preemptively for
every name in the audit list. Public runtime modules no longer import
`question_intent`, `semantic_planner`, `planner` or `router`, apart from
one guarded optional legacy loader (below). Separate mixed execution/reference
tests from private intelligence cases; retain all test intent.

`legacy_repository` is a host precedence route and never goes through the
provider. It depends on private `build_agent_plan` and the production `planner`
query plans (`build_query_plan`, `deterministic_query_plan`,
`detect_opponent_breakdown`), which `service` reaches only through one guarded
optional legacy loader. In v2, when that private code is absent, the route returns
the existing sanitized unavailable response for repositories without a governed
adapter, never a governed-query fallback. Production keeps legacy behavior through
the legacy compatibility tree until a separately reviewed migration.

Build the private provider in its own namespace, not an installed top-level `app`
package. Test a built wheel against this public candidate in isolated environments;
record exact source commits and import origins. During transition, retain the
existing pinned dependency and legacy compatibility tree for their own checks.
The new integration environment runs the public app plus provider, not that legacy
tree via `PYTHONPATH`. Ownership checks include the package paths.
Released host tags support isolated package assembly; see the
[runtime guide](intelligence-runtime.md). Adopting a released host does not remove
the legacy tree or authorize deployment. Those remain separately reviewed gates.

## Review decision

The refined design was approved on 2026-10-24. The local implementation follows
explicit named selection, host-owned precedence and validation, per-symbol
ownership decisions, same-resolved-player comparisons, and host-only deadline
and budget failure codes. See the [ownership audit](intelligence-ownership-audit.md)
and [acceptance evidence](intelligence-acceptance.md).

Public publication still requires approval of the exact implementation diff.
Package installation does not authorize deployment or a dependency-pin change.

## Request identity and supported provider dependencies

The host supplies only unambiguously resolved current player mentions, or its
selected prior identities when no current name is present. Ambiguous aliases do
not authorize either candidate. `player_references` binds opaque request-local
handles to canonical names; the model schema permits only those handles or null.
Providers must use those supplied handles, not construct a roster catalog.
The host independently checks returned canonical identities against the original
request and authorized prior scope, including zero- and multiple-mention requests.
The gateway caps user payloads at 64,000 characters and retains its first failure;
a provider cannot turn a swallowed transport/budget failure into clarification.

The model-plan envelope adds required nullable `reason`: null for query/compare,
and a closed `Reason` value for withheld output. `ambiguous_aggregation` and
`unsupported_quarters` preserve specific host-owned guidance; arbitrary model
messages are discarded. The execution schema itself remains unchanged.
The host fills omitted literal scope and the neutral default window
before aggregation validation, rejecting conflicting non-default values.

Supported public dependencies for v2 providers are `intelligence_contract`, the
four execution helpers in the audit list, `scope_policy.explicit_scope` and
`scope_policy.has_specific_metric`, and `semantics.load_contract`/`SemanticError`
for metric canonicalization. This explicit helper surface is versioned with the
host release; it is not permission to import private app or legacy planner code.
Fantasy roster advice is a public scope clarification before route dispatch,
including overview and comparison; it is not shooting qualification.

## Version 2 compatibility and readiness

### Public helper compatibility

`app.agent.provider_host.HOST_HELPER_VERSION` identifies the supported helper
surface independently of `nba_intelligence/2` and the semantic contract version.
The initial value is `nba_provider_helpers/1`. `HELPER_AUDIT` records the eight
public execution/scope/semantic symbols above; it is an audit list, not an import
sandbox or permission to publish private functions. Contract records remain in
`intelligence_contract` and are versioned by `API_VERSION`.

Installed packages using this surface must call
`require_host_helpers(("nba_provider_helpers/1",))` during provider module import
or factory construction, before any request. The tuple must be the package's
literal supported versions, not copied from the host constant. Missing helpers,
a missing version module on an older host, or an unsupported version fail
discovery with the existing sanitized configuration error. Existing v2 providers
remain compatible; adoption of this check is a package release responsibility.

Change the helper version whenever removing a listed symbol or changing its
signature, return contract or behavior incompatibly. Update conformance tests
with it. A compatible host code release need not change the helper version.
Release verification must separately bind the approved tag to its exact SHA,
source tree, requirements and import origins: this version check alone cannot
detect a stale checkout, modified helper implementation or shadowing `app` package.
The public repository is still a source host, not a pip-installable distribution.

### Provider API and readiness

Version 2 replaces the published version 1 shape: request-local player references,
closed reason additions and planning-only `PromptBundle` are incompatible changes.
Discovery uses `nba_stats_desk.intelligence.v2` and requires the literal descriptor
version `nba_intelligence/2`. Packages declare the version they implement; they
must not borrow the host's current `API_VERSION` to claim compatibility. Version 1
hosts/providers fail discovery rather than failing individual planning calls.

Startup accepts any configured model-provider key/model pair. Each request then
checks only its selected model provider and returns sanitized `provider_unavailable`
if that pair is missing. Readiness is checked outside the cache: the immutable
provider factory is cached by entry-point name only, so credential changes do not
construct a second instance. Invalid package selection remains a startup error.

For reference and installed providers, the host enforces its 1–2,000 character
question bound before identity scanning. Candidate-bearing ambiguity clarifies
before provider classification or planning, preserves choices and uses zero model
calls, including when an earlier name is unknown or another name resolves exactly.
Unknown names remain subject to ordinary identity validation. Provider discovery
and readiness checks still apply; a misconfigured explicit provider never falls
back. Exact identities continue through planning, and the conversation identity
layer rewrites validated choices before planning resumes.

### Host releases

A host release is an annotated `intelligence-host-vX.Y.Z` tag on a `main` commit
whose push-to-`main` CI passed; the release is the source tree at that commit.
Use patch versions for compatible fixes and minor versions for compatible
additions. Breaking provider, helper or semantic changes need a migration review
and matching contract-version changes. A tag label alone does not prove
compatibility; run [acceptance](intelligence-acceptance.md) against the exact
commit. Published tags are never moved or reused; repairs ship as a new version.
Providers pin the tag object and its peeled commit, which can be checked with:

```sh
git ls-remote origin 'refs/tags/intelligence-host-vX.Y.Z' 'refs/tags/intelligence-host-vX.Y.Z^{}'
git merge-base --is-ancestor 'refs/tags/intelligence-host-vX.Y.Z^{}' origin/main
```

Release notes and contract versions for each tag are listed on the repository's
[releases page](https://github.com/mikeh-studio/nba-stats-desk/releases).
