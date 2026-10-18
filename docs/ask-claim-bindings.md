# Ask evidence and presentation bindings

The additive `ask_claim_bindings/2` contract names exact public evidence paths,
identity, metric, unit, scope and presentation destinations. Fixed transformations
render those sources; matching equal numbers is never a binding operation.
Evidence digests identify content, not independent source accuracy. Existing
payload consumers remain compatible.

The independent validator checks destination-to-source schemas, evidence hashes,
identity/scope contexts, units and presentation values. Finalization runs after
visualization enrichment in `StatsAgent.answer`, before governed JSON/SSE answers
are delivered. Contradicted bindings produce a safe error response. Missing
bindings produce an explicit `incomplete` report and preserve compatibility.
This is not universal claim verification: unbound leaves are listed, including
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
contradicted surface. It always reports remaining gaps rather than universal
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

The conservative two-ranking renderer stress (not a supported planner comparison),
100-rows-per-table regression serializes to 241,380
bytes, including 144,333 binding bytes with ordinary JSON encoding (previously
1,516,865 binding bytes). The ordinary points summary through `StatsAgent` uses 8,248 total / 2,869
binding bytes. The supported single 100-row rank through `execute_plan` uses 124,293 total /
72,175 binding bytes. The two-player summary fixture uses 2,352 binding bytes.
Regression bounds are 500,000 total/400,000 binding bytes for ranking and 3,000
binding bytes for summary. These are regression bounds, not production truncation
limits. The existing SSE 1,650-byte incremental queue budget is unchanged: its
complete terminal event is separate and delivered without truncation. Local JSONL
history roundtrips the full payload with valid bindings. Browser storage remains
subject to its existing quota and eviction policy; no universal terminal-response
size or browser quota guarantee is claimed.
