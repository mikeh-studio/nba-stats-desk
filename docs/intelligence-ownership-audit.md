# Phase 3 ownership audit

The function list in the approved contract was an audit list. Extraction follows
responsibility; the production classifier was not moved wholesale into public.

| Audited symbol | Decision and owner | Preserved coverage |
| --- | --- | --- |
| `split_kind` | Extract into public `scope_policy`: identifies existing public split-handler scope, never generates queries. | player-split, Ask routing and scope regressions |
| `question_intent` | Split: scope refusal/aggregation ambiguity becomes public `question_scope`; fantasy roster-advice clarification also remains in public scope policy. Public compatibility module exports only scope policy. | public scope/round/fantasy tests and installed-package regressions |
| `venue_mentions`, `round_scope_message`, `has_specific_metric` | Extract literal venue, unavailable dimension and conversation task-change guards into public scope policy. | overview/calendar, round, conversation and aggregation tests |
| `explicit_aggregation`, `task_terms` | Extract public aggregation provenance and conversation policy. Provider metadata remains advisory. | aggregation-provenance and dated-points controls |
| `name_reply`, `name_tokens`, `names_other_player`, `starts_new_question` | Extract public clarification/supersession policy. | name-matching, pending clarification, route tests |
| `explicit_scope` | Extract literal calendar/phase/venue constraints; inferred production planning stays private. | date/window, serving, scope and malformed-plan tests |
| `explicit_metric_mentions` | Extract canonical catalog alias validation; general production metric selection stays in the frozen legacy planner/private provider. | metric, dated-points and provenance tests |
| `AgentRoute` | Extract labels and response data contracts into `legacy_types`; route scoring remains legacy/private. | registered-tool contract and existing private router tests |
| `execute_plan`, `leader_game_floor`, rank normalizers, plan schemas | One public owner: `plan_execution`. Installed provider imports those exact function objects. | execution, semantics, serving and installed-wheel identity proof |

`semantic_planner` is now a compatibility export, with no production prompt or
model planner. Public runtime modules use the new owners directly. The static
import test includes function-level imports and permits private legacy imports
only in `legacy_loader`.

The old non-governed repository route is a deliberate transition exception in the
approved contract. Its existing planner/router and prompt text remain frozen
compatibility code; service accesses them only through the optional loader.
Missing compatibility code returns an unavailable response. This is never a
fallback for failed governed planning. Removing that legacy path requires the
separate migration decision in the contract.

Production-only tests formerly in public `test_semantic_planner` and
`test_agent_router` already exist in the private compatibility suite. Public
schema/execution/tool-contract assertions remain in `test_plan_execution` and
`test_legacy_route_contract`. Public orchestration tests use an explicit
`scripted_intelligence` fixture for authored plans, without production prompt text
or heuristics. Reference tests and conformance tests exercise the real loader.
The old private app/test tree and its submodule pin remain unchanged.

Review clarification: the six extracted execution/schema functions retain AST
identity with the base. Scope extraction also canonicalizes generic fantasy
metric aliases through the contract default; that is a behavior change, covered
by metric-policy tests. Fantasy advice clarification is restored in public and
now precedes broad handler dispatch. The startup guard remains in this approved
PR scope; splitting it now would rewrite the published series unnecessarily.

## Claim and presentation ownership

The executable ownership map remains authoritative. These public surfaces are
reviewed together; downstream compatibility copies do not acquire development
ownership. The backend evidence contract and browser DOM contract serve different
purposes and deliberately keep distinct version identifiers.

