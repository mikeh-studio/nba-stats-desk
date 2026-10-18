# Ask harness quality: FROZEN RUBRIC v1

Frozen before program results on 2026-10-18. The A–E program ships separate PRs;
this document does not authorize merging. 8.5/10 is an aspiration, not a passing
threshold. Do not revise these gates to improve a measured score. No score is
awarded by this document; each later award needs a linked private evidence ledger.

| Domain | Points | Concrete gates (each independently awarded) |
| --- | ---: | --- |
| Oracle and coverage | 2 | 0.5: explicit case IDs and assertion paths for identity, metric/units, date/phase, null/zero/incomplete, unsupported scope and conversational recovery, including research, availability and references; 0.5: independent raw-row arithmetic plus deliberate wrong-payload rejection; 1: human-reviewed expected cases with recorded reviewer provenance for the critical risks. |
| Strict replay and isolation | 2 | 0.5: missing/mismatched/unused recordings fail closed; 0.5: both endpoint transports and fresh context with no external reads; 1: current recorded-provider evidence passes those gates with immutable request/response and source hashes. |
| Factual path binding | 2 | 1: every supported displayed numeric claim binds to its exact evidence path and scope; 1: mutations to displayed prose, tables and charts independently fail while untouched controls pass. |
| Private failure capture | 1.5 | 0.5: opt-in bounded private capture; 0.5: redaction/access and retention tests; 0.5: failure paths retain request-linked evidence without changing public error behavior. |
| Regression drafting | 1 | 0.5: sanitized failure-to-case drafts validate and reproduce; 0.5: human approval is required before accepting expectations, with provenance preserved. |
| Delivery and review evidence | 1.5 | 0.5: relevant local tests and complete endpoint suite with both transports; 0.5: independent review findings resolved and exact pushed-head CI checked; 0.5: inspectable immutable program ledger with hashes, limits and unresolved gaps for each A–E PR. |

Human-reviewed accuracy and current recorded-provider gates earn no points while
that evidence is unavailable. Automated passes, agent review, synthetic controlled
plans and historical provider runs cannot substitute for those gates.

## Feature A evidence boundary

`python -m scripts.ask_eval.coverage` derives the current inventory from the
validated corpus. Risk selections are explicit editorial tags; routes/outcomes
are asserted labels, not inferred successful executions. Every assertion retains
its case ID, turn, path and expected value. Missing selections in custom corpora
are reported, not silently counted. The full suite remains the default; the
critical map is a review aid, not a replacement denominator.

Runner manifests separate full corpus and selected-case summaries from automated
results, execution mode and per-case frozen review provenance. Existing
comparability hashes remain unchanged. Browser review decisions are separate
local records and never automatically approve fixture expectations.

Independent arithmetic tests use fictional raw rows and hand-derived constants.
They do not establish upstream completeness, production accuracy, live provider
behavior or full narrative factual binding (Feature C). All operational ledgers,
responses and review artifacts stay in ignored `reports/`; public fixtures remain
synthetic. See [evaluation](evaluation.md) and [boundary policy](public-private-boundary.md).
