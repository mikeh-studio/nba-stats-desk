# Opt-in rolling publication reader

The existing app remains the primary interface. `scripts.serve_production_candidate` composes
it with a separate ProductionApplication only when explicit operator configuration
and independently pinned source authority are present. V1 remains the fixed October
rehearsal contract. V2 manifest/binding/status use `core_stats_weekly_v1` with explicit
2026–27 Regular Season dates and unchanged deterministic metric semantics.

Production requires an operator-controlled source-use policy, fresh revocation
registry and independent release validation binding the complete manifest/binding,
inputs, code and checks. The reader checks immutable snapshot content and recomputes
all models. It rejects rehearsal receipts, missing/expired/revoked controls and
changed tables. The caller cannot provide arbitrary warehouse relations or SQL.

Reader config contains `project`, `namespace` (`nba_core_...`), `policy_path`,
`policy_digest`, `registry_path`, and `mode_path`. The mode path accepts a local
file for local operation or a GCS URI for durable cloud operation; each GCS read is
generation-pinned and bounded. Set its JSON `mode` to `legacy` to disable the reader.
Production GCS documents (mode, policy and registry) have a 30-second process-local
cache; release/pointer rows and snapshot metadata have a 5-second cache. Consequently
remote revocations and mode changes take at most 30 seconds to be observed, and
pointer/metadata changes at most 5 seconds. A failed refresh never reuses an expired
cache entry. Local authority files remain uncached. Authority validity times and
publication freshness are checked against the current clock on every request and
again before emission, including during a cache lease. Content is still read and
digest-checked before a new application is admitted. Only two release applications
are retained, and each control cache holds at most eight entries. Cache hits do not
extend leases.
Publication requests have an eight-request asynchronous admission limit, covering
mode reads, cold materialization, request-time checks and sync route handlers.
Control reads, materialization and sync endpoints use a separate eight-token
worker budget rather than Starlette's shared legacy worker tokens; queued
requests wait without a worker. The outer mode gate and inner reader have distinct limits, so direct
reader calls are also bounded without recursively acquiring the same limiter.
The limit covers response emission. Native task and AnyIO cancellation wait for
active sync workers to complete before releasing capacity. Async request-body,
stream and response-send waits remain cancellable. Each deployed process has its
own limits; this is isolation from legacy worker starvation, not
a distributed queue or a bound on total incoming connections.
Missing or expired authority affects publication requests (503), while legacy
startup and routes remain available. Ask retries the legacy service on publication
503, using the existing legacy source contract and displaying no publication ID.
The authority files and configuration must be independently administered and mounted
read-only. No source approval is supplied with this code. Importing `app.main`
does not activate the production reader. Public tests use synthetic controls only.

The launcher mounts the reader at `/publication-core` only in `production` mode.
Ask routes supported, self-contained core questions to the publication, and Player has a separate publication
card, even when the legacy player is missing. Both identify their publication and
scope, and leave the existing experience available when the publication is held.
Compare displays publication results for its selected pair and supported windows;
Performance offers a separate regular-season date/game selector pinned to one release.
Unsupported Ask questions and follow-ups and the legacy comparison and playoff
performance fields still read their existing sources; this is not a full-app publication cutover. The separate
`CutoverApplication` remains a limited-core rehearsal and is not used by this launcher.
For source/data incidents, revoke the affected release as well as changing mode.

Focused tests cover rolling status, bounds, reader content, revocation, pointer
pinning, historical/session availability and rollback. No deployment is implied.

V1 schemas remain supported for the isolated fixed-window offline rehearsal; they
are not deprecated and cannot authorize production. V2 is required for rolling
production publications. Neither version upgrades the other's authority.

The local weekly snapshot bridge requires an explicit `NBA_WEEKLY_SNAPSHOT_ROOT`;
the public app does not infer an operator's macOS home-directory state.
