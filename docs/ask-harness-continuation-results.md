# Ask harness continuation results — 2026-10-18

This continues the [historical harness results](ask-harness-results.md). Counts
and measurements below describe the earlier planner contracts, not the current
integrated result or deployment state.

## Current compatibility boundary

The current `research_intent/3` and `semantic_intent/3` contracts derive phase and
aggregation provenance on the server. Controlled planner inputs have migrated
while expected checks and fictional source bytes remain unchanged. Human review
covered **26 selected critical expectations**; it did not approve all 147 cases,
new planner-input bindings, generalized model accuracy or live warehouse results.

The original 14-call recording remains unchanged. Current strict replay rejects
its incompatible case binding before request matching, so the historical 6/6
result is not current provider-replay success. Later v2 measurement exposed
provenance-label refusals and dated-points scope failures, summarized in the
[#86](https://github.com/mikeh-studio/nba-stats-desk/pull/86) and
[#87](https://github.com/mikeh-studio/nba-stats-desk/pull/87) findings. These are
regression evidence. Controlled checks of the v3 corrections do not replace
fresh provider measurement.

## Changes and evidence

**Critical expectations.** The 26 reviewed contracts cover identity, units,
date/phase scope, zero/null/incomplete samples, unsupported scope and recovery.
Automated passes and source review do not substitute for human review of those
expectations. Public fixture review flags remain unchanged.

**Dated-points default ([#87](https://github.com/mikeh-studio/nba-stats-desk/pull/87)).**
When aggregation is omitted in the reviewed dated-points question, the default
is the mean over games with valid points, disclosed as points per game. The
original fixture returns **4 points per game**. Independent JSON/SSE controls
with values 0, 7 and null return **3.5** over two valid games, with three observed
games. Zero remains valid and null is excluded metricwise. Explicit totals and
per-game requests retain their behavior; unsupported conditions do not acquire
invented defaults.

**Evidence bindings ([#88](https://github.com/mikeh-studio/nba-stats-desk/pull/88)).**
Backend mappings connect structured presentation destinations to canonical source
paths, identity, metric, units, scope and fixed transforms. Independent validation
uses route contracts so equal-valued wrong-source swaps cannot pass merely
because numbers match. Contradicted governed output is withheld before JSON/SSE
delivery and cannot become successful follow-up context. Missing bindings remain
explicitly incomplete; legacy prose is not given invented proof.

Browser contracts connect canonical snapshots to exact DOM text and attributes
in overview/comparison tables, comparison charts, drawers and variation details.
Independent mutations check values, scope, labels, geometry and missing or
duplicate destinations. These checks establish presentation fidelity to declared
evidence, not upstream truth or statistical methodology. See the
[route and browser inventory](ask-claim-bindings.md).

## Immutable replay history

| Checkpoint | Original six-case recording | Interpretation |
| --- | ---: | --- |
| Original capture | 4/6 | One provider capture; 14 calls |
| Descriptive-route correction | 5/6 | Repaired `research_home` |
| Dated-points and binding checkpoint | 6/6 | Reviewed default repaired `date_range` |

These historical runs used the original recorded bytes and unchanged
expectations. Replay consumed all 14 calls, passed integrity/isolation gates and
made no provider calls. Repairs are **in-sample regression evidence**, not
held-out generalization, current live-provider accuracy or streaming-timing
validation.

| Artifact | Historical SHA-256 |
| --- | --- |
| Case corpus | `c8777b2c0564f72e8fb2781f4187da2ac0ac0de9133f301a497ae6bb44c92be0` |
| Fictional source | `95f0da617beaa68cf5cedae01199109f82d6c26d05b64a4262012e0b2af98f73` |
| [Frozen rubric](ask-harness-quality.md) | `31cca222e738cdf23e9e61852bfcd206ed4328f4b1e334360f4ff43b3eb34061` |

## Metadata and browser storage

The compact v2 binding manifest retains descriptors, transforms and root digests.
At the initial checkpoint, a **two-table, 100-row-per-table renderer stress test**
fell from **1,516,865 to 144,333 binding bytes**. This is not a reachable planner
comparison: the planner does not compare two rankings. The supported single
100-row rank used 72,175 binding bytes; a separate actual JSON/SSE endpoint fixture
used **59,342 binding bytes** (107,261 total). Ordinary points-summary metadata
was 2,869 bytes. These are fixture measurements, not universal size bounds.

After review fixes, the stress proof was 144,356 bytes and the supported
single-rank proof was 72,198 bytes, reflecting explicit chart-selection
provenance. Bounded inventory reporting reduced the populated overview response
from 261,251 to 83,844 total bytes, with a 1,432-byte report counting 2,598 unbound
leaves.

Before those review fixes, Chromium tests retained six ranking answers across
three chats through reload and a second page. Deliberate quota pressure evicted
the oldest chat, displayed the existing notice and preserved newer conversations.
The ranking scenario stored 646,087 bytes; a six-overview-answer scenario stored
1,570,003 bytes. DOM snapshots remain transient and are not saved with responses.

## Historical validation and remaining limits

The integrated checkpoint passed:

- **2,415 Python tests**, with 9 skips, plus final affected API/static checks.
- **147/147 controlled endpoint cases**, exercising JSON and SSE.
- **6/6 historical recorded cases**, consuming 14 calls without provider access.
- **24/24 semantic cases** and **102/102 JavaScript tests**.
- **65 final Chromium checks**: 48 Ask interactions, 16 binding/compatibility
  checks, and one ranking storage/quota scenario. These included restored history,
  redraw/drawer behavior and locale/timezone controls.
- Ruff, formatting, mypy, static-version, public-boundary and diff checks.

Each change received source review, with findings checked against contracts and
tested. These dated local results do not establish current CI, human approval of
every expectation, or deployment readiness. Reproduction commands are in the
[preceding results](ask-harness-results.md#historical-reproduction-and-validation).

Remaining gaps at this checkpoint include arbitrary legacy prose and early
streaming, generic browser charts/tables and research explorer, generated overview
and time-series/profile/sample prose, comparison methodology prose, and some
legacy drawer fields. Some backend fields are mapped where their generic browser
rendering has no DOM contract. See the [binding inventory](ask-claim-bindings.md)
for the supported surface. These results do not establish universal factual
correctness, live data quality or complete secret removal from private traces.
