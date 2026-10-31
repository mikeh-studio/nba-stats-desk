# Public / Private Boundary

This repository retains the existing public platform: ingestion, contracts, dbt models,
the web app, Ask orchestration, deterministic rendering, the evaluation harness and
a standalone reference planner. Model-backed planning can be supplied by an optional
installed provider through the [provider contract](intelligence-contract.md); the
app runs fully without one.

## Acquisition and unified data development boundary

The [exact public source-path map](acquisition-development-boundary.json) identifies
compatibility-only acquisition/unified paths and shared source guards. It contains
no private implementation or destinations. Production forward development at
compatibility-only paths belongs privately; existing public code is retained.
Shared public runtime guards and contracts have one public development authority.
Necessary correctness/security maintenance remains reviewable here.

Public main and the [disclosed draft checkpoint #112](https://github.com/mikeh-studio/nba-stats-desk/pull/112)
are separate trees. The map’s `presence_at_snapshot` describes membership only at its exact
`main_snapshot`; `absent_from_main_snapshot` makes no claim about moving main.
All entries were disclosed in that checkpoint, which receives no blanket merge
exception. The private extraction's
historical source is distinct from its supported public dependency. This policy
changes no source guard, app behavior, source access, scheduler, warehouse, host
lock or dependency pin, and removes no public code.

Public UI, serving, dbt models, reference behavior and shared interfaces retain
public ownership. Acquisition orchestration helpers such as `dags/dbt_builds.py`,
`dags/nba_pipeline.py`, `dags/publication.py`, `dags/warehouse_stages.py` and
`dags/season_config.py` are separately classified in the map; retaining dbt models
does not assign all orchestration forward development to public.

Run `scripts/check_acquisition_boundary.py --base BASE_SHA` to compare a proposed
change with its reviewed base. Only compatibility-only file changes require an exact
before/after hash and a correctness/security or approved public-promotion reason
in [the maintenance record](acquisition-maintenance.json). Missing or mismatched
records for those changed paths fail; unused historical records are reported and
authorize nothing. Shared public guards may receive normal feature work under
ordinary public review and tests; the check reports these changes without requiring
a private-maintenance record.
Snapshot values and membership are validated against the declared immutable Git
tree, and the snapshot must be an ancestor of the reviewed base commit. This
uses the supplied base rather than moving live main. Presence metadata can be
refreshed without changing ownership; ownership restrictions cannot be silently removed or reclassified. The check
reports policy-file changes in the Actions step summary for human review; it cannot establish the truth of
a maintenance reason or authorize publication. Contributor-written dispositions
are proposals. Human approval of the entire exact public diff remains mandatory.
The initial introduction of this map is explicitly reported as policy bootstrap.

The intended showcase consists of reviewed screenshots, high-level case studies
and individually approved synthetic examples. Do not copy private plans, source,
evaluation material or operational evidence into public files. Previously
disclosed history remains public. Public removal and runtime migration remain
separately reviewed actions.

Operational data never belongs in Git, public or private: raw conversations, user
feedback, traces, real evaluation captures, credentials, warehouse exports,
generated reports, trained artifacts and production state live in access-controlled
storage. Existing public history remains public, and browser assets are delivered
to users, so anything sensitive stays server-side.

## Enforced repository checks

- `.gitignore` excludes local instructions, operational directories, notebooks,
  database/model artifacts and key files. `.gcloudignore` includes these rules so
  they are also excluded from Cloud Run source uploads.
- After staging, run `python scripts/check_public_boundary.py`. CI runs the same
  check against the Git index, including files force-added despite ignore rules.
  It rejects private artifact paths and recognizable provider/cloud credentials;
  diagnostics show paths and rule names, never matching content.
- The check is targeted, not a comprehensive secret scanner or history scrubber.
  Review fixtures and public artifacts even when it passes, and construct synthetic
  credential-shaped test values at runtime instead of adding scanner exceptions.

## Ask logging, history and traces

- Ask service summaries keep server-generated request IDs, validated model IDs,
  routes, timings, token and tool-call counts, and outcomes. They omit question
  text, conversation IDs, tool arguments/results and raw provider exception bodies.
  Client-supplied request IDs are not reused; read the returned `X-Request-ID`.
- Failed Ask executions are not saved to local history; see the
  [debugging guide](pages.md#debugging-ask-failures).
- Keep service logs access-controlled with a deployment-specific retention policy.
  These source controls do not configure cloud IAM, log retention or external
  monitoring that might independently capture request bodies.

`AGENT_HISTORY_ENABLED=true` is a direct-localhost mode: Ask JSON/SSE and history
read/delete requests require a loopback socket peer, a localhost Host, no proxy
forwarding headers and, when supplied, a same-origin Origin. Remote or proxied
requests receive 403 before Ask generation or history mutation. This is not
multi-user authentication, so keep server history disabled for public deployments.
Browser-side history works independently.

Optional local Ask traces (`AGENT_TRACE_ENABLED`, off by default) use the same
direct-local predicate; remote requests are simply not captured. Records hold
bounded, redacted projections and stay out of Git. See
[trace storage and deletion](operations.md#private-local-ask-traces).

## Ask ingress

Run `python -m app` or pass `--no-proxy-headers` to Uvicorn so the app sees the
original socket peer. Rate limiting ignores `X-Forwarded-For` by default. Set
`AGENT_TRUSTED_PROXY_CIDRS` to specific ingress CIDRs only behind a verified proxy;
the app walks the forwarded chain right to left and uses the first untrusted hop.
Malformed, duplicate or oversized headers fall back to the socket peer, and
universal trust ranges are rejected. Confirm the actual ingress network instead of
copying a generic Cloud Run range.

The default in-memory limiter is per process; multi-instance serving should use the
Redis limiter for shared counters.

## Requirements for future feedback loops

Feedback collection, authenticated administration and automated recovery do not
exist yet. When they are added:

- Separate bounded public submission from restricted review. Validate size and
  shape, rate-limit submissions, and never expose other users' questions, answers
  or feedback.
- Record enough provenance to reproduce a failure (request and evidence references,
  software/model versions, reviewed expected behavior) while minimizing personal
  data and redacting secrets.
- Review feedback before it changes a regression expectation. Convert confirmed
  failures into synthetic or carefully sanitized fixtures, reviewing question text,
  evidence, tool results and metadata; removing a user ID alone is insufficient.
- Require server-side authorization for review inboxes, label changes, repair and
  backfill jobs, paid evaluation runs, and promotion or rollback. Keep
  administrative credentials separate from the public serving identity.
- Publish only reviewed, sanitized incident summaries. Check generated HTML,
  screenshots, CI artifacts and PR attachments as well as committed files.

## Documentation

Public docs cover implemented features, setup, API and data contracts, metric
definitions, limitations and reproducible validation. Working notes belong in the
ignored `local_notes/` directory and run artifacts in `reports/`. Root `AGENTS.md`
holds shared agent rules; an optional, Git-ignored `AGENTS.local.md` holds local
environment detail, and clones must work without it. `CLAUDE.md` and `GEMINI.md`
only import `AGENTS.md`.
