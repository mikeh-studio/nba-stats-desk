# Ask evidence and presentation bindings

The additive `ask_claim_bindings/2` contract names exact public evidence paths,
identity, metric, unit, scope and presentation destinations. Fixed transformations
render those sources; matching equal numbers is never a binding operation.
Evidence digests identify content, not independent source accuracy. Existing
payload consumers remain compatible.

The independent validator checks destination-to-source schemas, evidence hashes,
identity/scope contexts, units and presentation values. Finalization runs after
visualization enrichment in `StatsAgent.answer`, before governed JSON/SSE answers
are delivered. In enforcement mode, contradicted bindings produce a safe error response. Missing
bindings produce an explicit `incomplete` report and preserve compatibility.
This is not universal claim verification: unbound leaves are counted exactly with bounded representative paths, including
labels, identity and dates, rather than excluded to increase coverage. Formatting
constants, contract versions, game IDs and dates remain distinguishable by their
field paths; the report does not count every numeric token as a statistic.

## Route and subtype inventory

This inventory covers all thirteen entries in `app.agent.routes.ROUTES` and
additional branches inside their handlers. “Mapped” below refers to exact
structured destinations, not every surface in that route.

| Registry route | Actual construction owner / supported subtypes | Current explicit bindings and gaps |
| --- | --- | --- |
| unsupported_round | `service._answer`, scope refusal | No statistics returned; refusal text remains unbound |
| appearances | `appearance_ask.answer_appearances` | Table identities/counts and the deterministic full narrative identity/count/date/phase scope mapped; profile and source assumptions remain gaps |
| availability | `availability_ask.render_answer` | Main table labels, values and valid/observed sample pairs mapped to exact focal/exposure request and group metrics; audit date/opponent/participation/reported status/exclusion reason/warnings mapped; minutes-baseline audit prose, chart, inclusion-count narrative and profile remain gaps |
| research | `research.answer_payload`, `research_studies.study_answer` | Descriptive table identity, metric, values, units, aggregation and sample counts mapped; published-study descriptive table values/labels/units mapped; explorer fields remain gaps; prose/profile/filter explanations remain gaps |
| teammate_study | `teammate_ask.answer_study`, `study_claims` | Existing nine frozen mappings preserved and adapted to seven exact cells (two sample pairs and the interval combine operands); v1 narrative remains a gap |
| similarity | `reference_ask.render_reference`, `recent_similarity.render_recent_similarity` | Published score/name table; recent profile values, samples, dates, scores and identities mapped; prose, model-method constants, assumptions remain gaps |
| league_baseline | `reference_ask.render_reference` | Player/league values and sample counts mapped with distinct identities and scopes; narrative difference and scope assumptions remain gaps |
| award | `semantic_answer.SemanticAsk.answer`, `award_lookup` | Composes award identity with governed metric/overview results; child mappings retained, award intro/catalog-year text remains gap |
| player_split | `player_splits.answer_split` | Phase, venue, recent/prior, before/after values and differences mapped; side headers, metric labels, coverage pairs and scope assumptions mapped; main narrative remains a gap |
| player_comparison | `player_comparison.build_comparison`, `comparison_detail` | Core/additional scorecard values, percentiles, differences and comparison chart labels/geometry mapped to final DOM; generated prose, profile cards and methodology prose remain gaps |
| overview | `performance_overview.build_overview`, `overview_*` helpers | Primary/additional values, changes, percentile/peer position, cohort summaries and variation mapped to final DOM; generated prose/takeaways, time-series, profiles and generated sample prose remain gaps |
| metrics | `semantic_answer.render_answer`, `add_leaderboard_reference` | Summary/rank/percentile/current-baseline table values/identities/counts mapped; game-log date/game/team/phase/value and chart x/y mapped; contextual reference cells/gaps and exact scope/cohort/provenance/missing-sample/numerator-denominator assumptions mapped; table metric/date/phase titles and game-log chart identity/axis/series labels mapped; main narrative remains a gap |
| legacy_repository | `service._answer_from_evidence`, `_generate_answer`, compatibility tool loop | Preserved; arbitrary model-authored prose/tables are unbound. Allowlisted tools alone do not establish exact presentation support |

`service._answer` additionally handles reviewed event nonparticipation through
`participation.answer_known_nonparticipation`. This is factual identity/event/date
text, not a numeric metric result, and remains an explicit binding gap.

## Browser surface inventory

