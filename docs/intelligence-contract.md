# Intelligence provider contract

Ask separates **planning** from **execution**. A provider turns a question into a
proposed plan; the host validates that plan, runs governed queries and renders the
answer deterministically. The built-in `reference` provider plans without a model.
An optional installed provider can supply model-backed planning through the
`nba_intelligence/2` contract described here. See
[runtime behavior](intelligence-runtime.md) for configuration.

## Boundary

The host owns discovery, request context, conversation state, identity resolution,
route dispatch, scope validation, metric execution, evidence, claim validation and
presentation. A provider supplies planning prompt text, intent classification and
proposed routes/plans.

Providers return data, never SQL, formulas, callable names, executable code,
warehouse handles, tool lists, raw statistics or claimed evidence. Only the host
authorizes execution. These are interface constraints for trusted installed Python
code, not a sandbox: installing a provider grants normal process privileges. No
provider is loaded from a request, URL, arbitrary import string or user text.

## Discovery, compatibility and lifetime

`ASK_INTELLIGENCE_PROVIDER` defaults to `reference`, which never imports external
providers even if installed. Any other value names exactly one entry point in the
fixed group `nba_stats_desk.intelligence.v2`; its zero-argument factory is called
once at startup. An installed package is inert until selected.

Missing or duplicate names, invalid descriptors, factory errors, unknown route IDs,
and incompatible API or semantic-contract versions fail startup with a sanitized
configuration error. There is no fallback to reference. A descriptor with
`requires_model` also fails startup when no planning model or credentials are
configured; a later transport outage is a per-request failure.

Packages declare the literal version they implement (`nba_intelligence/2`) rather
than borrowing the host's `API_VERSION`. Version 1 hosts and providers fail
discovery instead of failing individual planning calls. New required fields or
meanings need a versioned contract change and shared tests.

Provider instances are immutable and reentrant. Factories, `prompts` and `classify`
perform no I/O. `plan` is asynchronous and cooperates with cancellation; the host
owns deadlines and model transport.

## Request flow

1. **Host precedence.** Input limits, pending clarifications and follow-ups use
   public conversation rules. Scope refusals and routes gated by settings or
   conversation state dispatch without the provider. A follow-up such as “What
   about rebounds instead?” keeps authorized prior scope but still needs a new
   plan. Only routes eligible for this request, configuration and provider enter
   `PlanningContext.routes`.
2. **Context.** `PlanningContext` holds the current question (1–2,000 characters),
   selected season, calendar date and at most one host-selected prior scope. Prior
   answers, evidence rows, secrets, settings and repository objects never cross the
   boundary. The current explicit scope wins.
3. **Classification.** `classify` returns an advisory `IntentSelection`, a
   `Withheld` result or a `ProviderFailure`. It is deterministic in v2. A selected
   handler route dispatches after the host rechecks eligibility, without `plan`.
4. **Planning.** For a query route, the host passes one `PromptBundle`, a host-owned
   budget and an optional model gateway. The gateway uses a fixed plan schema,
   configured model and bounded text; providers cannot choose endpoints,
   credentials, tools or budgets. Every transport attempt, including retries,
   consumes host budget, and usage is measured by the host.
5. **Structural validation.** Result variant, fields, types, enums, route, plan kind
   and query count are checked first. Unknown fields or malformed objects fail
   closed, including in-process typed instances.
6. **Narrow normalization.** Only `normalize_duplicate_rank_plan` and
   `single_period_rank_plan` may collapse identical ranking queries or an accidental
   second season, and only when their predicates prove it is safe. An explicitly
   requested season is never discarded.
7. **Semantic validation, then execution.** The host derives aggregation, resolves
   identity and validates metric, dates, phase, filters and qualification against
   the [semantic contract](semantic-contract.md). A `compare` plan must name the
   same resolved player, metric and aggregation on both sides; two-player
   comparisons use the `player_comparison` handler. Unsupported or ambiguous
   requests produce no partial answer.

The order **structural validation → narrow normalization → semantic validation →
execution** is mandatory; neither normalization nor follow-up resolution skips a
step.

`player_name` is an untrusted mention, and no model-supplied IDs are accepted.
`aggregation_source` is diagnostic: `unspecified` triggers public aggregation
resolution or clarification, never a guess. `league_leader` qualification applies
the public appearance-floor policy and cannot relax it. v2 adds no custom formulas,
multi-query batches, cross-season pooling or arbitrary handler payloads.

