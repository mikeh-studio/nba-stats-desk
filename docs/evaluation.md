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
