# Evaluation workflow

The repository includes deterministic semantic checks and an offline workflow
for generating and reviewing ten evidence-backed answers. Model calls run through
Codex CLI outside the public Ask request handler. Generated results, source
snapshots, curated cases, and human notes belong in ignored `reports/` directories.

## Offline checks

```bash
python scripts/evaluate_semantics.py --output reports/semantic-layer/evaluation.json
python -m pytest tests/test_reporting_evidence.py tests/test_player_context.py tests/test_context_data_repair.py -q
```

Tests use fictional evidence. They check numeric aggregation, temporal scope,
missingness, citation fallback, output schemas, artifact integrity, and rendering.
For example, two fictional games with 1/1 and 0/9 shooting must produce 10% FG.
These checks require no provider credentials or paid inference.

## Frozen inputs

Use a Python environment with repository dependencies. Capture warehouse evidence
with configured read-only BigQuery access, substituting your own project:

```bash
python scripts/capture_semantic_snapshot.py --project YOUR_PROJECT \
  --seasons 2025-26 --maximum-bytes-billed 100000000 \
  --output reports/evaluation-inputs/snapshot.json
```

The reporting runner takes that snapshot, a corpus JSON array, and a cases JSON
array. Build your own cases against the captured player identities. Public
fictional shapes are available in
[`tests/test_reporting_evidence.py`](../tests/test_reporting_evidence.py).

| Input | Required contents |
| --- | --- |
| Corpus record | Unique `R`-number `id`; `title`, `publisher`, `author`, HTTPS `url`; numeric `player_ids`; `event_start`, `event_end`, `version_available_date`; `summary`, `content_kind`, `content_sha256`, `retrieved_at`, `status` |
| Case | `id`, `player_id`, `player_name`, `question`, `category`, `expectations`, `seasons`, `phase`, `start`, `end`, `previous_start`, `previous_end`, `reporting_mode`, `reporting_cutoff`; optional `teammate_id` |

Corpus summaries are attributed curator-written paraphrases of at most 100 words,
not publisher quotations. `content_kind` is `curated_paraphrase`; `status` is
`available` or `withdrawn`. Compute the summary checksum using
`app.agent.reporting_evidence.digest(summary)`. Cases require exactly ten distinct
IDs, matching snapshot identities, and ordered non-overlapping comparison windows.

`published_by_cutoff` filters on the displayed publication/update date;
`retrospective` permits later reporting about the selected event window.
Neither reconstructs an archived article version. Preserve reporting provenance
and source-access terms when creating a corpus.

## Prepare, inspect, generate, review

Choose a new run directory for each attempt. The first two steps make no model calls:

```bash
python scripts/evaluate_reporting_evidence.py prepare \
  --snapshot reports/evaluation-inputs/snapshot.json \
  --corpus reports/evaluation-inputs/corpus.json \
  --cases reports/evaluation-inputs/cases.json \
  --run-dir reports/evaluation-run
python scripts/evaluate_reporting_evidence.py preview --run-dir reports/evaluation-run
```

Inspect `preview.html` and `generation-preview.txt` before generation. Missing
statistical components block generation. With authenticated Codex CLI access and
the configured generator/evaluator models available, these stages consume model usage:

```bash
python scripts/evaluate_reporting_evidence.py generate --run-dir reports/evaluation-run
python scripts/evaluate_reporting_evidence.py evaluate --run-dir reports/evaluation-run
python scripts/evaluate_reporting_evidence.py report --run-dir reports/evaluation-run
```

The generator is `gpt-5.6-luna`; the evaluator is `gpt-5.6-terra`. Stage schemas
constrain result count, IDs, and claim citation types. Local gates check order,
uniqueness, and citations. Statistical claims cite `S_*` evidence, reported
context cites `R*`, and interpretations can combine both. Structural validation
is not proof of factual support. Invalid claims cause the Ask-shaped payload to
fall back to its deterministic answer; the review retains the original model output.

The runner uses an isolated temporary directory, disables shell/web tools, and
records prompts, raw events, and usage. It does not automatically retry paid
calls or substitute models. Failed attempts retain their evidence; use a new run
directory rather than overwriting outputs.

`review.html` shows answers, evidence, advisory assessments, and human decisions.
`step-review.html` audits the pipeline and token accounting. Browser notes can be
exported; response and step reviews have separate storage keys. Serve these files
on loopback for consistent browser storage. They are local review artifacts.

Input plus output tokens form the measured total; cached and reasoning token
details are not added twice. Batch calls do not provide measured per-answer
allocation. CLI usage is not an API invoice. Deterministic stages use zero LLM
tokens but still incur compute costs. Small curated evaluations do not establish
retrieval recall, calibrated judge accuracy, or production readiness.

## Optional context and repair

With a frozen injury snapshot containing a `rows` array, execute the context SQL
locally and pass the resulting directory to preparation:

```bash
python scripts/build_player_context.py \
  --snapshot reports/evaluation-inputs/snapshot.json \
  --injury-snapshot reports/evaluation-inputs/injuries.json \
  --output-dir reports/evaluation-context
```

