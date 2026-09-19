# Model catalog review

Ask's enabled models remain in `app/config.py`. Discovery never adds a model to
the dropdown or changes a default. An optional local check provides a review
report and an explicit candidate smoke test.

## Check availability

From the repository root, with dependencies installed and provider credentials
configured in your local `.env`:

```bash
python -m app.model_catalog
```

Read `reports/model-catalog/review.md`. The report includes configured IDs missing
from the provider, unconfigured GPT/Claude IDs to review, additions/removals since
the last successful snapshot, and shutdown dates when the provider supplies them.
Unconfigured IDs may include specialized models and snapshots; they are discovery
candidates, not necessarily compatible Ask models. An older model is not
necessarily deprecated. Absence of a shutdown date is not a guarantee of support.

The checker follows SDK pagination and attempts to retrieve configured aliases
that are absent from the list. A missing credential or failed request produces
exit code 2 and an unverified/stale report, retaining the previous successful
snapshot. Exit code 0 means both catalogs were refreshed, not that all models
passed review. `latest.json` stores state; timestamped JSON files preserve runs.
Reports are ignored by git. Use separate output directories for different accounts.

Catalog checks run locally on demand. Provider credentials stay in your local
environment; no GitHub Actions secrets or scheduled workflow are required.

## Evaluate one candidate

```bash
python scripts/evaluate_agent_questions.py \
  --provider openai --model CANDIDATE_MODEL_ID \
  --fixture tests/fixtures/model_compatibility.yml
```

Use `--provider claude` for Anthropic. This is an explicit live evaluation: it
requires configured BigQuery access and provider credentials, and incurs normal
warehouse/model usage. The catalog check does not run inference evaluations.
A candidate does not have to be enabled in Ask to run this command.

The three questions exercise rankings, player trends, and similarity through the
existing agent. Checks cover expected tool names, expected terms, nonempty answers,
and structured top-level fields. Reports record the exact provider/model, case
failures, and elapsed seconds in separate `reports/model-evals/<timestamp>.json`
files. These are smoke tests, not proof of numeric accuracy, raw provider schema
compliance, streaming, conversation history, or cost. Manually inspect those paths
before promoting a model, then update the existing options/defaults in config.

Provider API references:
- https://developers.openai.com/api/reference/python/resources/models/methods/list
- https://platform.claude.com/docs/en/api/models/list
