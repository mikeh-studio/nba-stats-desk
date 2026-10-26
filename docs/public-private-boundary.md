# Public / Private Boundary

## Decision: thin split

Public owns the platform and whole web app: UI, templates, static assets, app
composition, conversation workflow, deterministic server-written prose, evaluation
harness and a standalone reference planner. Private owns production prompts,
planner/router, intent heuristics and private evaluation cases. The
[ownership map](repository-ownership.md) and its JSON patterns define the split.

Public app features are developed here with `product-ui`; private intelligence is
developed in its owning repository. Tests of public implementation and reusable
synthetic fixtures are public. Private evaluation cases, including sanitized cases
distilled from production failures, stay private unless specifically approved for
publication. Raw conversations, captures and feedback remain outside Git.

App composition and `service.py` orchestration are public. The v2 hook supplies
private production planning text and proposals. The v2 protocol has no answer-prose
channel; the host rejects malformed or extra output. Providers are trusted
in-process code, not sandboxed plugins.
Bounded public scope/chart selection and the frozen legacy narration exception
are classified in the [ownership audit](intelligence-ownership-audit.md). Governed
calculations, evidence/claim validation, read adapters, pipelines and existing
private research model ownership do not move. Deterministic prose stays public.

## Transition and dependency

The public app loads an optional intelligence package and runs standalone with
the reference planner. Released host tags support reproducible package assembly.
Select a reviewed immutable compatible host release for assembly. Removing old
app copies or replacing the legacy submodule still requires a reviewed cutover.
The private Active migration freeze is unchanged; existing copies are
compatibility scaffolding, not ownership.

PR #97 merged as `2e2a319`; the unchanged private pin `735bf4f` is now on public
main. It is not a released tag or proof of live coverage. Do not bump the pin just
to obtain policy updates. Maintain the shared policy tools directly in both repos.

Existing public history remains public. Browser assets are delivered to users;
sensitive intelligence stays server-side. Repository privacy supplies neither
website authentication nor permission to expose source data. Dependency pins prove
source reproducibility, not credentials, archive coverage or deployment readiness.

## Deliberate public promotion

1. Prepare a public-owned app/platform change, or explicitly select a private
   capability for publication.
2. Inspect its complete dependency closure, fixtures, documentation, screenshots,
   generated artifacts, and PR text. Replace private inputs with synthetic cases.
3. Preserve license notices and prove it builds without private dependencies.
4. Obtain explicit user approval of the exact diff before pushing, then open a
   focused public PR with relevant validation. A push to
   any public branch already publishes code; review the boundary before pushing.
5. Record the resulting public owner/version privately. Retire any duplicate
   private implementation and update the dependency pin through a separate review.

Never mirror the private repository, automatically copy private branches into the
public repository, or use public CI to evaluate private source or records.

## Private source and storage

Private Git may contain product source, synthetic evaluation cases, design notes,
experiment definitions, and proprietary model source. It must not contain raw
conversations, user feedback, real evaluation captures, credentials, source dumps,
warehouse exports, generated reports, trained artifacts, or production state.
Keep those in access-controlled storage outside either Git repository. The existing
ignore and staged-index checks remain necessary but are not complete secret scans.

