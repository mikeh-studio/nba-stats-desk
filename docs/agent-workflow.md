# Agent startup and handoff

`AGENTS.md` is the instruction entry point. The boundary document and
[ownership map](repository-ownership.md) define capability ownership. Do not keep
independent versions of these rules in agent-specific files or chat memory.

## Before implementation and before pushing

1. Read `AGENTS.md`, its optional local supplement, this workflow, and the ownership
   map. Inspect branch, changes, and remotes. Preserve unrelated work.
2. Fetch the intended repository's `origin/main` and start a fresh `codex/` branch
   or isolated worktree. Do not reset or automatically rebase a dirty checkout.
3. Run `python3 scripts/agent_startup.py --intent INTENT` from that checkout.
4. State the repository/branch, capability owner, planned validation and push
   destination. Continue only when the selected intent matches the actual task.
5. Run relevant tests and boundary checks. Repeat preflight before pushing; inspect
   the complete public diff before any public push. Use explicit `git push origin`
   with the task branch rather than an implicit or alternate destination.

| Intent | Repository and purpose |
| --- | --- |
| `product` (default) | Private product: UI, prompts, routing, workflows, experiments |
| `shared-fix` | Public: shared pipeline, statistics, identity/evidence correctness |
| `reference-maintenance` | Public: reference-app security, correctness and compatibility |
| `public-promotion` | Public: a specifically selected, sanitized component approved for publication |
| `policy` | Either repository: instructions, ownership docs and boundary tooling |

Public-first feature work is not reference maintenance. If uncertain, design in
private and resolve the shared interface before publishing. Public PR #97 stays
unmerged. The private runtime pin to its existing commit is intentional and must
not be changed just to obtain newer instructions.

## Agent entry points

- Codex and Cursor: root `AGENTS.md`.
- Claude Code: root `CLAUDE.md` imports `@AGENTS.md`, including clients that do
  not discover `AGENTS.md` directly.
- Gemini CLI: root `GEMINI.md` imports `@./AGENTS.md`.
- GitHub Copilot: `.github/copilot-instructions.md` directs it to root `AGENTS.md`.
- Other agents: explicitly load `AGENTS.md` and run preflight in their task setup.

CI verifies these exact adapters and required policy files in the Git index.
Root `CLAUDE.md` is allowed only as the exact shared-policy import; arbitrary or
nested Claude instruction files remain excluded from the public index. Local
notes belong in ignored `AGENTS.local.md`, without secrets. Do not use
`AGENTS.override.md` to shadow shared rules.

Existing sessions may have already loaded old instructions. Restart them in the
correct checkout or explicitly provide the new policy and require preflight again.
Fresh instructions are not automatically broadcast to running agents. In old
worktrees without the script or policy, use a new checkout from current main;
do not reinterpret missing checks as permission to continue publishing.

## What the check establishes

The script verifies the staged policy/adapters, the expected GitHub fetch and all
origin push URLs, the repository role for the task, a `codex/` branch, and matching
local/staged guidance. It rejects dependency worktrees, missing/stale policy,
wrong destinations, public product work, detached HEAD and main for implementation.
It reports dirty state without discarding changes or printing their contents.
It makes no network calls, reads no credentials, and does not mutate Git settings.
`--check-policy` is CI layout verification and permits detached checkout; it must
not be used to bypass implementation startup checks.

The generic script is maintained by the public platform and vendored at the same
version in the private product because its runtime dependency stays on #97.
Keep the common regression suite with the utility when updating either copy.
Repo-specific role and validation commands live in `docs/agent-policy.json`.

This is a mistake-detection workflow, not an access-control system. Agents can
skip commands or alter files; intent declarations cannot prove human authorization.
CI runs after publication and branch protection cannot recall a public push.
Required checks, protected policy-file review, and separately scoped credentials
are stronger controls, but this change does not configure account permissions,
branch protection, global agent settings, or automatic hooks. It never authorizes
production operations or changes model/serving flags.

## References

- [Codex instruction discovery](https://learn.chatgpt.com/docs/agent-configuration/agents-md)
- [Claude shared instructions](https://code.claude.com/docs/en/memory)
- [Cursor AGENTS.md](https://cursor.com/docs/rules)
- [Gemini context imports](https://geminicli.com/docs/cli/gemini-md/)
- [Copilot repository instructions](https://docs.github.com/en/copilot/how-tos/copilot-on-github/customize-copilot/add-custom-instructions/add-repository-instructions)
