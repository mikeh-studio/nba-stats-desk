# Local Jobs monitor

The public app can explicitly mount the existing read-only warehouse refresh
monitor for a local state directory:

```sh
PYTHON_DOTENV_DISABLED=1 python -m app.local_server app --state-dir /absolute/path/to/refresh-state --snapshot-root /absolute/path/to/private-data --port 8785
```

Use `python -m app.local_server jobs --state-dir /absolute/path/to/refresh-state`
for the standalone Jobs page at `/`. Both `app` and `jobs` accept repeatable
`--also-state-dir` arguments and an optional `--snapshot-root`. The three former
`scripts/serve_*` entry points remain compatibility wrappers with their original
modes and options. All modes bind loopback with proxy headers disabled and disable
dotenv loading. The `weekly` mode still requires independent authority; it does
not inherit the normal app's data-source configuration.

Open `/jobs-monitor/` on the loopback server. The Jobs navigation link appears
only in this opt-in process. The monitor reads existing configuration, status,
run records and redacted logs and reports launchd state. It does not start a job,
query the warehouse, collect sources, install a scheduler or select a snapshot.
Unknown or inactive scheduler state is displayed as such. Do not place operational
records in Git. The optional snapshot root reads existing offline PTS build
records separately from the warehouse refresh. Snapshot scheduler state and live
freshness are not inferred from an offline success.

The normal app can read a local weekly snapshot when
`NBA_WEEKLY_SNAPSHOT_ROOT` is explicitly set to an absolute path. With the setting
unset or empty, it does not discover a local snapshot. `--snapshot-root` explicitly selects a root
for the combined launcher. The Ask page's weekly PTS lookup and scoped Player
and scoring comparison sections read the selected successful local build; other
app features retain their existing data source. The local result is labeled
with its season, date scope and snapshot ID, and original source expiry remains
enforced. Natural-language Ask is unchanged.

The offline PTS candidate can also explicitly mount the same monitor by passing
`jobs_app=create_jobs_app(state_dir, snapshot_root=snapshot_root)` to
`create_candidate_application`. It then exposes a Jobs link and remains bound
to the selected offline PTS service. Configuring a monitor does not select a
snapshot or prove that the monitored build is the one selected by the service;
operators must check the snapshot IDs when composing a candidate.

`create_weekly_candidate_application` reads the offline weekly selection using
the supplied reader policy. Pass all four historical inputs to enable a
two-season candidate. To refresh in the same process, provide `binding_loader`
and, when reader or identity authority may change, `refresh_context_loader`.
These trusted caller-supplied loaders provide the current binding and any updated
policy, binding checker, player catalog, historical policy or composition
authority. A new selection is installed only after validation; an in-flight
request retains its original service. Without a binding loader the selection
remains fixed. This opt-in candidate does not activate the installed runtime.

An explicit local launcher connects the candidate to an independently supplied
authority file and optionally the Jobs monitor:

```sh
PYTHON_DOTENV_DISABLED=1 python -m app.local_server weekly --data-root /absolute/data-root --authority /absolute/authority.json --jobs-state /absolute/refresh-state --port 8785
```

The authority document is controlled separately from the snapshot; it is not
created or approved by the snapshot reader. It must be a regular file owned by
the current user and not group/world writable. The top-level fields are `schema`
(`weekly-app-authority/1`), `binding` (all `CandidateBinding` fields, with ID
tuples as arrays and expiration as an ISO timestamp), `current_status`,
`current_policy`, `players` (an array of `[id, name]` pairs), and `historical`
(null, or the historical fields below). The status fields are `status_id`,
`checked_at`, `valid_until`, `allowed_snapshot_ids`, and `dependency_states`.
The current policy fields are `freshness_policy_fingerprint`,
`source_identity_authority_id`, and an explicitly approved
`max_status_age_seconds` from 1 through 86400 (one day). The status's own
expiry and the snapshot's source freshness deadline still apply. The optional
`historical` object contains `root`, `snapshot_id`, `status` (same shape), and
`composition`: `policy_id`, `identity_record_id`, `historical_identity_id`,
`current_identity_id`, and `approved_player_ids`.

An operator or separately trusted issuer must update the file atomically when
the approved selection or status changes. Do not create an authority record
merely because a snapshot exists. When an authority changes or is revoked, the
reader rechecks it; invalid or expired authority cannot be extended by refresh.
Policy, player-catalog or composition changes require a changed binding; status
can renew independently with its actual verification time. A weekly snapshot
does not itself provide the daily authority-status verification.
An issuer-produced authority includes a `registry` object containing the
absolute registry `path`, `policy_path` and approved `policy_sha256`. Every
binding and status read verifies the policy file and checks the owner-controlled
registry for disabled policy, stale
revision, revoked snapshots, dependencies or code. The launcher fingerprints
the local app source and refuses a binding for different code. This registry is
a local revocation authority; it does not independently monitor upstream sources.
The launcher itself neither collects data nor issues an approval, and it remains
an opt-in loopback candidate until the operational serving policy is adopted.
