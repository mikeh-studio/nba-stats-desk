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
