# Phase 3 ownership audit

The function list in the approved contract was an audit list. Extraction follows
responsibility; the production classifier was not moved wholesale into public.

| Audited symbol | Decision and owner | Preserved coverage |
| --- | --- | --- |
| `split_kind` | Extract into public `scope_policy`: identifies existing public split-handler scope, never generates queries. | player-split, Ask routing and scope regressions |
| `question_intent` | Split: scope refusal/aggregation ambiguity becomes public `question_scope`; fantasy recommendation classification remains private `intent.fantasy_decision`. Public compatibility module exports only scope policy. | public scope/round tests; existing private first-try/planner regressions and package tests |
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
