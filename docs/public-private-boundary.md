# Public / Private Boundary

This repository retains the existing public platform: ingestion, contracts, dbt models,
the web app, Ask orchestration, deterministic rendering, the evaluation harness and
a standalone reference planner. Model-backed planning can be supplied by an optional
installed provider through the [provider contract](intelligence-contract.md); the
app runs fully without one.

## Showcase ownership amendment: acquisition and unified-data Phases 1–2

Production acquisition/recovery and the unified-data Phase 1–2 implementation
have private forward-development ownership. Their exact file inventory and
transfer plan are maintained in the private product repository. This policy
amendment accompanies a privately validated offline transfer. It removes no public
compatibility files and changes no source guard, application behavior, source
access, scheduler, warehouse or dependency pin.

The existing public baseline remains available and unchanged by the transfer.
The private implementation is not automatically selected by any public reader or
scheduler. Necessary baseline correctness and security fixes remain reviewable
here; new production acquisition and unified
data enhancements are not public by default. UI, serving, dbt, reference planning,
generic evaluation infrastructure and existing documentation remain public for
this slice; their retention is not approval for every future product enhancement.

The intended public showcase consists of reviewed screenshots, high-level
engineering case studies and selected runnable synthetic examples. No new sample
or extraction is approved for publication by this amendment. Do not copy private
implementation or private planning records into public documentation or fixtures.
Previously published versions remain disclosed. Source guards must remain
effective in both retained public execution and any later private implementation.

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