| Owner | Visible field families |
| --- | --- |
| `agent.js:renderAnswerPayload`, `renderScopeAssumptions`, `renderContext` | Markdown answer, clarification labels, scope assumptions, chips, source/metric notes, profiles |
| `agent.js:renderAuxiliaryPayload`, chart/table helpers | Generic table titles/columns/cells/descriptions; chart axes/labels/points/tooltips; chart selection after enrichment |
| `ask_presentation.js` | Overview primary/additional metrics, values, changes, percentiles, scope, insight/takeaway paragraphs, peer comparisons, variation and coverage |
| `ask_comparison.js` | Two-player identity/scope cards, metric values/percentiles/differences, additional sections, comparison chart and explanatory text |
| `agent.js:renderComparisonExplorer` | Published research summary cards, per-game dots/tooltips, grouping counts, missing-value counts, means and sample labels |
| `agent.js:renderPlayerProfile` | Identity/team/position, age/height/weight, sample and scope labels |

A browser that reads a canonical aggregate directly still needs an independently
checked source-to-DOM mapping. A source JSON path mapped to itself is not such a
mapping. Canonical prose copied into a paragraph can establish transport fidelity
but cannot independently establish the statistical semantics of its numbers.

The separate `ask_ui_claim_bindings/1` DOM contract links canonical snapshots to
exact leaf text and attributes. Overview/comparison tables and comparison chart
labels/geometry are mapped. Metric drawers map distribution summaries, point/bin
geometry, tooltips, axes and available sample counts; variation point/summary
geometry and peak details are mapped. Missing legacy sample fields and freeform
methodology notes remain gaps. DOM validation rejects missing/duplicate targets,
wrong source/metric/identity/scope and changed presentation, withholding the
contradicted surface in enforcement mode. It always reports remaining gaps rather than universal
verification. Snapshots/descriptors are transient DOM-session data, not extra
persisted response metadata. Generic charts/tables and research explorer still
have no DOM contract, even where backend table cells have exact bindings.

The approved omitted-aggregation policy disclosure remains an unbound policy
label. Its canonical query-plan policy determines the offset of following exact
scope/sample assumptions; JSON/SSE regressions check this combined behavior.

## Limits

Private request artifacts, source exports and evaluation ledgers stay outside Git.
The envelope contains only existing public route evidence and bounded source
identity records. No paid provider calls are required. Legacy Claude compatibility
may emit prose deltas before finalization; the governed fail-closed guarantee does
not apply to that unbound legacy stream. No universal rubric point or human review
approval follows from these mappings.

## Context and payload-size checks

Metric/split surrounding labels are checked independently of numeric cells.
Evidence-derived titles, side labels and scope prose have exact source operands.
Static column labels have fixed renderer-contract guards; they are not presented
as statistical evidence. Appearance prose binds its entire deterministic template
to identities, counts, season, phase and date operands. Availability audit rows
retain exact sorted source paths; imported labels are never matched by value.

The v2 manifest interns paths as a prefix trie, metric/unit descriptors and
fixed transforms. Claims reference exact destination/source paths and shared
identity/scope/context anchors; each evidence root has one digest. Validation
expands these references and applies the independent route rules. It rejects
Boolean indexes, forward/cyclic or dangling references, duplicate registry entries,
unsafe paths and extra fields. Valid unused prefix nodes may remain after chart
selection; they do not establish a claim. Construction indexes are not serialized.

The post-review conservative two-ranking renderer stress (not a supported planner
comparison), 100 rows per table, serializes to 241,364 bytes, including 144,356
binding bytes (originally 1,516,865). The supported single 100-row rank through
`execute_plan` uses 124,500 total / 72,198 binding bytes. These measurements differ
slightly from the earlier compact checkpoint because chart selection is now an
explicit registry field. Regression bounds remain 500,000 total / 400,000 binding
bytes for the stress fixture and 3,000 binding bytes for the small descriptive
summary fixture.

Gap reporting traverses every rendered field but stores at most 32 representative
unbound paths, exact per-root/total counts and an explicit truncation flag. Errors
similarly retain exact counts and at most 16 details. The controlled populated
overview endpoint uses 83,844 total bytes, including a 1,432-byte report counting
2,598 unbound leaves (formerly 261,251 total bytes before bounded reporting).
Overview, comparison and explorer regression shapes each count 30,000 leaves while
keeping their report under 4 KB. Samples are never represented as an exhaustive
list. Followups, period options and clarification options are included as unbound
rendered families.

These are fixture regression bounds, not production truncation limits. The
existing SSE 1,650-byte incremental queue budget is unchanged: its complete
terminal event is separate and delivered without truncation. Local JSONL history
roundtrips the full payload. Browser storage retains its quota and eviction policy.
No universal terminal-response size or browser quota guarantee is claimed.

