# Public / Private Boundary

This repo stays public as NBA Stats Desk: the shared data platform, reusable
analytics, and reference application. A separate private product repository owns
new website experiences, product-specific workflows, and proprietary analysis.
Private model research can remain in its existing dedicated repositories.
Keep reusable implementation public and operational data and administrative
access restricted. A private code repository can power a public website; it does
not provide authentication or authorize publication of its data.

## Repository ownership

| Area | Owner and boundary |
| --- | --- |
| `dags/`, ingestion and repair scripts, `contracts/`, `dbt/`, `season.yml`, `dags/season_config.py` | Public platform remains the single source of shared pipeline code, source validation, season scope, and statistics. |
| Governed NBA metrics, identity resolution, season/phase validation, evidence provenance, baseline models, and reusable evaluation tools in `app/` | Public and authoritative, including improvements discovered during private product development. |
| Reference UI, Ask orchestration, prompts, and tools in `app/` | Public reference implementation that continues to receive fixes and useful shared improvements. |
| New differentiated website flows, personalization, product-specific agent workflows, proprietary features and models | Private product or research repositories. Generic improvements can be deliberately contributed back with sanitized tests. |
| Generic infrastructure templates, CI, and reusable release tooling | Public when free of operational details; product-specific deployment configuration and release policy belong in the private product repo. |
| Credentials, user records, raw feedback, source exports, traces, trained artifacts, and production state | Access-controlled storage outside Git, including private Git. |

### Sharing the platform

The private product pins an immutable public revision. The
public repo remains independently buildable and never depends on private code.
Do not copy DAGs or dbt models into the product or start a second production
ingestion schedule. Shared fixes land here first; dependency updates are separate
reviewable product changes. Preserve this repository's license in the dependency.

Select a narrow runtime interface for each product capability before implementing
it. Prefer an existing reviewed read API when it supplies the required evidence;
otherwise review the missing contract before introducing direct warehouse reads
or a small shared library. Keep governed arithmetic in this platform rather than
copying it into product SQL or a private Ask fork. Public shared logic defines
the NBA statistics; private methods may derive clearly labeled proprietary
scores without claiming those scores are canonical NBA metrics.

A source pin does not freeze live schemas, data, freshness, or season/archive
routing. Product requests must preserve explicit scope and evidence availability.
Dependency updates follow the reviewed rollover sequence; fixture-based CI does
not establish live readiness. Source reuse supplies no credentials or permission
to run warehouse writers. Future private model jobs publish only validated,
versioned outputs through an explicitly reviewed publication contract.

### Initial transition

Existing tracked platform and reference-app code stays here. Ownership follows
responsibility, not the date code was written: shared correctness changes land
publicly, while differentiated product workflows start private. There is no bulk
copy of the working checkout, ignored
research, or uncommitted work. Moving a currently tracked module later requires
an explicit path inventory, consumer compatibility checks, and a reviewed change.
Previously published code remains in public history.

Repository setup does not activate production. Prior-season archive publication,
IAM, deployment, scheduler repair or activation, warehouse writes, and any live
polling remain pending operator review under the
[season rollover procedure](operations.md#season-rollover).

## Keep Public

- source ingestion and orchestration
- source contracts
- dbt bronze, silver, gold, and agent models
- feature input tables
- public baseline similarity output contracts
- reference FastAPI routes and reusable read-only app behavior
- public-safe docs and validation commands
- existing reference agent orchestration, prompts, and tool definitions
- reusable feedback, evaluation, repair, and release-control tooling
- reviewed synthetic or sanitized regression fixtures and concise evaluation
  summaries or incident write-ups
- `.env.example` placeholders

## Private source repositories

- new product-specific website code, personalization, and agent workflows
- proprietary feature engineering and product-specific deployment configuration
- tuned similarity model code
- proprietary feature weights, thresholds, and model-selection experiments
  (documented public baseline parameters remain public)
- private research source, experiment definitions, design documents, and synthetic
  product evaluation cases that contain no real user or operational records

Assign each proprietary model one source owner. Product and research repositories
must not maintain parallel copies of the same model or pipeline. Keep shared
metric/evidence corrections public with sanitized tests.

## Private storage outside Git

- raw user feedback, conversations, traces, private labels and evaluation captures,
  and scouting records
- generated reports under `reports/` and local Ask history under `local_notes/`
- raw datasets and generated notebook outputs
- model artifacts, checkpoints, and embeddings
- real `.env` files, service accounts, tokens, and warehouse exports
- raw quarantine exports and incident records containing sensitive information

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
nba-personal-model/
  src/             # model implementation, not trained binaries
  evals/synthetic/ # synthetic fixtures and evaluation code
  docs/            # private design and methodology
  README.md
```

This is an ownership example, not a migration of an existing repository. Local
`notebooks/`, `reports/`, raw data, and generated model artifacts must remain
ignored or in access-controlled external storage. Ignored files are not backed
up by Git. Product-specific adapters can be tracked privately; reusable adapters
can be contributed here after review.

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
