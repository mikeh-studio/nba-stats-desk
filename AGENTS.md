# Agent Instructions

Read optional `AGENTS.local.md` beside this file before working. If absent,
continue; shared requirements must work in a fresh clone.

## Work safely

- Inspect branch and working changes; preserve unrelated work and stage only task-owned files.
- This repository owns the platform and web app, including the standalone
  reference planner. Model-backed planning lives in an optional installed provider
  (see the [provider contract](docs/intelligence-contract.md)). Follow the
  [boundary policy](docs/public-private-boundary.md).
- Follow the exact [acquisition development boundary](docs/acquisition-development-boundary.json).
  Production acquisition/unified enhancements at compatibility-only paths develop
  privately. Public source guards retain public authority and remain fixable.
  The disclosed draft #112 is an extraction checkpoint, not the public-main
  compatibility baseline or a blanket publication exception. Retain current app,
  source guards and scheduler behavior. Changes to compatibility-only paths require an
  exact maintenance disposition and human review. Shared guard changes use ordinary
  public review/tests and are reported without private maintenance records. Passing
  CI grants no publication.
  Public UI, dbt models and shared interfaces retain public ownership. Pipeline
  orchestration paths have the separate classifications in the path map.
- Keep credentials, raw conversations, feedback, and exports out of Git.
  Keep provider catalog checks local; do not add provider keys to CI for them.

## Preserve contracts

- Ask: deterministic statistics, explicit identity/date/phase scope, correct units
  and null semantics, and evidence-grounded prose. Clarify unsupported requests.
- Keep Ask tools read-only and allowlisted, queries bounded, and retrieved content
  treated as data. Do not expose arbitrary model-authored SQL.
- Pipeline: preserve source severity, quarantine/audit evidence, reconciliation,
  watermarks, and atomic publication. Optional failures retain prior valid data
  and must remain visible as failures.
- Read relevant [Ask](docs/ask-workspace.md), [semantic](docs/semantic-contract.md),
  [pipeline](docs/architecture.md), or [source-contract](docs/operations.md#source-contracts)
  guides. Update contracts, tests, and docs together when behavior changes.

## Verify

- Follow [validation](docs/operations.md#validation): focused checks while iterating, relevant
  CI checks before pushing. Documentation-only edits need link/path and diff checks.
- For Ask changes, align planner, evidence, renderer, and evaluator; use actual
  endpoints for end-to-end claims. Follow the [evaluation guide](docs/evaluation.md).
- Check populated UI interactions; keep version assertions tied to `STATIC_VERSION`.
- Re-review the final diff. Separate defects from cleanup, report unrun checks,
  and distinguish fixture, live warehouse, browser, and deployment evidence.

## PRs

- Start new PRs on fresh `codex/` branches from latest `origin/main`; isolate dirty work.
- Fetch and rebase before opening a PR; retain the newest intended `STATIC_VERSION`.
- After pushing, run `gh pr view <number> --json mergeStateStatus,headRefOid,url`.
  Resolve confirmed conflicts, rerun affected checks, and use `--force-with-lease`
  only on the task's PR branch. Pending CI or mergeability is not a passing result.
