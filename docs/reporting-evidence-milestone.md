# Reporting evidence: local evaluation milestone

This milestone combines the existing governed player overview with a small,
curated reporting corpus. It produces ten responses through Codex CLI using
`gpt-5.6-luna`, evaluates those original responses with `gpt-5.6-terra`, and
renders a local human-review packet. It does not enable a new public Ask route
or make model-generated narrative part of the deployed service.

## What is implemented

`app/agent/reporting_evidence.py` prepares evidence through the same
`build_overview` function used by Ask. Statistics remain deterministic. Reporting
is filtered by numeric player identity, event window, withdrawal status, and
the latest displayed publication/update date, then ranked by keyword overlap.
Exact duplicate summaries are removed. Conflicting reports remain separate.

The generator receives compact metrics, both comparison windows, appearance
counts, observed teams, and eligible reporting summaries. Evaluation expectations
are withheld from the generator. Each generated claim has a kind and evidence
IDs: `S_*` for statistics and `R*` for reporting. Citation validation checks IDs
and evidence types; **it does not establish semantic support or numerical
accuracy**. Terra evaluates those dimensions separately. Human review is final.

`attach_reporting` emits an Ask-shaped payload and preserves all governed
tables, charts, identity, and semantic evidence. Invalid citation structures
fall back to the deterministic answer. The review report deliberately shows
the original generated response, including failures, so model quality is not
hidden by the fallback. This adapter is ready for a later, explicitly enabled
Ask integration after review; Codex CLI is used only in the local evaluation
runner, never in a public request handler.

## Inputs

Keep real warehouse snapshots, the curated corpus, private evaluation cases,
prompts, model outputs, and human review under ignored `reports/ask-evidence/`.
Public tests use fictional contract fixtures. No article bodies are collected.

Corpus JSON is an array with these required fields per source:

| Field | Contract |
| --- | --- |
| `id` | Unique `R` followed by digits |
| `title`, `publisher`, `author`, `url` | Attribution; URL must be HTTPS without credentials |
| `player_ids` | Explicit warehouse player IDs |
| `event_start`, `event_end` | Inclusive dates of the events described in the summary |
| `version_available_date` | Latest displayed article publication/update date, ISO date |
| `summary` | Curator-authored factual paraphrase, at most 100 words |
| `content_kind` | `curated_paraphrase` |
| `content_sha256` | `reporting_evidence.digest(summary)` |
| `retrieved_at` | Capture timestamp |
| `status` | `available` or `withdrawn` |

Optional metadata can record a canonical URL, unknown first-publication date,
date precision, team IDs, and curation notes. A public page being accessible
does not establish permission to ingest or redistribute its full text. Source
access and storage terms must be checked before automated collection.

Cases JSON contains exactly ten distinct cases. Each has `id`, `player_id`,
`player_name`, `question`, `category`, `expectations`, `seasons`, `phase`,
`start`, `end`, `previous_start`, `previous_end`, `reporting_mode`, and
`reporting_cutoff`. Dates use ISO calendar dates; comparison windows are explicit,
ordered, and non-overlapping. Player names must match snapshot identities.

`published_by_cutoff` rejects reports whose displayed update falls after the
cutoff. `retrospective` can use later reporting about events in the requested
windows. Neither mode proves what an exact archived article version said at a
past instant. Statistics are corrected retrospective warehouse facts. This
milestone is not a historical knowledge reconstruction system.

## Run

Use a Python environment with the repository dependencies and an authenticated
Codex CLI. CLI authentication is reused; the runner does not use application API
keys or pass them to model subprocesses. Verify with `codex login status`.

Capture warehouse evidence once using the existing bounded, read-only tool:

```bash
python scripts/capture_semantic_snapshot.py \
  --project YOUR_PROJECT --seasons 2024-25 \
  --maximum-bytes-billed 100000000 \
  --output reports/ask-evidence/inputs/2024-25-snapshot.json
```

Prepare all evidence before spending model tokens:

```bash
python scripts/evaluate_reporting_evidence.py prepare \
  --snapshot reports/ask-evidence/inputs/2024-25-snapshot.json \
  --corpus reports/ask-evidence/inputs/corpus.json \
  --cases reports/ask-evidence/inputs/cases.json \
  --run-dir reports/ask-evidence/run-01

python scripts/evaluate_reporting_evidence.py generate \
  --run-dir reports/ask-evidence/run-01
python scripts/evaluate_reporting_evidence.py evaluate \
  --run-dir reports/ask-evidence/run-01
python scripts/evaluate_reporting_evidence.py report \
  --run-dir reports/ask-evidence/run-01
```