Date/datetime and finite Decimal evidence normalize at the JSON boundary; nonfinite
or unsupported binding sources produce a governed mismatch response. Safe route,
query and conversation context survives that response; unsafe context is omitted.
Canonical descriptors are derived from evidence. Fixed route formatting rules,
not renderer-supplied unit hints, check displayed units and scaling. Aggregation is
bound through canonical scope/context; generic metric table headers do not always
display average-versus-total, and unbound narrative/policy labels are not thereby
certified. Python proof hashes are not a cross-language numeric canonicalization
standard: JavaScript may rewrite 5.0 as 5. Client-restored backend proof is retained
for inspection, not revalidated by a server history endpoint. The separate DOM
contract validates restored presentation against its canonical snapshot.

## Observation rollout

`ASK_CLAIM_ENFORCEMENT` accepts `log` (default) or `enforce`; any other explicit
value fails configuration. Both modes validate mapped bindings. Log preserves a
known contradicted response/surface and normal conversation effects, while attaching
an invalid report. This is a temporary observation choice, not verification.
Enforce withholds contradicted surfaces and cancels pending successful conversation
effects. Missing legacy bindings remain incomplete in both modes. Existing
identity/date/phase, unsupported-intent, read-only and JSON-serialization safeguards
remain enforced; serialization failures return a safe error in both modes.

The server renders the current mode into the Ask page. A module snapshots that
configuration before answer rendering; saved payload mode never overrides it.
History restoration under enforce also withholds a saved backend-invalid answer,
including generic tables without DOM mappings. This does not revalidate or trust
JavaScript-mutated backend proof. Mode changes require process/deployment restart and page reload; editing the
environment alone does not update a running process. Already
open pages retain their snapshot. Browser reports from a stale mode receive a
bounded rejection rather than being relabeled with the new mode. Rollback selects
log, restarts/redeploys the process, and reloads the current app; older safeguards stay active.

Private `ask_claim_observation` logs use an independent strict field projector:
origin-specific finite route/surface/status/reason/version/mode enums and bounded
assessed/invalid counts. Bound and unbound counts remain in validation reports,
not telemetry: their backend and browser units differ. Route labels come only
from canonical payload routes; absent or unrecognized routes are unknown.
They contain no request/conversation IDs, names, questions, answers, values, paths,
source or query scope, or arbitrary error messages. Backend emits one assessment
per finalized response. Browser emits one assessment per displayed surface and at
most one later first-invalid transition. Repeated hover/redraw does not add an
assessment or repeat a mismatch; changing drawer metric/view or restoring a new
answer creates a new consecutive display instance. A→B→A counts three instances;
repeated toggling does not establish independent activation evidence. Transition events have assessed=0 and must not be used
as additional denominators. They record only the first failure category per view.

The browser buffer is capped at16 events, sent in batches without retries. The
same-origin-only endpoint requires Fetch Metadata and compares an optional Origin
against the public Host authority, accepting HTTP/HTTPS without trusting forwarded
headers. This supports TLS termination with an internal HTTP ASGI connection;
malformed or duplicate origins are rejected. It accepts JSON only, limits actual
streamed bodies to8192 bytes and batches to16, admits at most120 requests/minute
per process, and permits four concurrent body readers with a two-second timeout.
Absent Fetch Metadata is rejected. There is no client identity map, query API,
new database or file store. Logging failures do not affect Ask or UI behavior.
Browser events are untrusted and best-effort: queue overflow, stale-page rejection,
network/admission limits, offline pages and navigation can lose observations.
Counts are received assessments, not a complete traffic denominator or an overall
failure rate. Existing private deployment log retention and IAM policies apply.

Activation requires representative assessed response and rendered-view volume for
each mapped route/surface, including legacy/history paths, separate complete,
incomplete and invalid counts, investigation of mismatches and delivery losses,
and an explicit operator decision. Zero mismatches without denominators is not an
activation gate. This rollout measures mapped binding consistency, not warehouse
truth, complete claim correctness or provider accuracy.

DOM assessments currently cover mapped overview/comparison tables, comparison
charts and metric drawers only. Generic/unmapped views and known-backend-invalid
answers suppressed on restoration do not emit DOM assessments. Their displayed
volume is not established by this telemetry. The bounded local drop counter is
session-only and includes overflow, failed/rejected delivery and projection errors;
it is not delivered as a reliable server loss denominator. At most one browser
batch is in flight, with a two-second abort, no response-body read and no retries.

The local, bounded UI failure counter counts every invalid validation attempt in
both modes; it is separate from deduplicated observations and is not a view denominator.
Strict JSON preflight rejects unsupported or recursive values in both modes before
claim traversal. Unsafe payloads receive a constant invalid report with inventory
unavailable, and the backend retains only independently serializable context fields
of at most8192 UTF-8 bytes each. Internal binding markers never leave finalization.
