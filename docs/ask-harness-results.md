# Ask harness program results — 2026-10-18

**Current contract addendum:** The results and v2 rework statements below are
historical checkpoints. Later authorized private v2 measurement exposed provenance
refusals and dated-total failures, summarized in the public [PR86 findings](https://github.com/mikeh-studio/nba-stats-desk/pull/86)
and [PR87 findings](https://github.com/mikeh-studio/nba-stats-desk/pull/87).
The current research and semantic v3 corrections derive provenance on the server;
new synthetic endpoint controls establish only their controlled behavior. Fresh
v3 provider measurement is pending. Historical scores, recordings and approval
receipts below remain unchanged; this addendum claims no new accuracy score.

**Historical checkpoint:** The results below describe the earlier planner contract.
The subsequent `research_intent/2` rework passes the unchanged expected checks in
147/147 controlled synthetic endpoint cases. It changes planner inputs and cannot
reuse the old provider recording as current model evidence: strict replay rejects
the incompatible binding. Original recordings, historical scores and approval
receipts remain unchanged. No new provider capture or model-accuracy score is
claimed. See [evaluation boundaries](evaluation.md) and the
[current research contract](ask-workspace.md#research-intent-contract).

See also the [historical continuation assessment](ask-harness-continuation-results.md).

The controlled endpoint inventory remains **147 cases** and passes **147/147
before and after**; A and C strengthened independent assertions without changing
the denominator.
The one authorized provider capture used 14 calls across six fictional cases and
passed **4/6**. At that historical checkpoint, offline replay passed **5/6**, repairing only
`research_home`; all 16 original recording files and `observed-outcomes.json`
remain immutable. No paid capture was repeated and none of the six recorded-case expectations was
retuned.
The remaining `date_range` mismatch is ambiguous average-versus-total wording
with an unreviewed oracle, not a confirmed application defect.

The 5/6 result is an **in-sample regression repair**, not held-out generalization,
live model accuracy, or production data validation. Alternate fictional endpoint
controls test specific identities, metrics, venues, phase boundaries, and refusals.

## Changes and review

Dependency order is A → B → C → D → E → F. These are separate dependent PRs;
this program does not merge or deploy them.

| Feature | Change and evidence | Single external review: resolved issues or clarified limits |
| --- | --- | --- |
| [A — #81](https://github.com/mikeh-studio/nba-stats-desk/pull/81) | Explicit critical-risk coverage and independent raw-row oracle mutations; [tests](../tests/test_ask_coverage.py) | Coverage labels remain distinct from endpoint success and human approval; safe coverage reporting. |
| [B — #84](https://github.com/mikeh-studio/nba-stats-desk/pull/84) | Strict provider recording/replay, immutable original bundle, fresh JSON/SSE context and Python outbound-read guard; [tests](../tests/test_ask_cassettes.py) | Request/manifest integrity, source isolation, response completion and timing boundaries; Python guard is not an OS sandbox. |
| [C — #82](https://github.com/mikeh-studio/nba-stats-desk/pull/82) | Study prose/table/chart evidence consistency and scope controls; [endpoint tests](../tests/test_teammate_ask.py) | Scope validation, displayed precision and contrast metadata; no universal binding claim. |
| [D — #83](https://github.com/mikeh-studio/nba-stats-desk/pull/83) | Opt-in private request-linked traces with bounded storage, access checks, redaction and retention; [tests](../tests/test_private_traces.py) | Admission bounds, retention locking and redaction; best-effort secret detection and limited code/config provenance remain explicit. |
| [E — #85](https://github.com/mikeh-studio/nba-stats-desk/pull/85) | Reviewed failure-to-case drafts with source provenance and always-pending human approval; [tests](../tests/test_ask_trace_draft.py) | Private source copying, fictional reconstruction privacy, trace provenance and crash-safe output ordering. |
| [F — #86](https://github.com/mikeh-studio/nba-stats-desk/pull/86) | Bounded descriptive study-kind correction with the existing breakdown default; [tests](../tests/test_descriptive_research.py) | Precise phase/dispatch scope, safe Unicode metric rejection, literal independent expected values, replay route assertions and in-sample disclosure. |

Each feature received exactly one static public-source-bundle review from
`claude-opus-5-5` at High effort. That reviewer did not run the repository or tests.
Implementation agents validated the findings and ran the checks. Raw review
prompts and outputs remain private; agent review is not human oracle approval.

F full-matches an independently resolvable descriptive question before changing a
study kind. It accepts no exposure, prior research scope, extra filters, ambiguous
identity or unsupported question fragment. Only this correction applies
`ResearchQuery`'s Regular Season default instead of an inherited study Both default.
Correctly classified breakdown plans, including Both, remain unchanged. Model
provenance is retained and the route reason identifies the correction; immutable
provider recordings preserve the original raw plan. Live diagnostic projections
are not a guarantee of complete original planner reconstruction. See the
[current intent contract](ask-workspace.md#research-intent-contract), which supersedes that historical grammar.

## Historical reproduction and validation

These commands and counts describe the historical checkout. In the current
`research_intent/2` checkout, controlled evaluation is supported, while the frozen
replay command exits 2 on the changed case binding before comparing requests.
Use fresh report directories:

```bash
python -m scripts.ask_eval.coverage
python scripts/evaluate_ask.py --output reports/ask-harness/controlled-new
python scripts/evaluate_ask.py --replay-cassettes tests/fixtures/ask/recordings/openai-mini-20261018 --output reports/ask-harness/replay-new
python -m pytest -q
python scripts/evaluate_semantics.py --output reports/semantic-layer/new.json
npm run test:tracking
ruff check .
ruff format --check .
mypy
```

On the historical checkout, replay deliberately exits 1 for the retained date-range mismatch while consuming
all 14 recordings, passing the harness integrity gate, and making zero provider
calls. The CI regression asserts both transports' repaired route/reason and every
other original outcome. The final integrated local tree passed **2,269 Python
tests (9 skipped)**, **197 focused research/replay tests**, **147/147 controlled
endpoint cases**, **24/24 semantic fixtures**, and **95/95 JavaScript tests**.
Ruff, formatting, mypy (81 source files), staged public-boundary and diff checks
also passed. Exact pushed-head CI is separately required; a local pass is not CI
or deployment evidence.

A private E demonstration seeded an application aggregate defect in an isolated
copy: means/ratios received an extra one. Independent arithmetic was
`sum([0,2,4,6,8,10])/6 = 5`. Initial and JSON-/SSE-derived draft runs failed 0/1 on
the defective app and passed 1/1 on the corrected app. Case, source, grader and
fixture-adapter hashes stayed identical before/after; only the application changed.
A separately hashed private server copy enabled trace capture, while the tracked
evaluation server keeps capture disabled. No provider or warehouse calls occurred.
This demonstrates an application-defect-to-draft loop, not grader self-confirmation.

## Frozen assessment

The [frozen rubric](ask-harness-quality.md) remains byte-identical, SHA-256
`31cca222e738cdf23e9e61852bfcd206ed4328f4b1e334360f4ff43b3eb34061`.
The supported maximum is **8.0/10, provisional until all six exact-head PR checks
and the immutable closeout ledger complete**. This is below the 8.5 aspiration;
no heuristic partial credit is added.

| Domain | Supported points | Evidence boundary |
| --- | ---: | --- |
| Oracle and coverage | 1/2 | Explicit critical map 0.5; independent arithmetic and wrong-payload rejection 0.5; human-reviewed critical expectations 0. |
| Strict replay and isolation | 2/2 | Strict missing/mismatched/unused gates 0.5; both transports/fresh context and Python outbound guard 0.5; historical immutable real-capture replay evidence 1. Not OS-level isolation. |
| Factual path binding | 1/2 | Independent displayed prose/table/chart mutation controls 1; universal exact binding across all supported claims/renderers 0. |
| Private failure capture | 1.5/1.5 | Bounded opt-in capture, access/redaction/retention controls and request-linked failure evidence. |
| Regression drafting | 1/1 | Validating reproductions and provenance; drafts always remain pending human approval. |
| Delivery and review | Up to 1.5/1.5, conditional | Relevant local/end-to-end checks 0.5; resolved review and all exact-head CI 0.5; immutable per-feature ledger 0.5. Final two gates await closeout. |

The private `program-closeout-ledger.json` records per-feature heads, hashes,
validation, review dispositions and unresolved limits. It is intentionally not a
public file link. The final closeout must confirm CI against the actual pushed
heads before treating the provisional total as earned.

Remaining justified work is independent human review of critical oracles,
including date-range wording/aggregation, and shared exact claim bindings across
all supported renderers and the generic legacy repository route, with independent
prose/table/chart mutations. The score does not establish live data quality,
deployment readiness, generalized model accuracy, or complete secret removal
from best-effort private traces.