The generator uses Luna with low reasoning; the evaluator uses Terra with medium
reasoning. Each stage runs one batch of ten cases to amortize CLI overhead.
Cases therefore share a model context: this is a low-cost pilot, not ten
independent trials. Each case explicitly forbids using another case's evidence,
and the cutoff case helps expose this risk. Stronger future evaluations should
use independent sessions and a held-out question set.

CLI calls use an empty temporary working directory, read-only permissions,
disabled shell and web search, ignored user config, and ephemeral sessions.
The prompt forbids tool use; a completed tool event invalidates the run. No
automatic model substitution, output repair, or model-call retry occurs. A
failed attempt retains its artifacts; use a new run directory for a new attempt.

## Review and provenance

`review.html` is a standalone page with ten answers, expandable evidence,
collapsed Terra evaluations, and human decision/notes fields. Notes are stored
in that browser and can be exported as JSON. `review.md` is a readable companion.
The report does not transmit notes. Source links navigate to the publisher when
clicked. Serving the directory on loopback makes browser storage more reliable
than opening a `file:` URL.

Each report identifies its season and labels the comparison months in navigation.
Use a new run directory for another season or question set; prior responses and
browser notes remain intact. Optional `review-notes.json` entries (`case_id`,
`message`) render additional verification findings separately from the original
answer and Terra's verdict, including errors the evaluator missed.

The run also retains the frozen corpus/cases, full prepared Ask payloads,
snapshot identity, retrieval exclusions, generator/evaluator prompts and schemas,
raw outputs, CLI event streams, usage metadata, and structural validation
results. Original model output files are never overwritten. CLI metadata records
the exact requested model; the CLI event stream may not echo a resolved model ID.
Token counts are actual CLI usage, not an estimated dollar bill.

`step-review.html` is a separate review of eight pipeline stages, with independent
decisions, notes, and a JSON export. Each stage shows its inputs, outputs,
evaluation criteria, findings, limitations, and links to saved artifacts.
`step-audit.json` is the machine-readable companion. Regenerating `report` adds
these files to an existing recorded run without making new model calls.

The step audit independently recomputes per-game statistics from frozen raw rows,
rechecks structural citations, and reconciles CLI usage metadata with raw
completed-turn events. It checks saved prompts and responses against execution
evidence. Source fidelity, question representativeness, and retrieval recall
still need human assessment; these are not presented as automated passes.

The token ledger separates input and output usage for Luna generation and Terra
evaluation. Cached input and reasoning details are not added twice. Each model
ran one ten-case batch, so individual response token costs are unavailable.
Deterministic runtime stages use zero LLM tokens; setup and source curation are
unmetered, not free. The measured subtotal excludes setup, smoke tests,
implementation, and step-audit authoring. CLI subscription usage does not provide
a measured dollar cost. Response-review notes and step-review notes use distinct
browser storage keys and export filenames.

Terra scores statistical accuracy, citation support, temporal scope, causal
restraint, and usefulness from 0 to 2. Passing requires 2 on the first four,
at least 1 on usefulness, and no major issue or structural error. A ten-case
pilot and an automated judge do not establish production quality.

## Local checks

```bash
ruff check app/agent/reporting_evidence.py scripts/evaluate_reporting_evidence.py scripts/reporting_step_review.py tests/test_reporting_evidence.py
ruff format --check app/agent/reporting_evidence.py scripts/evaluate_reporting_evidence.py scripts/reporting_step_review.py tests/test_reporting_evidence.py
python -m pytest tests/test_reporting_evidence.py tests/test_performance_overview.py tests/test_semantic_migration.py
```

Tests cover numeric provenance, missing metrics/appearances, identity and time
filters, duplicate/conflicting/withdrawn evidence, unsafe URLs, citation fallback,
preservation of governed payloads, frozen-input tampering, batch identity checks,
judge rubric consistency, and escaping model content in the review page.

After human review, the next implementation step is to fix observed failures,
expand and diversify the corpus, and evaluate retrieval before enabling narrative
enrichment in interactive Ask. Scheduled ingestion, embeddings/vector search,
social-platform collection, and production serving remain future work.

## Review hardening

The CLI delegates artifact integrity, generator/judge contracts, isolated model
execution, and report rendering to `scripts/reporting_*.py`. The shared page shell
lives in `scripts/templates/reporting_review.html`; preview, response, and step
reviews populate named slots and keep distinct browser-review storage keys.

Before each model call, the output schema constrains array length to the requested
case count and restricts case IDs. Local validation still rejects duplicates and
incorrect order. Interrupted or invalid attempts retain their original artifacts;
there is no automatic paid retry. Claim instructions explicitly separate R-only
reported events from S-only statistical observations, with mixed evidence allowed
for interpretations. The strict fallback remains in place. These changes apply to
future runs; saved model answers and their original evaluations are not rewritten.