Add `--context-dir reports/evaluation-context` to the prepare command. Database
and audit hashes must match the statistics snapshot. SQLite execution and dbt
parsing do not replace BigQuery execution checks. See [Player context](pages.md#player-context).

For a completed 2025–26 season snapshot needing repair, the repair tool reconciles
player logs against separate official team logs and preserves an audit ledger:

```bash
python scripts/repair_context_snapshot.py --capture \
  --source-dir reports/repair-sources \
  --original reports/evaluation-inputs/snapshot.json \
  --output-dir reports/repaired-snapshot
```

`--capture` reads the official NBA endpoints; omit it to validate saved responses.
This command does not write warehouse tables or call a model. It requires complete
season inputs. `scripts/capture_context_injuries.py --help` describes the optional
selected-date report capture; those samples do not establish full intraday coverage.

Other semantic evaluation entry points are `evaluate_historical_semantics.py`,
`evaluate_semantic_language.py`, and `evaluate_semantic_injuries.py` under `scripts/`.
Use each command's `--help` for inputs and outputs. Historical reference freezing,
live warehouse capture, and model-backed language evaluation are explicit operations;
keep their datasets, reference answers, and detailed results local.

## Dynamic availability attribution

`tests/test_availability.py` generates questions for an unregistered synthetic
pair and independently checks known means, pooled percentages, source game IDs,
reversed roles/aliases, scope modifiers, missing data and JSON/SSE/follow-up parity.
Unsupported conditions must produce no statistics, including when the ordinary
semantic planner is called directly.

For a live local server configured with the same private availability bundle:

```sh
python scripts/evaluate_availability.py \
  --evidence reports/availability/unique-run.json \
  --url http://127.0.0.1:8017 \
  --output reports/evaluation/unique-availability-run
```

The runner samples new pairs from source team appearances with a fixed seed,
generates metric/filter variants, and retains every response. An independent
oracle joins raw box scores, schedule and latest pregame reports, recalculates
means/ratios and verifies requested roles, scope, group game IDs, valid/observed
counts, tables, narrative statistics and chart values. Missing verified samples
must return no substitute statistics. It respects the normal per-minute request
limit and makes no model calls. This proves attribution against the frozen inputs,
not completeness of upstream reports or human-reviewed basketball interpretation.

Availability evaluations must follow `availability/2`: independently classify
final participation before applying prior-only minutes qualification, reconcile
included and excluded game IDs, and verify unavailable values for empty groups.
`tests/test_availability.py` covers played/Out conflicts, confirmed DNP versus
unknown absence, both-player minute filters, the exact threshold boundary,
insufficient history, no future leakage, and filter context persistence.
The live `scripts/evaluate_availability.py` oracle uses the same frozen source
bundle with an independent implementation; it does not establish upstream NBA
source completeness. Keep downloaded repair sources and failed attempts private.

Review regressions also cover informational “find out” routing, minimum-games
rankings, research follow-up routing, prior-season appearance counts, explicit
versus default phase coverage, and classification-specific provenance. Browser
tests cover returning from history after a hidden completion and preventing
empty drafts from evicting answered chats. Model PR reviews remain advisory;
confirm findings against source and regressions before implementing them.

## Split routing and completed-season warehouse repair

Synthetic regressions cover phase words versus identities, pooled ratios,
disjoint appearance windows, inclusive event boundaries, missing venue/components,
unsupported modifiers, source-free capability clarification, follow-ups, and
JSON/SSE parity:

```sh
python -m pytest tests/test_player_splits.py tests/test_game_log_repair_stage.py \
  tests/test_repair_game_schedule.py tests/test_repair_promotion.py -q
```

`scripts/stage_game_log_repair.py` rebuilds a completed 2025–26 repair in unique,
expiring BigQuery datasets. This is a warehouse-writing operator tool, separate
from read-only Ask. It validates official player/team logs and the official full
schedule, preserves raw sources and prior attempts, backs up existing tables,
rebuilds dbt dependencies, checks exact repaired fact equality, and refreshes
similarity/archetype outputs together. Neutral-site designations are reconciled
against schedule teams/dates/scores, not guessed from two away labels. Missing
profile attributes stay null while identities observed in facts are retained.
Similarity validation handles nullable warehouse measurements and checks compact
archetype labels against the matching player’s feature measurements.

For completed-season correction checks, `--retain-schedule` preserves verified
warehouse assignments when the current NBA schedule has moved to a newer
season. Missing or conflicting date/opponent/venue mappings block publication.
`--require-original-baseline` additionally verifies the input comparison
snapshot against the backed-up gold fact before rebuilding. The
[daily maintenance command](operations.md#daily-player-comparison-updates) uses both options and
performs no warehouse writes when its validated inputs are unchanged.

```sh
python scripts/stage_game_log_repair.py --project YOUR_PROJECT \
  --source-dir reports/repair-sources --original reports/inputs/snapshot.json \
  --schedule reports/repair-sources/schedule.json \
  --run-dir reports/repair-stage-UNIQUE --dbt /path/to/compatible/dbt
```

Inspect the complete dbt run results, source/version manifests, changed-value
ledger, schedule reconciliation and warnings. Fatal checks or skipped dependencies
block validation; configured warnings retain their severity and full results.
`--resume-from` can retry a failed preparing baseline into a new local run directory
only while its live sources remain unchanged. Do not rerun a validated or promoted
candidate as a failed attempt.

The separate `scripts/promote_game_log_repair.py` requires a validated manifest,
its exact SHA-256 and a new receipt path. It rejects changed source/candidate
versions, incompatible schemas and missing dependencies. Nullable schema additions
precede the transaction; all row replacements occur in one transaction with
multiset baseline assertions and a 3 GB script billing cap (including per-statement
minimums). Before-tables remain in the isolated datasets for
seven days by default. Preserve needed backups before expiration. Existing views
and historical analysis snapshots are not automatically republished.

```sh
python scripts/promote_game_log_repair.py --manifest reports/repair-stage-UNIQUE/validated.json \
  --expected-sha256 REVIEWED_MANIFEST_SHA256 --receipt reports/repair-receipt-UNIQUE.json
```

Test deliberate promotion failures only on disposable tables. A successful staged
build does not establish live promotion; the receipt and independent post-promotion
checks do. No paid language-model evaluation is part of this workflow.

Validated repair manifests now bind each candidate's schema and complete row
multiset with SHA-256. Promotion asserts those digests inside the same transaction
as the inserts, before any live row mutation; concurrent candidate writes cannot
change the transaction's read snapshot. Metadata versions are also rechecked
before schema additions. Older manifests without content evidence must be
restaged. These additional reads remain subject to the script billing cap.
The concurrency guarantee relies on BigQuery's
[transaction snapshot isolation](https://docs.cloud.google.com/bigquery/docs/transactions).

## Ask endpoint regression and human review

The Ask harness stores draft scenarios and linked paraphrases in
`tests/fixtures/ask/cases.json`, with fictional statistics in
`tests/fixtures/ask/source.json`. Generate current counts and assertion coverage
with `python -m scripts.ask_eval.coverage`. The schema validates
case shape, IDs, structured assertions and review provenance. Expectations cover
identity, phase/date scope, numeric tolerances, missingness, rendered values,
refusals and multi-turn behavior. Award cases use the existing reviewed catalog.
Availability/study cases include both missing-source refusals and successful
frozen-source answers; the separate availability suite provides additional checks.

Run in the app Python environment, using a **new** output directory each time:

```sh
python scripts/evaluate_ask.py --output reports/ask-harness/candidate-01
python scripts/evaluate_ask.py --code-root /path/to/baseline-checkout \
  --label baseline --output reports/ask-harness/baseline-01
python -m scripts.ask_eval.review \
  --baseline reports/ask-harness/baseline-01/results.json \
  --candidate reports/ask-harness/candidate-01/results.json \
  --output reports/ask-harness/review-01.html
```

Both code revisions run through real loopback JSON/SSE endpoints, using the same
fixture adapter, source, cases and grader. The baseline checkout supplies app
code; the current evaluation runner supplies controlled model responses. Cases
with legacy narratives use an explicit placeholder, so missing evidence contracts
must not be presented as a measured increase in real-model factual accuracy.
The comparator rejects mismatched inputs. Reports retain code/content hashes,
provider/model, per-turn payloads and checks, timing, parity, and failed attempts.
Progress is flushed per case; failed runs are never overwritten.

Controlled runs measure endpoint contract behavior, not natural-language planning
accuracy. CI runs these checks without credentials. Real-provider evaluations are
separate, explicit operations against the same frozen sources:

```sh
python scripts/evaluate_ask.py --live --provider openai --model APPROVED_MODEL_ID \
  --case points_mean --case follow_metric --max-model-calls 12 \
  --output reports/ask-harness/provider-01
```

Select an endpoint-allowlisted model and configured provider credentials. Live
runs require a model, explicit cases (or a private input directory) and a bounded
model-call budget. Calls are
counted across both transports, SDK retries are disabled in the evaluation
adapter, and the runner does not retry runs or substitute models. Controlled
invalid-plan injection cases cannot be submitted as real-model cases. Live parity
means that both transports satisfy the same structured expectations; it does not
require identical prose. These runs have not been implied by offline CI success.

The self-contained HTML shows questions, expected/baseline/candidate values,
answers, tables, complete responses, source inputs, hashes and limitations. It
supports filtering, review decisions and a JSON export. Decisions are stored
in browser storage under the source/case/candidate hashes. Serve locally for stable
storage, and retain the exported file privately. Saving requires an explicit
approval/revision decision; reviewer name and notes are optional. Each case lists
what needs human judgment: question/identity/scope alignment, the expected
calculation or justified refusal, and evidence-grounded wording. Follow-ups,
league populations, and published similarity scores have specific guidance.
Approval covers both the expectation and candidate answer; the old baseline is
comparison context, not an additional answer to approve. Feedback beside the Save decision
button confirms local persistence or warns that browser storage failed; in that
case, export before closing to preserve the in-memory decisions. Pending review
is not a completed decision. Reviewer decisions do not change
automated scores. Cases remain pending until a human approves them; passing the
suite does not make it a human-reviewed benchmark.

Real-browser smoke checks use the same local fixture server and actual responses:

```sh
npm ci
npx playwright install chromium
npm run test:ask-browser
```

They check populated baseline values/charts, follow-up scope after reload,
clarification, narrow-screen similarity tables, recent comparisons, verified
nonparticipation, methodology navigation, and human-review save/export behavior. The test server uses the local
`.venv-app` when present, otherwise `python`. Generated artifacts stay in ignored
`reports/`. Test infrastructure is separate from the public application; never
serve the fixture server remotely or deploy it as the app entry point.

### Production snapshot comparisons

The endpoint runner also accepts `--input-dir PRIVATE_DIR`, containing a
schema-valid `cases.json` and `source.json` with `source_kind: production_snapshot`,
`season`, a checksum-validated semantic `snapshot`, and captured repository
`details` keyed by player ID. Facts and profiles must be frozen before either code
revision runs. Record separate profile capture timestamps; they are not an atomic
cross-table snapshot. Use independent arithmetic to author expected values and
retain the oracle builder with the private capture. Similarity scores from a
published profile test faithful serving, not the quality of the similarity model.

```sh
python scripts/evaluate_ask.py --input-dir reports/PRIVATE_CAPTURE \
  --live --provider openai --model CONFIGURED_MODEL --max-model-calls 80 \
  --output reports/PRIVATE_CAPTURE/candidate-live
python -m scripts.ask_eval.review \
  --source reports/PRIVATE_CAPTURE/source.json \
  --baseline reports/PRIVATE_CAPTURE/baseline-live/results.json \
  --candidate reports/PRIVATE_CAPTURE/candidate-live/results.json \
  --output reports/PRIVATE_CAPTURE/review.html
```

Use `--code-root` for the archived baseline. The live evaluation server loads the
working checkout's `.env` for both code revisions without copying credentials to
the archive or artifacts; already-exported environment variables take precedence.
Uncaptured repository reads fail explicitly; there is no live warehouse fallback.
Runs retain per-call usage, raw model responses, exception types and timings in
private `model-calls.jsonl`, plus per-turn usage in the scored report. No provider
keys or production captures belong in CI or Git. The call cap bounds attempts,
not dollar cost; token usage is not an invoice. A small paired run and draft human
labels do not establish broad accuracy or deployed-service health.


### Interpreting results and review limits

A case passes only when every structured assertion and both endpoint transports
pass. Missing evidence fields fail this contract even when the old narrative is
correct. Compare refusals separately from numeric answers. New league baselines
pool appearances within the requested calendar scope; the older dashboard baseline
uses a different population, so a difference is not automatically hallucination.
Uncaptured replay reads must be called out as evaluation limitations, not deployed
service defects. Use `--notes PRIVATE_NOTES.json` when generating the review to
attach a `summary` string and a `cases` mapping from case ID to interpretation.
These notes do not change the immutable raw scores or mark cases human-approved.

The local 2026-10-09 review used 61 synthetic scenarios (48 baseline / 61 updated
passes), followed by 18 production-snapshot scenarios for two players with
`gpt-5.4-mini` (9 baseline / 18 updated passes; 48 / 16 model calls). The production
run included one baseline request blocked by an uncaptured rankings read. These
are small contract-conformance runs, not broad accuracy estimates, latency
benchmarks, or tests of the deployed service. Per-case human decisions remain
pending. Detailed cases, captures, model responses and HTML remain private under
`reports/`; do not attach them to the public PR. Later code review fixes use
focused offline regressions rather than silently rerunning paid experiments.


PR review regressions additionally cover pending-reference replacement, participation
routing ahead of incidental similarity/baseline wording, appearance windows ending
on the focal player's last game, bounded membership payloads and stable hashes,
reference error wrapping, and zero SDK retries in both Claude/OpenRouter adapters.
The production snapshot run above used the earlier `league_baseline/1` contract;
its saved artifacts are immutable and are not relabeled as a live test of version 2.
No new paid run is implied by these offline fixes.


The next revision incorporates human feedback with seven additional synthetic
scenarios and a revised recent-period expectation. New numerical expectations
remain pending review. Prior production captures, scored runs and exported human
decisions are retained unchanged; a new offline replay can apply revised
expectations to the same frozen facts without paid calls or warehouse access.


### Expanded offline Ask coverage

Generate current corpus counts and assertion coverage with
`python -m scripts.ask_eval.coverage`. The result separates source rows, cases,
distinct scenarios, paraphrases and frozen expectation review states. See the
[frozen quality rubric](ask-harness-quality.md).
Reports show both denominators separately, plus per-family scores. Paraphrases
inherit the parent oracle and do not increase the distinct-scenario count.
The expanded cases include conversation transitions/recovery, successful
availability comparisons, research/study answers and statistical boundaries.
Existing refusal scenarios remain in the corpus.

Synthetic availability attachments distinguish positive participation, pregame
Out plus no appearance, verified zero-minute nonparticipation, and unknown
absence. Research cases verify filtered means, pooled percentages, totals,
missing components, exact phase/dates, and a fictional frozen published estimate.
The study estimates are predeclared contract fixtures, not a fitted or validated
statistical study. Expected arithmetic and membership are documented beside each
case and independently checked in `tests/test_ask_coverage.py`.

The fixture server materializes attachments in a process-owned temporary folder
and activates them only for cases that request them. No credentials, warehouse
queries or paid calls are required. Controlled research/semantic plans establish
adapter and evidence contracts; even paraphrase passes do **not** establish real
model interpretation accuracy. New expectations remain pending human review.

`evaluate_ask.py` permits up to 400 HTTP requests by default offline; live runs
retain the 160-request default and require explicit model and call budgets.
An explicit nonpositive `--max-requests` is rejected. Every turn is run through
both actual JSON and SSE endpoints, with separate conversation IDs. Recovery
cases explicitly evict server state and resend bounded browser context.

Use the same new fixture adapter, corpus, source and grader when comparing a
baseline application checkout with candidate code:

```bash
python scripts/evaluate_ask.py --code-root /path/to/baseline --label baseline \
  --output reports/ask-harness/unique-baseline
python scripts/evaluate_ask.py --label candidate \
  --output reports/ask-harness/unique-candidate
python -m scripts.ask_eval.review \
  --baseline reports/ask-harness/unique-baseline/results.json \
  --candidate reports/ask-harness/unique-candidate/results.json \
  --output reports/ask-harness/unique-comparison.html
```

The populated browser suite covers availability group values and metric changes,
research identity overrides, published-study uncertainty, and restored baseline
context. Mutation tests reject wrong identity, scope, values, membership counts,
and replacing unavailable values with zero or missing fields. These are offline
endpoint/browser results; CI and deployment remain separate validation gates.

### Baseline attachment capabilities

Frozen attachments are configured lazily per case. An older application's
settings need no availability/study fields or attachment digest modules to run
attachment-free cases. Only the explicitly requested attachment is materialized;
study-only cases do not import availability dependencies, and attachment paths do
not carry into later cases.

When a case requests an attachment unsupported by the selected application
revision, the evaluation server returns HTTP 501 with
`evaluation_fixture_capability_unavailable` and an explicit attachment/reason.
That case fails rather than passing, being skipped, or using another data source.
The runner retains this JSON error for both JSON and SSE requests, records the
HTTP failure, and continues evaluating unaffected cases. Runtime metadata lists
these capability failures separately from ordinary contract mismatches. A missing
frozen attachment also fails closed; it never enables live warehouse fallback.

## Ask route evaluation

`tests/fixtures/semantics/route_cases.json` labels the intended Ask route for
known misroutes, controls, follow-up chains, pending-clarification replies,
reversed study roles and research refusal classes. Each case has three phrasings,
an `expected` map (dotted paths; a list means any of), `forbidden` routes and a
`category` (`route`, `scope_drop`, `followup`, `refusal`, `control`). Generic
cases use the fictional players in `tests/fixtures/ask/source.json`; study-pair
cases use the public pair identities in `app/research_studies.py`. `opponent_splits`
is a planned route with no handler yet, valid only as an expectation.

```bash
python scripts/evaluate_ask_routing.py --mode current
python scripts/evaluate_ask_routing.py --mode ask
```

`current` replays the dispatch registry plus each handler's deterministic
pre-planner checks with zero model calls. `ask` runs `StatsAgent.answer` on the
frozen Ask fixture warehouse and reads the payload `ask_route`; its default
controlled model returns planner refusals, so only route decisions are measured
and outcomes of model-calling turns are `unknown`. `--provider openai` runs a live
planner locally with a bounded `--max-calls` budget; provider keys are never added
to CI. Reports default to `reports/ask-routing/<date>/<mode>.json` with JSONL
progress beside them, and are never overwritten.

Each report gives route accuracy per category, a confusion matrix, **scope
drops** (a forbidden route whose outcome is an answer or unknown), forbidden-route
hits, wrong-path refusals (a refusal or clarification from the wrong route) and
misroutes with unknown outcomes. A `research` label matches an expected
`research_breakdown` or `teammate_study` and is counted separately. `--fail-under`
and `--max-scope-drops` turn the summary into a gate. The frozen fixture has no v3
study catalog, so `ask` mode reads every study as coverage pending. Fixture
results are not live-warehouse or browser evidence.

## Offline execution-policy QA

Run the real OpenAI Responses interface and Claude/OpenRouter adapters over
synthetic SDK transports (no provider keys, network, or warehouse required):

```bash
ASK_EXECUTION_REPORT=reports/ask-execution/unique-run/qa.html \
  python -m pytest tests/test_agent_execution.py -q
```

The matrix exercises valid and malformed planning, 429/503 recovery, exhausted
provider timeouts, non-retryable errors, the request deadline, and Claude partial
stream failure. It asserts physical attempt counts, SDK retry suppression,
timeout propagation and stage timings. Additional tests exercise real JSON/SSE
endpoints, one terminal event, safe traces, and no successful conversation/history
save on deadline failure. Deterministic routes assert zero model calls.

The optional local HTML/JSON report contains synthetic outcomes and **virtual
clock** timings; it is not a paid model-quality evaluation or a measurement of
production/provider latency. Choose a fresh output path for each attempt so
failed QA artifacts remain available. Compare real latency only in a separately
authorized, controlled run. The endpoint coverage harness remains complementary:
its controlled planner cases do not establish resilience to provider faults.

The live question smoke runner records execution-policy failures (including
provider and request timeouts) per case and continues to later cases. Reports
retain the safe failure code without provider exception bodies.

## Offline delivery measurements

Measure actual loopback SSE frames from the public frozen source and controlled
model, without provider or warehouse calls:

```bash
python scripts/measure_ask_delivery.py \
  --output reports/ask-capacity/unique-delivery-measurement
```

The command refuses an existing output directory, fixes the public input paths,
disables live execution, and records only case IDs and frame-size metadata in
`captures.json` and `summary.json`. The server log remains a local run artifact.
Run the ordinary endpoint evaluation separately for factual contract assertions.

The pre-hardening capture on October 12, 2026 measured 124 cases / 145 SSE turns:

| Delivery measurement | Observed value |
| --- | ---: |
| Maximum complete incremental output | 1,650 UTF-8 wire bytes |
| Maximum incremental event count | 9 |
| Largest incremental frame | 200 bytes |
| Median / 90th / 95th / 99th percentile incremental bytes | 368 / 1,592 / 1,650 / 1,650 |
| Maximum final frame | 21,007 bytes |
| Maximum interim Python string storage | 2,091 bytes |
| Equivalent interim Python bytes storage | 1,947 bytes |
| Deque container at 9 entries, in that interpreter | 760 bytes |

The default 1,650-byte / 9-event mailbox uses those observed regression maxima.
It omits further interim updates when full, with a separate terminal slot for
the full final/error event. The final frame is excluded from both quotas. The
implementation retains bytes frames. Heap measurements are interpreter-specific
string/bytes/container sizes; they exclude the worker, evidence, terminal payload,
lock, and current serialization/send buffer.
These measurements establish neither total memory bounds nor production load,
provider output lengths, provider latency, or optimal concurrency.

Admission retains a slot until both worker completion and JSON/SSE response-send
closure, counting workers and unsent complete terminal payloads together. A
stalled body send can retain capacity past the computational deadline; there is
no separately measured or configured transport timeout in this change. Global
capacity rejection is HTTP 503; a competing same-season/conversation request is
HTTP 409. Test the worker-first and delivery-first completion orders, stalled
sends, and send failures rather than treating worker completion alone as release.
Delivery trace metrics are captured at worker finalization, so later detachment
is not included. Raw JSON socket disconnect detection may be unavailable.

The command counts emitted frames rather than sampling peak retained backlog.
Once overflow suppression is active, rerunning against the bounded application
may capture fewer interim frames; use the pre-hardening capture to assess the
original default rationale. Exercise quota boundaries, oversized frames, final
delivery, detach/completion, JSON/SSE overload, and successful-only persistence
with endpoint tests and populated browser QA. Paid evaluation and deployment
remain separately authorized work.

Persistence checks must cover staged pending-clarification set/clear operations,
same-request reads of that overlay, retention of prior state on failure, and the
last budget checkpoint before committing staged in-memory updates. The local
history append remains a separate write; passing these checks does not establish
atomicity between conversation state and history file I/O.

Capacity regressions include blocked JSON error-body sends, ASGI 2.3 disconnects
while a send is blocked, bursty synthetic Claude-shaped deltas with
`answer_streamed=True`, trace capacity snapshots and start failures, and the
409/503 request-rate quota policy. Synthetic streaming faults make no provider
calls and do not establish live Claude throughput or latency. The populated
browser suite verifies 409 conversation-busy recovery with explicit retry.

Restore-time validation errors are checked at the actual endpoints: JSON HTTP
400, SSE HTTP 200 with one terminal `error` event, safe `HTTP_400` trace codes,
request IDs, and capacity release. Recovery from already-saved context precedes
governed execution; tests distinguish those retained recovery side effects from
new failed-answer persistence. Successful staged agent mutations and separate
history I/O are still not a cross-storage transaction.

## Overview presentation regressions

`tests/test_overview_insights.py` checks volume versus efficiency, per-minute
versus per-game changes, strong peer standing alongside personal decline,
qualification exclusions, null semantics, selective coverage and input order.
Editorial regressions also cover absent-baseline headings, opposing assist rates,
small TS changes, exact versus rounded leader ranks, separate cohort qualifications,
stable elite activity and explanatory sidebar bodies. Word-count and phrase checks
are targeted contracts, not independent ratings of prose quality.
`tests/test_overview_tracking.py` checks private codes, deduplication, exclusions,
separate demand/gap counts and absence of raw text. JSON/SSE tests verify these
contracts at the endpoints, including private gap logging outside responses.
Routing checks preserve focused, mixed-metric and unsupported requests.

The `balanced_*` harness cases use fictional rows with independently specified
scope/value and insight-selection expectations. Desktop/mobile tests cover prose,
chart selection and saved-text preservation. A separate private captured-warehouse
run can compare revisions using identical rows and independently recomputed peer
cohorts. Such a run validates local endpoints over a captured source; it does not
establish deployed behavior or paid model accuracy.

Overview presentation regressions in `tests/test_performance_overview.py` cover
explicit general/focused metric selection, preserved scope guards, pooled ratios,
zero attempts, partial samples, short samples, and absence of best-percentile
headline selection. Browser tests verify primary row counts, sample notes,
expandable details, saved-state recovery and narrow scoring results. These checks
use frozen observations and controlled models, not live-model accuracy tests.


`tests/test_ask_review_scope.py` verifies inferred scalar metric dates at both
actual JSON and SSE endpoints using independently expected synthetic values.
Calendar-year/rolling-year cases combine two season archives; historical months
use the proper archive; missing archives and conflicting named seasons withhold
results. Controlled planner cases preserve month comparisons and since-month
ranges. Time-default tests cover explicit-season month inference, multi-season
follow-ups and within-season as-of anchors. Browser/JavaScript checks retain
compact table dimensions while verifying announced distribution ranks/statuses.

Follow-up regressions preserve a historical primary season even when comparison
archives appear in saved context, resolve off-season recent-game requests from
preceding available appearances, and clip a planner's current-month end to the
request date. The current-month fixture includes a later 100-point game to prove
that clipping excludes future appearances from the mean.

### Position Overview evaluation

`tests/test_overview_positions.py` uses fictional players to check historical
provenance, hybrids, coverage, cohort thresholds, conventional ties, complete
components, reference selection, exact first-two-appearance scope and private
tracking. `test_overview_insights.py` checks main/sidebar rate contradictions.
Browser verification must exercise labeled peer details and matching distributions
as well as the unchanged league table. Evaluate real questions separately with
frozen source captures; a valid league fallback is not evidence that historical
position cohorts exist. Keep before/after answers and failed attempts private.


### Overview variation and requested-peer fulfillment

Overview v4 separates HTTP delivery, arithmetic correctness, and fulfillment of
an explicit position comparison. An unavailable requested cohort is partial,
never an answered league substitute. Regression coverage includes metric-led
openers, same-cohort per-36 context, independent quartiles, tied dated highs,
missing games, tiny samples, and peaks outside the chart point cap. Re-run
`tests/test_overview_variation.py`, distribution and Overview suites, plus the
JavaScript suite and populated mobile/desktop charts after rendering changes.
Frozen-source runs and synthetic position-group tests establish different things;
real position comparisons require separately verified season membership.


### Schema-v1 review provenance limitation

The coverage inventory reports declared review states without verifying reviewer
identity or approval binding. Schema v1 intentionally permits an omitted reviewer
and does not bind approval to a hash of the expectations. Even an `approved`
declaration therefore does not establish human-reviewed rubric credit. Browser
review decisions are separate records. A future versioned provenance contract may
strengthen this without silently rejecting existing v1 corpora.

Critical risks list the selected case, one-based turn, expected assertion path and
value, maximum tolerance, and missing assertions. Presence means the required
assertion exists; actual endpoint passes and human approval remain separate.

## Strict recorded-provider replay

The endpoint runner also supports recording actual provider calls against frozen
inputs and replaying them offline. Controlled planning remains the default.
Recording consumes provider usage and requires explicit cases, model, attempted
call budget, and an ISO semantic clock date. Use a fresh ignored directory:

```bash
python scripts/evaluate_ask.py --live --provider openai --model gpt-5.4-mini \
  --case points_mean --max-model-calls 4 --clock 2026-10-18 \
  --record-cassettes reports/ask-harness/my-capture/cassettes \
  --output reports/ask-harness/my-capture/results
python scripts/evaluate_ask.py \
  --replay-cassettes reports/ask-harness/my-capture/cassettes \
  --output reports/ask-harness/my-offline-replay
```

Replay resolves provider, model, selected cases and clock from the manifest;
explicit conflicting arguments fail before server startup. It creates no provider
client and spends no provider calls. The fixture server blocks Python-audited
outbound socket/DNS operations and process spawns, including local proxies, and
runs the asyncio event loop. This is a Python-level guard, not OS isolation of
native extensions. The runner is a separate process connecting inbound to loopback.
Frozen repositories reject uncaptured reads and retain sticky failure evidence
independently of application fallback; no warehouse fallback is available.
Zero-call evidence combines blocked real client construction, consumed recordings,
and the Python guard, rather than relying on the unused live-call counter alone.

Version 2 recordings bind each independent case/turn/JSON-or-SSE call sequence to
its provider, model, complete semantic request hash, fixture settings, selected
case contract, source, schema and fixed date. Version 2 binds the manifest hash
in the terminal record and validates stored request/match hashes and record shape.
The sole supported v1 bundle is the original reviewed capture, pinned by raw-file
hashes and canonical cassette hash; arbitrary unverified v1 bundles are rejected.
Transport timeouts/retries are saved
separately; callbacks are replayed in their captured order. SDK output items, tool
IDs/arguments, usage and available finish/refusal fields at the app's adapted
Responses boundary are retained; this is not a complete provider wire capture.
Claude stop reasons are retained and truncation/refusal/pause stops fail the gate.
OpenRouter rejects incomplete/filtered/refused results and retains accepted finish
reasons. Unsupported values fail serialization. Failed attempts remain failures,
with safe error
classifications instead of private error messages. No provider retry policy is
changed in the production app.

Missing, out-of-order, mismatched, extra and unused calls fail the terminal harness
gate even when an optional application fallback produces a valid payload. Reports
retain this gate separately from case assertion scores, with cassette hashes and
attempt counts. Full corpus hashes remain comparability provenance; replay binds
the individual selected case contracts so an unrelated case edit does not invalidate
a recording. Application and adapter revisions are recorded as provenance; changed
application code may replay when its requests and contracts still match.

The clock seam affects semantic date defaults, scope checks, splits and overview
periods only. Execution deadlines, source capture timestamps and history clocks
remain real time. New recordings include elapsed time and available timeout/
remaining-deadline bounds. Bound overruns and execution failures during provider
calls make capture unsuccessful. ExecutionFailure codes remain distinguishable.
Replayed chunks establish callback ordering, not provider streaming latency,
cancellation, deadline effects or post-call scheduling. Replay does not freeze or
reproduce those execution clocks; changes after the captured boundary can still
change the application's timing-sensitive outcome. Each transport has its own recording;
parity means both satisfy the same independent expectations, not identical prose.

Keep raw captures private. Before copying synthetic recordings into
`tests/fixtures/ask/recordings`, inspect requests, response text, evidence, IDs,
paths, metadata and attachments for privacy. Record this agent privacy review
separately from human expectation approval; pending cases stay pending. A replay
pass establishes frozen response behavior, not current model quality, live
warehouse accuracy or deployment readiness. Never regenerate expectations or
repeat a paid capture automatically after a failure.

The reviewed public recording in
[`openai-mini-20261018`](../tests/fixtures/ask/recordings/openai-mini-20261018/README.md)
contains 14 calls across six cases. Its immutable capture and historical replay
passed 4/6; the prior bounded correction produced a historical 5/6 checkpoint.
Those are historical contract results. `research_intent/2` and
`semantic_intent/2` change the prompts and schemas. Current replay rejects the migrated case binding before request-hash
comparison; this is not evidence of executing the new contract against old calls. The
recording, matching rules, expectations and historical outcomes remain unchanged.
No paid recapture was performed for that historical rework. Controlled planner fixtures migrate input shape
only; unchanged expected checks do not transfer human approval of old whole-case
hashes to new ones. New contract endpoint tests are synthetic evidence, not a new
model accuracy score.

Settings binding changes require explicit compatibility review. New diagnostic or
storage-only settings should be excluded with tests when introduced; existing
generation, scope and execution policy settings must remain bound. Never rewrite
an existing capture's settings hash merely to make replay pass.

New private recording/report directories are created with mode 0700 and raw files
with mode 0600. Response terminal failure metadata also fails the harness gate,
even if application fallback produces a usable payload; missing optional status
fields on provider adapters are not themselves failures.

The evaluator loads only three provider credential keys from `.env` and suppresses
automatic dotenv loading while importing the app. It binds explicit fixture settings,
not every inherited process environment variable. Capture/replay middleware returns
structured 400 errors for invalid case/turn headers and preserves 501 capability
details; semantic clock context is reset even when request setup fails.

## Frozen teammate-study factual output

`tests/test_teammate_ask.py` sends valid-scope but factually false narration through
both public JSON/SSE handlers. Independent expectations reject swapped subjects,
metrics, dates, units, contrast direction, significance wording, table labels and
unexpected charts. Positive/zero/negative estimates and malformed imported
statistics are covered separately. `research_published_study` in the existing
controlled corpus asserts the exact deterministic answer, table, scope, all nine
claim records and a literal independently calculated content hash. Its imported
estimates are not derived from the co-located box-score or availability rows.
Each claim value, scope, unit, source/display path and hash is independently mutated
to confirm rejection. Its intentionally adversarial response is a controlled injection, not
recorded or live-provider evidence; review remains pending. Existing availability,
overview and reference tests cover their distinct evidence and rendering contracts.
These checks do not establish universal governance of injected compatibility
repositories; see [factual-output reachability](ask-workspace.md#factual-output-reachability).

### Private trace-to-case drafting

`python -m scripts.ask_eval.draft` creates a fresh ignored `reports/` bundle for
local review, with mode 0700 directories and 0600 files. It makes no provider or
warehouse calls. Supply `--trace`, `--source`, `--cases`, `--review`, and `--output`.
The source and cases use the existing frozen evaluator formats. Cases must contain
explicit checks and an independently authored `oracle_note`; the trace's observed
answer is never used to generate expectations.

The review JSON uses `schema: ask_failure_review/1`, `status: confirmed_failure`,
`reviewer_kind: agent` or `human`, a `reviewer`, a `failure_explanation`, and
`expectations_independent_of_observed_answer: true`. Bind it to the exact input
bytes with `trace_sha256`, `source_sha256`, and `cases_sha256`. This is an explicit
authorship attestation, not machine proof that the oracle is independent. Suspected
failures alone are rejected. `mode: private_reproduction` retains private evidence.
`mode: fictional_reconstruction` additionally requires `--original-source`, its
`original_source_sha256`, `separately_authored_fictional_inputs: true`, and a
`reconstruction_note`. Author the fictional source, questions, controlled plans,
and checks separately; text substitution is not a sufficient reconstruction.
An unchanged source copy is rejected in reconstruction mode; differing bytes or
content alone do not prove anonymization.

The bundle freezes the authored source, pending cases, and private provenance containing
the trace reference/hash and failure signature, application/config hashes, reviewed explanation and
reproduction limitations. Reconstruction retains only an original-source path/hash
reference, never a copy of the private original. It does not modify the trace or
overwrite a prior bundle. Provenance is written first and cases last; incomplete
writes are rejected and cleaned up when possible.
Missing controlled planner responses or attachments are listed as limitations;
they must be supplied in a new reviewed draft before claiming complete replay.
Trace projections alone cannot reconstruct warehouse rows or model responses.
Use `scripts/evaluate_ask.py --input-dir reports/YOUR_DRAFT --output reports/YOUR_RUN`
to run the existing JSON/SSE evaluator with frozen inputs and controlled plans.

All drafts force `human_reviewed: false` and case review `pending`, even when the
failure reviewer is human. No option approves, publishes, or copies a draft into
public fixtures. Provider recordings are not accepted as source metadata. Review
questions, source rows, generated outputs, provenance and attachments before any
separate public promotion. Credential/contact-pattern checks are limited safeguards,
not a comprehensive privacy review. Corrections require a fresh output directory;
keep all run artifacts and the original trace private.

Inputs are limited to 8 MiB per file. Symlinked path components are rejected,
including macOS `/tmp` and `/var` aliases; use their actual resolved paths. Existing
owned parent directories may retain 0755 permissions; new bundles remain 0700.
Ignored files may still be backed up or synced: Git-ignore is not an access or
cloud-sync boundary. Trace code hashes describe D's first-capture on-disk Python
fingerprint, not executed code or dependency versions. Config hashes cover only
D's allowlisted diagnostic settings. A differing current application fingerprint
is recorded as a reproduction limitation, never silently treated as replay proof.


The historical 5/6 recorded result is an in-sample regression repair against the same
capture that exposed the defect. It is not held-out generalization or live model
accuracy. Alternate fictional endpoint cases establish only the bounded scope
and refusal behavior they exercise.

The `research_intent/3` correction makes phase provenance server-authoritative;
model provenance labels remain advisory. Synthetic JSON/SSE controls exercise
correct values with misleading labels, contradictory values, and intent changes.
They do not establish current provider reliability. A later authorized private
v2 capture exposed the provenance-label refusals summarized in the public
[PR86 findings](https://github.com/mikeh-studio/nba-stats-desk/pull/86). Current v3 evidence
is controlled testing, pending a fresh versioned provider measurement. Historical recordings,
expected checks, source bytes and private measurement ledgers remain unchanged;
new contract evidence requires separately versioned evaluation.

The dated-points default regression uses independent JSON/SSE endpoint holdouts,
including zero and missing values, explicit-total contradictions and unmatched
conditions. The original recording is incompatible with the new planner contract;
controlled endpoint tests do not establish live-provider or warehouse accuracy.


The `semantic_intent/2` controlled input migration changes planner responses only;
case expectations and source bytes stay unchanged. That v2 contract required
model provenance labels; ideal-plan controlled clients did not establish live
provider reliability. Subsequent private live-provider measurement exposed correct
plans refused for labels and wrong dated totals escaping alternate date wording.
The v3 correction makes provenance server-owned and reuses canonical interval
validation; advisory labels no longer govern execution. New whole-case hashes
are not human-approved merely because their checks were unchanged. JSON/SSE dated
points controls cover zero/null values, filters, explicit-total contradictions and
compatible follow-up inheritance. Separate direct planner/executor checks verify
that independent 2/4/6 inputs produce mean 4 and sum 12. These checks establish
controlled endpoint and calculation behavior, respectively, not fresh provider
accuracy or historical cassette compatibility.


`semantic_intent/3` endpoint controls use independently authored public synthetic
questions with intentionally inaccurate or missing model labels and count values.
They cover canonical and alternate ISO intervals, explicit and inherited value
contradictions, zero/null samples and invalid scopes. The existing 147-case source
and expected checks and historical recordings remain unchanged. Controlled passes
establish those endpoint behaviors only. Fresh live-provider measurement for v3
is pending; the private v2 failures are regression evidence, not new heldouts.

## Exact binding regressions

`python -m pytest tests/test_claim_bindings.py -q` checks literal independent
values, equal-valued identity/source swaps, metric/unit/date/phase/hash/format
mutations, chart points, and paired actual JSON/SSE log/enforce behavior: invalid
bindings remain visible in log and are withheld in enforce, while older refusals
remain enforced in both. Tests assert emitted assessment counts and canonical
route/mode attribution, not just reconstructed events. A bounded
100-player, two-metric fixture checks evidence hashing once per stage. These
checks establish the mapped surfaces in the [inventory](ask-claim-bindings.md),
not universal prose accuracy, human approval, live warehouse or deployment health.

## Agent serving preparation

See the [Phase 1 serving contract](ask-serving-contract.md) for the offline metric
inventory, publication validation, telemetry, and parity checks. Production Ask
retrieval remains unchanged; serving routing and live performance measurement are
subsequent phases.

## Shared player-name matching regressions

`tests/test_name_matching_routes.py` exercises JSON and SSE endpoints with
synthetic source evidence across appearances, splits, similarity, league
baselines, planned summaries/game logs/ranks/percentiles, research, and study
identity handling. Corrected queries must return the same statistics and scope
as canonical-name controls. Partial names clarify; unrelated names return
`not_found`, without tables or a planning call. Selection and typed replies
resume the original filters, duplicate names retain distinct IDs, and team/metric
words must not become player candidates. Overview/comparison controls remain in
`tests/test_player_suggestions.py`. These checks establish fixture behavior;
local live-source probes and browser checks are reported separately.