Repository migration does not activate production. Archive publication, IAM,
deployment, scheduler changes, warehouse writes, and live polling remain separate
operator decisions under the [season rollover procedure](operations.md#season-rollover).

## Feedback and operational workflow requirements

These are requirements for future implementation, not a statement that feedback
collection, authenticated administration, or automated recovery already exists.

Keep one shared public platform and use private source repositories for the
product or deliberately proprietary research. Store operational records in
access-controlled local or cloud storage with explicit retention and deletion
rules. A private source repository is not a storage destination for logs,
feedback, credentials, raw exports, or trained artifacts.

When adding feedback and improvement loops:

- Separate bounded public feedback submission from restricted review. Validate
  input size and shape, rate-limit submissions, and do not expose other users'
  questions, answers, or feedback through the submission interface.
- Record enough provenance to reproduce a failure: request and evidence
  references, relevant software/model versions, and the reviewed expected
  behavior. Minimize stored personal data and redact secrets from traces.
- Review feedback before treating it as ground truth or changing a regression
  expectation. Convert confirmed failures into synthetic or carefully sanitized
  public fixtures where possible. Review question text, evidence, tool results,
  and attached metadata; removing a user ID alone is insufficient.
- Require server-side authorization for remotely accessible review inboxes,
  label/approval changes, repair and backfill jobs, paid evaluation jobs, and
  promotion or rollback actions. Public source code does not imply public
  permission to execute these operations. Keep administrative write credentials
  separate from the public serving identity.
- Keep the existing local-only history boundary until an explicitly designed
  authenticated replacement is implemented and verified. A conversation ID or
  an unlinked administrative URL is not authorization.
- Publish only reviewed, sanitized summaries of incidents and improvements.
  Keep raw run artifacts in restricted storage. Check generated HTML, screenshots,
  CI artifacts, and PR attachments as well as committed files before publication.

Ignore patterns prevent accidental additions; they do not provide access control
or remove previously tracked data. Private storage needs its own permissions and
backup policy.

## Enforced repository and deployment checks

- `.gitignore` excludes private instructions, operational directories, notebooks,
  database/model artifacts, and key files. `.gcloudignore` includes these rules
  so local private files are also excluded from Cloud Run source uploads.
- After staging, run `python scripts/check_public_boundary.py`. CI runs the same
  check against the Git index, including files force-added despite ignore rules.
  It rejects private artifact paths and recognizable provider/cloud credentials;
  diagnostics show paths and rule names, never matching content.
- This targeted check is not a comprehensive secret scanner or history scrubber.
  Review fixtures and public artifacts for sensitive content even when it passes.
  Construct synthetic credential-shaped test values at runtime instead of adding
  scanner exceptions for test directories.
- Ask service summaries retain server-generated request IDs, validated model IDs,
  routes, timings, token counts, tool-call counts, and outcomes. They omit question
  text, conversation IDs, tool arguments/results, and raw provider exception bodies.
  Read the returned `X-Request-ID`; client-supplied IDs are not reused. Rich answer
  payloads remain available to the requester and optional direct-local history.
  Failed Ask executions are not saved to local history; follow the
  [debugging guide](pages.md#debugging-ask-failures) for private diagnosis.

Keep service logs access-controlled with a deployment-specific retention policy.
These source controls do not configure cloud IAM, log retention, or external
monitoring integrations that might independently capture request bodies.

## Private research integration

The public repo can expose feature tables such as
`nba_gold.player_similarity_feature_input`. A private package can consume those
tables, train the personal model, and write private reports or model artifacts
without changing the public contract.

Example tracked private research source:

```text
private-model/
  src/             # model implementation, not trained binaries
  evals/synthetic/ # synthetic fixtures and evaluation code
  docs/            # private design and methodology
  README.md
```

This is an ownership example, not a migration of an existing repository. Local
`notebooks/`, `reports/`, raw data, and generated model artifacts must remain
ignored or in access-controlled external storage. Ignored files are not backed
up by Git. Product-specific adapters can be tracked privately; reusable adapters
can be deliberately promoted here after review.

Keep `AGENT_HISTORY_ENABLED=false` for public deployments unless the endpoint is
put behind a real auth boundary.

## Enforced Ask ingress boundaries

`AGENT_HISTORY_ENABLED=true` is a direct-localhost mode: Ask JSON/SSE and history
read/delete requests require a loopback socket peer, a localhost Host, no proxy
forwarding headers and, when supplied, a same-origin Origin. Remote/proxied
requests receive 403 before Ask generation or history mutation. Browser history
still works independently when server history is disabled. This is not multi-user
authentication; keep server history disabled for public deployments.

Run `python -m app` or pass `--no-proxy-headers` to Uvicorn. The application must
receive the original socket peer, not a client address already rewritten by the
server. By default, rate limiting ignores X-Forwarded-For. Set
`AGENT_TRUSTED_PROXY_CIDRS` to specific ingress network CIDRs only when deploying
behind a verified proxy; the app walks the forwarded chain from right to left and
uses the first untrusted hop. Malformed/duplicate/oversized headers fall back to
the socket peer. Universal trust networks are rejected. Confirm the actual ingress
network and header-appending behavior; do not copy a generic Cloud Run trust range.

The default in-memory limiter remains per process. Public multi-instance serving
should use the existing Redis limiter for shared counters. These changes do not
introduce user authentication or resolve the remaining dependency advisories.

## Documentation publication policy

### Agent instructions

The root `AGENTS.md` contains shared implementation rules and links to the public
guides. Keep it in version control. An optional root `AGENTS.local.md` contains
private environment and workflow detail and is ignored by Git; the shared file
explicitly asks agents to read it when present. It is not an automatically merged
instruction filename. New clones and worktrees must work without it.

Keep shared security and data requirements in public guidance. Local supplements
must preserve those requirements and must not contain credentials. Avoid using
`AGENTS.override.md` for this split: Codex selects it instead of `AGENTS.md` in the
same directory. See [instruction discovery](https://learn.chatgpt.com/docs/agent-configuration/agents-md).

### Public guides and private artifacts

Public docs showcase implemented features and provide setup, API/data contracts,
metric definitions, limitations, and reproducible validation commands. Keep
architecture explanations focused on the system that exists. Public evidence
should be concise, dated, and clear about what was actually tested.

Keep detailed research surveys, rejected alternatives, roadmap planning,
proprietary tuning, and synthetic product evaluation designs outside this public
documentation tree; private source repositories can track those documents for
collaboration. Raw experiment prompts/outputs, private evaluation captures, cost
ledgers, session transcripts, and operational debugging records stay outside Git.
Use ignored `local_notes/` for working records and `reports/` for run artifacts,
with access-controlled backup separate from source repositories.

Do not blanket-ignore `docs/` or Markdown files. Existing tracked material must
be deliberately moved or untracked; ignore patterns only prevent future additions.
Removing a current file does not erase previous commits or public PR history.
Documentation boundaries do not conceal algorithms or prompts already present in
public source code. Changes to that code boundary require a separate review.

Optional private Ask traces (`AGENT_TRACE_ENABLED`, false by default) use the
same direct-local ingress predicate as history, but remote requests simply
remain uncaptured. Keep this disabled in public deployments. Records include
bounded, redacted local question/context projections and must remain private.
See [private trace storage and deletion](operations.md#private-local-ask-traces).
They are diagnostic projections, not complete replay captures or public logs.

## Agent entry instructions

Follow [the agent startup workflow](agent-workflow.md). Root `CLAUDE.md` and
`GEMINI.md` contain only imports of `AGENTS.md`; the Copilot adapter points to
that same file. These are shared source instructions, not private local notes.
The startup check defaults to private production intelligence (`product`); use
`product-ui` for public app work. It verifies repository,
branch, instruction wiring and origin destinations before editing or pushing.
