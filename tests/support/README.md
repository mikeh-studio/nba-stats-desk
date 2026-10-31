# Shared test factories

Shared synthetic setup belongs here, so importing a settings factory or source
builder does not import another test module and its collection code. These
modules must not import `tests.test_*` or `test_*` modules.

| Module | Explicit fixture profile |
| --- | --- |
| `semantic.py` | Frozen semantic evidence, controlled plan responses and semantic settings |
| `api.py` | API repository/client factories; the API settings factory omits credentials by default |
| `agent.py` | Legacy tool-orchestration settings, controlled responses and repository |
| `espn.py` | Small ESPN schedule/box/PBP fixtures with explicit provider and player IDs |
| `historical.py` | Historical observations and receipts; participant completeness is a separate opt-in wrapper |

Keep these profiles distinct. A default complete cohort, implicit provider
fallback, credential, or silently filled statistic can invalidate a test of a
different boundary. Construct fresh mutable data per case; do not move local
reviewed evidence, credentials or private provider logic into public fixtures.

The aggregation-provenance tests retain exhaustive label/value/wording cases at
the policy boundary and representative positive/contradictory requests through
both JSON and SSE. Arithmetic, zero/null membership, assumptions and public
diagnostic normalization remain endpoint assertions. Follow-up, scope mismatch
and unsupported-input endpoint regressions remain separate.

Use focused tests during iteration and the full offline suites before a broad
test refactor is considered verified. Follow the repository's dependency and
credential restrictions; do not turn skipped or excluded checks into passing
evidence. Live source validation remains separate from synthetic fixture tests.