## Identity

The host passes only unambiguously resolved current player mentions, or its
selected prior identities when no name is present. `player_references` binds opaque
request-local handles to canonical names, and the model schema allows only those
handles or null. Providers never receive a full roster. Ambiguous names clarify
before classification or planning, with zero model calls. The host rechecks returned
identities against the original request and authorized prior scope.

## Prompts, failures and observability

`PromptBundle` carries a revision and inert planning-system text only; v2 has no
answer-generation prompt. Host safety instructions and schema take precedence over
provider text, and questions are data, never instructions. Prompt text and raw model
replies never appear in responses, logs, fixtures or errors.

`Withheld` carries a closed reason code and no statistics or free-form message.
Timeouts, transport errors, malformed output and exhausted budgets are failures,
not unsupported questions. Providers may return only `provider_unavailable` or
`invalid_provider_output`; the host alone assigns `provider_timeout` and
`budget_exhausted`. Failures use the existing sanitized JSON/SSE error envelopes,
with no second provider, hidden model fallback, partial plan or fabricated evidence.

Diagnostics record provider, version, route, outcome, latency, host-measured usage
and prompt revision, never prompt text.

## Supported host helpers

Providers may import `intelligence_contract`; the execution helpers `execute_plan`,
`leader_game_floor`, `normalize_duplicate_rank_plan` and `single_period_rank_plan`
from `plan_execution`; `explicit_scope` and `has_specific_metric` from
`scope_policy`; and `load_contract` and `SemanticError` from `semantics`. These
helpers are part of the `nba_intelligence/2` contract: removing one or changing its
signature or behavior incompatibly requires a new API version.

## Model-call boundary

Every runtime model call is either bounded public selection or transport, or the
retained legacy route. v2 offers planning only, not answer generation.

| Call site | Boundary | Coverage |
| --- | --- | --- |
| `intelligence.py`, `intelligence_contract.py` | Provider planning text and schema-only proposals; host validation and deterministic rendering in `semantic_answer.py`. | `test_intelligence.py`, `test_intelligence_prose_boundary.py`, `test_reference_intelligence.py` |
| `research_ask.py` | Bounded research scope selection under a strict schema; deterministic catalog and evidence presentation. | `test_research_api.py`, `test_descriptive_research.py` |
| `teammate_ask.py` | Frozen-study scope selection with schema and artifact checks before deterministic narration. | `test_teammate_ask.py` |
| `visualization.py` | Selects an existing verified chart candidate ID; no values, code or data requests. | `test_visualization.py` |
| `execution.py`, `claude_client.py`, `openrouter_client.py` | Transport, retry and budget adapters with no rendering authority. | `test_intelligence.py`, transport tests |
| `legacy_loader.py` and legacy branches in `service.py` | Optional legacy repository route that can produce model-written prose. It is never a fallback for governed planning and returns a sanitized unavailable response when the optional code is absent. | `test_legacy_route_contract.py`, `test_agent_service.py` |

## Conformance tests

- `tests/test_intelligence.py`: discovery, selection, hostile providers,
  validation order, failure codes and budget accounting.
- `tests/test_reference_intelligence.py`: the standalone reference examples over
  JSON and SSE with no model key.
- `tests_browser/reference_intelligence.spec.js`: browser smoke test of the
  reference provider with all model keys absent.

All execution evidence in these tests is synthetic fixture data. They establish
integration behavior, not live-model language quality or warehouse readiness.

## Host releases

A host release is an annotated `intelligence-host-vX.Y.Z` tag on a `main` commit
whose push-to-`main` CI passed; the release is the source tree at that commit.
Use patch versions for compatible fixes and minor versions for compatible
additions. Breaking provider or semantic changes need matching contract-version
changes. A tag label alone does not prove compatibility; run the conformance tests
against the exact commit. Published tags are never moved or reused; repairs ship as
a new version. Providers pin the tag object and its peeled commit, which can be
checked with:

```sh
git ls-remote origin 'refs/tags/intelligence-host-vX.Y.Z' 'refs/tags/intelligence-host-vX.Y.Z^{}'
git merge-base --is-ancestor 'refs/tags/intelligence-host-vX.Y.Z^{}' origin/main
```

Release notes and contract versions for each tag are listed on the repository's
[releases page](https://github.com/mikeh-studio/nba-stats-desk/releases).
