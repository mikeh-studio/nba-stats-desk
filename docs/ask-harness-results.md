# Ask harness program results — 2026-10-18

These are historical results for the earlier planner contracts. The current
`research_intent/3` and `semantic_intent/3` contracts derive phase and aggregation
provenance on the server. The original recording remains unchanged, but current
strict replay rejects its incompatible case binding before comparing requests.
Controlled endpoint checks do not replace fresh provider measurement or establish
current model accuracy. See [evaluation boundaries](evaluation.md), the
[current research contract](ask-workspace.md#research-intent-contract), and the
[historical continuation results](ask-harness-continuation-results.md).

## Results and limits

The controlled endpoint inventory passed **147/147 cases** before and after the
changes below. Independent assertions were strengthened without changing the
denominator. The one provider capture used **14 calls across six fictional
cases** and passed **4/6**. At this historical checkpoint, offline replay passed
**5/6**, repairing only `research_home`; the original recording files and
`observed-outcomes.json` remain unchanged. No provider capture was repeated and
none of the six recorded-case expectations was retuned.

The remaining `date_range` mismatch was ambiguous average-versus-total wording,
not then a confirmed application defect. Its later reviewed behavior is recorded
in the [continuation results](ask-harness-continuation-results.md).

The replay repair is **in-sample regression evidence**, not held-out
generalization, live model accuracy, or production data validation. Alternate
fictional endpoint controls test specific identities, metrics, venues, phase
boundaries, and refusals.

## Changes and evidence

| Change | Evidence | Validation limit |
| --- | --- | --- |
| [#81](https://github.com/mikeh-studio/nba-stats-desk/pull/81): critical-risk coverage and independent raw-row oracle mutations | [Coverage tests](../tests/test_ask_coverage.py) | Coverage labels are distinct from endpoint success and human approval. |
| [#84](https://github.com/mikeh-studio/nba-stats-desk/pull/84): strict provider recording/replay, immutable bundle, and fresh JSON/SSE context | [Cassette tests](../tests/test_ask_cassettes.py) | The Python outbound-read guard is not an OS sandbox. |
| [#82](https://github.com/mikeh-studio/nba-stats-desk/pull/82): study prose/table/chart consistency and scope controls | [Endpoint tests](../tests/test_teammate_ask.py) | Scoped assertions do not establish universal claim binding. |
| [#83](https://github.com/mikeh-studio/nba-stats-desk/pull/83): opt-in request-linked traces with bounded storage, access checks, redaction and retention | [Trace tests](../tests/test_private_traces.py) | Secret detection is best effort; code/configuration provenance is limited. |
| [#85](https://github.com/mikeh-studio/nba-stats-desk/pull/85): failure-to-case drafts with source provenance | [Draft tests](../tests/test_ask_trace_draft.py) | New expectations require human approval. |
| [#86](https://github.com/mikeh-studio/nba-stats-desk/pull/86): bounded descriptive study-kind correction | [Research tests](../tests/test_descriptive_research.py) | The recorded repair is in-sample evidence. |

Each change received source review; findings were checked against the contracts
and tested. Source review does not substitute for human approval of expected
answers.

The historical descriptive correction applied the breakdown Regular Season
default only after matching an independently resolvable descriptive question.
It rejected ambiguous identities and unsupported fragments. Correctly classified
breakdown plans retained their scope, and immutable recordings preserved the
original model plan. The [current intent contract](ask-workspace.md#research-intent-contract)
supersedes that historical grammar.

## Historical reproduction and validation

These commands and counts describe the historical checkout. Use fresh output
directories. On current contracts the frozen replay command exits 2 on the
incompatible case binding; it is not a current provider-accuracy test.

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

At this historical checkpoint, replay deliberately exited 1 for the retained
date-range mismatch, consumed all 14 calls, passed integrity checks and made no
provider calls. Local validation passed **2,269 Python tests (9 skipped)**,
including **197 focused research/replay tests**, **147/147 controlled endpoint
cases**, **24/24 semantic fixtures**, and **95/95 JavaScript tests**. Ruff,
formatting, mypy (81 source files), public-boundary and diff checks also passed.
These are dated local results, not current CI or deployment evidence.

A synthetic seeded-defect exercise failed on an incorrect aggregate and passed
after its correction, with the source, expectations and grader unchanged. It
establishes the application-defect-to-draft loop, not independent model accuracy.

## Evidence reference and remaining limits

The [frozen methodology rubric](ask-harness-quality.md) remains unchanged, SHA-256
`31cca222e738cdf23e9e61852bfcd206ed4328f4b1e334360f4ff43b3eb34061`.

The checkpoint does not establish universal claim binding across every renderer,
complete sanitization of private traces, live data quality, deployment readiness,
or generalized model accuracy. The [binding inventory](ask-claim-bindings.md)
and [evaluation guide](evaluation.md) describe the supported checks and limits.
