# Agent Instructions

## Required startup

This repository follows the thin split. Before editing, read
[the agent workflow](docs/agent-workflow.md) and run from the repository root:

```bash
python3 scripts/agent_startup.py --intent INTENT
```

Use `product-ui` for public web-app features; `product` for private production
prompts, planner/router, intent heuristics and eval cases. `shared-fix`,
`reference-maintenance` and `public-promotion` are public scopes; `policy` covers
repository guidance/tooling in either repo. Default `product` remains private.
Report the repository/branch, ownership decision, intended checks and push target.
If blocked, change to the correct checkout or reconcile stale guidance; do not
weaken the guard or relabel product work to bypass it. Create a fresh `codex/`
branch from current `origin/main` before implementation. Preserve dirty work.

Run the same check again before pushing. Public publication requires explicit
user authorization covering the reviewed public diff before its first push.
An intent flag, a passing check, or a PR opened after pushing is not authorization.
Public PR #97 is merged; the existing private pin is an ancestor of public main.
Never automatically mirror private branches or edit inside the shared dependency.

Read optional `AGENTS.local.md` beside this file before working. If absent,
continue; shared requirements must work in a fresh clone.

## Work safely

- Inspect branch and working changes; preserve unrelated work and stage only task-owned files.
- This public repository owns the platform and whole web app: composition, UI,
  conversation workflow, deterministic prose, evaluation harness and a standalone
  reference planner. Follow the [boundary policy](docs/public-private-boundary.md)
  and [ownership map](docs/repository-ownership.md). Production prompts,
  planner/router, intent heuristics and private eval cases belong in private.
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
