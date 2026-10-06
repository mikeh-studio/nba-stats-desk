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
parsing do not replace BigQuery execution checks. See [Player context](player-context.md).

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
