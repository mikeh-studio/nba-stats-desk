# Public / Private Boundary

This repo is intended to stay public as NBA Stats Desk: the agentic stats
workbench, GCP-backed data platform, and reference application. The personal
model layer should live in a private repo or package.

## Keep Public

- source ingestion and orchestration
- source contracts
- dbt bronze, silver, gold, and agent models
- feature input tables
- public baseline similarity output contracts
- FastAPI routes and read-only app behavior
- public-safe docs and validation commands
- `.env.example` placeholders

## Keep Private

- tuned similarity model code
- feature weights, thresholds, and model-selection logic
- manual labels, evaluation sets, and scouting notes
- generated reports under `reports/`
- local Ask history under `local_notes/`
- notebooks and local experiments
- model artifacts, checkpoints, and embeddings
- real `.env` files, service accounts, tokens, and warehouse exports

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
