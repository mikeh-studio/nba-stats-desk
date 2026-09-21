# Public / Private Boundary

This repo is intended to stay public as NBA Stats Desk: the agentic stats
workbench, GCP-backed data platform, and reference application. The personal
model layer should live in a private repo or package. Adding feedback loops,
evaluation, and recovery workflows does not itself require moving system code
into a private repository. Keep reusable implementation public and operational
data and administrative access restricted.

## Keep Public

- source ingestion and orchestration
- source contracts
- dbt bronze, silver, gold, and agent models
- feature input tables
- public baseline similarity output contracts
- FastAPI routes and read-only app behavior
- public-safe docs and validation commands
- agent orchestration, application prompts, and tool definitions
- feedback, evaluation, repair, and release-control implementation
- reviewed synthetic or sanitized regression fixtures and concise evaluation
  summaries or incident write-ups
- `.env.example` placeholders

## Keep Private

- tuned similarity model code
- proprietary feature weights, thresholds, and model-selection experiments
  (documented public baseline parameters remain public)
- raw user feedback, conversations, traces, private labels and evaluation sets,
  and scouting notes
- generated reports under `reports/`
- local Ask history under `local_notes/`
- notebooks and local experiments
- model artifacts, checkpoints, and embeddings
- real `.env` files, service accounts, tokens, and warehouse exports
- raw quarantine exports and incident records containing sensitive information

## Feedback and operational workflow requirements

These are requirements for future implementation, not a statement that feedback
collection, authenticated administration, or automated recovery already exists.

Keep one public system repository by default. Store operational records in
access-controlled local or cloud storage with explicit retention and deletion
rules. A private code repository is appropriate for deliberately proprietary
personal-model work or confidential integrations; it is not the default store
for logs, feedback, or credentials. Credentials must remain outside Git, including
private repositories.

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
  [debugging guide](public-service.md#debugging-ask-failures) for private diagnosis.

Keep service logs access-controlled with a deployment-specific retention policy.
These source controls do not configure cloud IAM, log retention, or external
monitoring integrations that might independently capture request bodies.

## Local Spinout

The public repo can expose feature tables such as
`nba_gold.player_similarity_feature_input`. A private package can consume those
tables, train the personal model, and write private reports or model artifacts
without changing the public contract.

Recommended private repo shape:

```text
nba-personal-model/
  models/
  notebooks/
  reports/
  evals/
  README.md
```

Keep any local bridge scripts ignored until they are safe to publish.
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
proprietary tuning, private evaluation cases, prompts/outputs from experiments,
cost ledgers, session transcripts, and machine-specific debugging notes outside
the tracked documentation tree. Use ignored `local_notes/` for working notes and
`reports/` for run artifacts. Use a separate private repository for durable backup
and collaboration; ignored local files are not remotely backed up by this repo.

Do not blanket-ignore `docs/` or Markdown files. Existing tracked material must
be deliberately moved or untracked; ignore patterns only prevent future additions.
Removing a current file does not erase previous commits or public PR history.
Documentation boundaries do not conceal algorithms or prompts already present in
public source code. Changes to that code boundary require a separate review.