| Capability | Public owner / consumer | Regression evidence |
| --- | --- | --- |
| Evidence/destination binding, identity, metric, unit and scope | `claims.py`, `claim_rules.py`, `claim_contexts.py`, `claim_renderers.py`; HTTP finalization in `app/main.py` | `tests/test_claim_bindings.py`, `test_claim_review.py`, `test_claim_policy_integration.py` |
| Browser source-to-DOM binding (`ask_ui_claim_bindings/1`) | `app/static/ask_claim_bindings.js`; presentation, comparison and drawer renderers | `tests_js/ask_claim_bindings.test.js`, `tests_browser/claim_bindings.spec.js` |
| Observation producer, Python allowlist and ingestion (`ask_claim_rollout/1`) | `app/static/ask_claim_rollout.js`, `app/agent/claim_rollout.py`, `route_labels.py` | `tests/test_claim_observation_contract.py` exercises the real JS producer and Python consumer; `tests_browser/claim_rollout.spec.js` covers actual DOM-to-HTTP ingestion |
| Deterministic governed prose and saved responses | `semantic_answer.py`, public route renderers and `app/main.py` finalization/history | `tests/test_intelligence_prose_boundary.py`, `test_reference_intelligence.py`, `tests_browser/claim_storage.spec.js` |

Paths without a directory in the owner column are under `app/agent/`; bare
regression filenames are under `tests/`.
When changing an observation category, review both producers and the Python
origin-specific allowlists. Backend `response`/`ask_claim_bindings/2` observations
are not browser observations; browser ingestion rejects that origin. Unknown
browser route/surface values normalize to `unknown`, unknown invalid reasons to
`other`; unexpected wire keys and invalid enums are rejected. The producer test
also verifies first-invalid transitions, mixed-batch atomic rejection, backend
origin rejection and stale-mode rejection in both enforcement modes. Populated
browser tests verify reading the actual page meta tag. The Python-to-JS category
exercise covers the consumer's admitted browser categories; it is not an automatic
inventory of private JavaScript constants. Review new JS-only categories against
the Python allowlist before adding them; a consumer-driven test alone cannot
discover a producer-only addition.
Observation agreement does not prove evidence arithmetic or universal binding.
[Claim coverage and limitations](ask-claim-bindings.md) remain authoritative:
unbound surfaces can be incomplete, and log mode does not suppress every
contradiction. No enforcement default or contract version changes here.

## Model-call boundary

Each runtime call site is either bounded public selection/infrastructure or the
already retained legacy exception. New unexplained intelligence or factual-prose
paths block completion of an ownership review; do not silently label them legacy.
The v2 hook provides planning only, not an answer-generation extension.

| Call site | Classification and factual-output boundary | Coverage |
| --- | --- | --- |
| `intelligence.py` / `intelligence_contract.py` | Public gateway/protocol: provider planning text, schema-only proposals, host validation and `semantic_answer.py` deterministic rendering. No model answer fields. | `tests/test_intelligence.py`, `test_intelligence_prose_boundary.py`, `test_reference_intelligence.py` |
| `research_ask.py` | Public bounded research intent/scope selection; strict schema and literal/prior scope validation precede deterministic catalog/evidence presentation. Prompt strings select scope, not factual prose. | `tests/test_research_api.py`, `test_descriptive_research.py`, `test_research_phase_provenance.py` |
| `teammate_ask.py` | Public bounded frozen-study scope selection; schema and artifact scope checks precede deterministic narration. | `tests/test_teammate_ask.py` (scope mismatch, schema rejection and JSON/SSE) |
| `visualization.py` | Public selection of an existing verified chart candidate ID; no values, code or data requests accepted. | `tests/test_visualization.py` |
| `execution.py`, `claude_client.py`, `openrouter_client.py`, `service._create_response` | Public transport, retry/budget and compatibility adapters. Transport alone grants no rendering authority; each caller above/below owns its output restriction. | `tests/test_intelligence.py`, transport and service tests |
| Frozen legacy `planner.py` snapshot (private intelligence ownership, optional access through `legacy_loader.py`), legacy branches in `service.py` (`_answer_from_evidence` and repository tool loop), prompt access through `legacy_loader.py` | Retained non-governed legacy exception: can produce model-written prose/tables and streamed text. It is not extracted into the v2 package and cannot be entered as a governed failure fallback. Retirement requires separate migration review. | `tests/test_legacy_route_contract.py`, `test_agent_service.py`, `test_intelligence.py` (missing legacy and import boundary) |

This inventory describes current entry points, not general permission to introduce
public production intelligence. Additional private planning data for a special
handler requires the separately reviewed typed extension in the
[provider contract](intelligence-contract.md). The canonical app behavior and
reachability guide is [Ask workspace](ask-workspace.md).
