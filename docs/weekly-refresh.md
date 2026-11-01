# Weekly personal refresh

`scripts/weekly_refresh.py` runs the existing validated core pipeline as a bounded
one-shot Airflow DAG execution. It does not require a continuously running
scheduler. The default personal schedule is Sunday at 08:00 America/Los_Angeles.
The installer prepares configuration and a macOS LaunchAgent; preparation alone
does not activate it.

## Explicit ESPN box-score path

For a personal local ESPN source, prepare a **separate state directory** with
`--source espn --first-date YYYY-MM-DD` in addition to the installer arguments
below. The date is the first date to discover, not a claim of verified league
coverage. Source selection is explicit; NBA failures never silently change the
NBA source contract or populate NBA gold tables with ESPN records.

This path invokes `scripts/espn_box_refresh.py` rather than Airflow. It reuses
the existing regular-season ESPN result/box validator and revision publisher in the fixed,
isolated `nba_gamebook_dev_espn_only` dataset. Discovery and summaries must agree
on final status, date, teams and score. Player shooting and rebound arithmetic
and player/team scoring totals are checked. Raw bytes, capture hashes, revision
inputs and per-run reports remain in private evidence storage.
Discovery uses an explicit bounded scoreboard request (`limit=1000`), and
acquisition gets at most three attempts with separate failed-capture evidence.
An empty HTTP 200 body is a source failure, never a zero-game day. Invalid
statistics still fail validation; acquisition retries do not relax those rules.
Existing healthy
facts are reused only after fresh capture and validation of identical revisions;
that check is retained in the run report and does not advance store observation
clocks or clear failed health. New or changed facts use the existing guarded
transactions. Unknown transaction outcomes retain their checkpoint and stop.

Each run discovers through yesterday in Eastern time and replays fourteen days
before the previous successful data-through date. One batch is capped at 31
dates; a longer gap fails visibly and requires an explicit bounded backfill.
Freshness reads accepted, NBA-mapped player box rows. Unresolved NBA player
identities are excluded and reported; ESPN game IDs stay provider-prefixed in
the existing opt-in local Ask adapter. Defensive and offensive rebounds and
fouls are retained as reported fields.
The provider's `--` minute marker remains null; it never becomes zero and does
not invalidate otherwise validated counting statistics. The query evidence
reports the number of appearances with unknown minutes.

After box collection, a separate `espn_game_mapping_weekly.py` stage requests
the PBP Stats season-game feed once, including older accepted games whose NBA
aliases remain unresolved. Existing date/home/away, native NBA ID season/phase,
exact score and bidirectional uniqueness guards apply. Publication binds to
current result/box revisions and never rewrites statistics or existing aliases.
The stage has a ten-minute process limit; individual captures retain their
existing timeout and size bounds. A denied, rate-limited, timed-out or invalid
feed is not retried in that run. It produces a degraded mapping stage, retains
prior mappings and checks, and does not erase accepted boxes. Uncertain warehouse
outcomes and identity conflicts still stop execution with their checkpoints.

Jobs shows mapped/unresolved/conflicting alias counts, source acquisition status
and time, and current-revision PBP Stats discrepancies, including final-score
differences and separately verified NBA identity. These appear in the current
observation and new run details. A preflight reads warehouse metadata and the
last recorded discovery outcome; opening Jobs only reads local files. Discovery
acquisition failures do not masquerade as fresh source captures. A live mapping-only
check writes its real acquisition outcome to `game-mapping.json` in the configured
state directory; it does not claim a complete box refresh.

The job does not claim NBA verification of ESPN statistics,
complete league coverage, play-by-play reconciliation, assist-pair validation
or deflections. Prior PBP facts and their dependency statuses remain unchanged.
Full-cohort rankings remain disabled in the local ESPN Ask adapter. Historical
queries continue through the season-specific NBA archive. This does not promote
ESPN data into the production NBA pipeline, change a deployed app, or change
the frozen private runtime.

Run the prepared worker manually and verify both source scopes before repeating
the installer with `--install`. The weekly scheduling, local lock, bounded
process limit, failure history and retry limits below apply to either explicitly
selected source. Only one configuration can own the weekly LaunchAgent label.

For a separate local read-only API review without changing an existing runtime:

```sh
PYTHON_DOTENV_DISABLED=1 python scripts/serve_season_review.py --project YOUR_PROJECT
```

It binds to loopback port 8013, explicitly selects the standalone reference
planner and local ESPN source, and supplies no model credentials. Test supported
deterministic queries through `POST /api/agent/ask?season=2025-26` and
`?season=2026-27`, for example `LeBron James points per game in the 2026-27 regular
season`. Historical stats APIs remain available with their explicit season.
This is an API review runtime, not a change to the frozen product runtime or
its configured providers. Stop it with Ctrl-C when finished.
For a direct browser-readable player-game preview, open
`/api/season-review?season=2025-26&player_id=2544` or
`/api/season-review?season=2026-27&player_id=2544`. These bounded reads return
source, snapshot, coverage and identity limitations alongside up to 82 rows.

## Preserve the previous season first

Follow the reviewed [season rollover procedure](operations.md#season-rollover)
before activation. The worker refuses to rebuild base serving tables containing
another season. Silver/gold builds select the current season, so running them
against an unarchived prior-season warehouse would remove its serving results.
Neither `--force` nor the installer bypasses this protection. Existing migration
freeze, archive publication, host adoption and deployment gates still apply.

## Prepare and check

Use explicit nonsecret configuration and existing Application Default Credentials.
No command in this workflow reads or sources `.env`; it does not invoke Make.
The Airflow executable must come from the separately configured Airflow runtime.
The worker and installer use the project's required Python interpreter.

```sh
PYTHON_DOTENV_DISABLED=1 python scripts/install_weekly_refresh.py \
  --project YOUR_PROJECT --bucket YOUR_BUCKET --season 2026-27 \
  --airflow /absolute/path/to/airflow \
  --state-dir /absolute/private/path/to/weekly-refresh

PYTHON_DOTENV_DISABLED=1 python scripts/weekly_refresh.py \
  --config /absolute/private/path/to/weekly-refresh/config.json --check
```

Preparation writes a private configuration and candidate plist to the state
directory. The check runs only a bounded, read-only warehouse scope query. It
cannot run ingestion. A blocked check records `season_rollover_required` in
`status.json`; do not enable writers until the separate rollover checks pass.

After the rollover and operational review, repeat the installer with the same
arguments and `--install`. Installation performs its own preflight before loading
the agent. It disables the superseded `com.nba-gcp.airflow-scheduler` LaunchAgent
so it cannot resume daily writes on login. The original plist and database are
preserved. Use a new state directory to prepare changes to an installation.

The calendar trigger uses macOS local time. Configure the Mac to the selected
timezone for the exact calendar trigger. An hourly due check also evaluates the
configured timezone in Python, including DST; it does no warehouse or source
work when a successful weekly slot is already recorded. This recovers missed
login/sleep runs within an hour of the Mac becoming available. The Mac must be
logged in, awake and online; this is not an always-on hosting guarantee.

## Execution and failures

- Only one run can hold the local lock. Multiple missed weeks coalesce into the
  latest due slot; the existing season-specific warehouse watermark and a 14-day
  replay buffer determine the source catch-up window.
- A weekly slot gets at most three automatic attempts, at least 24 hours apart.
  The underlying NBA requests have two attempts and ten-second request timeouts.
  The full DAG process has a two-hour limit; timed-out process groups are stopped
  and reaped. A manual `--force` ignores due-time limits but retains scope guards.
- The worker uses a separate Airflow database under its state directory, avoiding
  inherited stale jobs. It preserves the existing source contracts, quarantine,
  reconciliation, dbt checks and publication behavior.
- For NBA, success requires the DAG command to pass and the current-season fact table to
  contain observations no more than ten days old. This age check does not certify
  every expected game is present; source coverage remains a separate pipeline
  validation obligation.
- For ESPN, success requires the explicit result/box collector to pass and
  accepted, NBA-mapped current-season rows to be no more than ten days old.
  Observed coverage and excluded identity rows remain explicit.
- `status.json` distinguishes success, failure and blocked scope. A failed attempt
  retains the prior successful slot/date. Command output stays in the private
  per-run log; status records never include exception bodies or model keys.
- Injury and other existing supporting assets retain their existing optional
  failure rules. ESPN/PDF development DAGs remain separate and paused; deflections
  collection is not added by this cadence change. No model calls are made.

Inspect `status.json`, the latest run log and source-specific warehouse health
after a run. A source timeout is a failure, not a successful zero-game update.
This worker does not send external notifications or promise source uptime.

## Local Jobs page

### Lightweight ESPN coverage checks

The ESPN collector records the final game IDs returned for every requested date,
including successful zero-game dates. Failed/unknown dates never become empty
successes. Each game checks raw played appearances against normalized rows and
resolved identities, including zero-point players. Duplicate destinations and
unresolved, dropped or unexpected rows remain explicit. Metric input checks use
the existing semantic formulas; missing minutes block MIN/per-36 coverage while
complete points can pass. Play-by-play is not required for box-score checks.

Per-run `result.json` and `coverage.json` retain the details. The weekly worker
merges date checks into local `coverage.json` and puts a compact summary in
`status.json` and its run record. This keeps older verified dates when the
two-week correction window moves forward. Dates predating the first audit are
unknown until explicitly checked; previous success flags are not backfilled.
Failed refreshes do not advance the checked-through cutoff. When all dates are
accounted for, the retained appearance count is also compared with the observed
published mapped-row count.

The existing Jobs observed-coverage panel displays checked dates, accepted/listed
finals, missing dates/games, player-row issues and metric input blockers. Readiness
is scoped to the configured season start and checked-through date, labeled
**ESPN-reported**. It is a coverage prerequisite, not an NBA certification or a
ranking publication: existing source/qualification guards remain enabled. A
metric must still satisfy its minimum games/attempts and approved serving source
before ranks or percentiles can be shown. This change adds no new scheduler,
model calls, independent NBA fetches or warehouse writes beyond the existing job.

Run the monitor from this checkout with the same state directory as the worker:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHON_DOTENV_DISABLED=1 \
  python scripts/serve_jobs.py --state-dir /absolute/private/path/to/weekly-refresh
```

Open [localhost:8785/jobs](http://127.0.0.1:8785/jobs). Keep this command running
while using the page; stop it with Ctrl-C. This standalone local app does not
load application settings, `.env`, model providers or warehouse clients. It
binds to loopback and rejects nonlocal, forwarded and cross-origin access.
Do not expose it through a proxy or tunnel.

The page reads `status.json`, `config.json`, and up to 1,000 `runs/*.json`
records. Refreshing the page only rereads local records. It checks the installed
LaunchAgent configuration and whether its label is loaded and enabled, so a
candidate plist is never presented as an active schedule. The next weekly slot
uses the configured timezone and DST. If enabled, an earlier retry or catch-up
eligibility time is shown with the hourly due-check caveat. This reports local
scheduler state, not a guarantee that the Mac will be awake or online.

New worker executions record a unique identity, start/finish times, monotonic
duration, result, failure reason and pipeline stage timings. Preflight checks
appear separately and do not count as refresh attempts or successful ingestion.
No-op due checks and overlapping invocations do not create run records. A record
left running without a worker lock is marked interrupted, with its result
unknown. Earlier history and durations are not reconstructed.

Freshness means the latest observed current-season NBA gold fact date or
accepted NBA-mapped ESPN box date for the selected source, using the worker's
ten-day threshold. Prior-season observations appear separately.
An observation timestamp is shown because opening this page does not recheck
the warehouse. The age threshold alone does not prove every game is present.
Add repeatable `--also-state-dir` arguments to retain other prepared jobs in the
source-labeled job selector, including failed NBA attempts alongside an ESPN
job. The monitor does not claim separate run history or freshness for every
optional source or legacy DAG.

Run details can display the last 64 KB of that run's command log using the
existing diagnostic redactor. Redaction covers known patterns and does not
make raw logs safe to publish. Keep the state directory private and outside Git.
The monitor offers no write, retry, archive or activation controls.

## Validation

`python -m pytest tests/test_weekly_refresh.py` exercises weekly/DST boundaries,
missed runs, retry exhaustion, overlap, preserved success state, stale output,
configuration isolation, process cleanup and the archive guard. Fixtures do not
establish live NBA availability or warehouse publication acceptance.

`python -m pytest tests/test_jobs.py tests/test_weekly_refresh.py` also checks
monitor isolation, access restrictions, DST scheduling, retry eligibility,
interrupted/corrupt records, target-season freshness and bounded log access.

## Guarded real-provider ESPN review

A trusted local launcher can construct `ESPNReviewPolicy` from a bounded raw
ESPN warehouse capture and its SHA-256. Capture and validate the source before
installing the policy. This is an immutable review of actual data at the recorded
`snapshot_at`, not a continuously refreshed source. Recapture and restart explicitly
to review newer data; weekly refresh continues independently. Keep raw bundles,
source receipts and launchers private and outside Git.

The policy accepts only current-season regular-season evidence from the fixed
`nba_gamebook_dev_espn_only.espn_layer_player_box_scores` relation. It revalidates
retained box inputs, revision/dependency hashes, identity uniqueness, row caps and
partial coverage through the existing ESPN adapter. The payload/hash is part of
the cache identity; changing the bundle requires a new policy. Runtime requests
revalidate the pinned data and execute no warehouse SQL. Existing byte/time caps
apply to the operator's initial capture.

Opt-in requires both `ask_local_espn_dev=True` and this exact validated policy
type. Previous history, trace, serving-mode, model-selection, wire allowlist,
reservation-before-send, token and retry guards stay active. Other policies still
reject ESPN opt-in. Historical/mixed scope, outside-scope identity and arbitrary SQL
remain rejected, with no warehouse/provider fallback. Full-cohort rankings remain
unavailable; answers retain ESPN source and coverage limitations. This is no
production-source promotion or deployment.

Validate with `tests/test_espn_review_policy.py`, `tests/test_runtime_policy.py`,
`tests/test_scoped_source_policy.py`, `tests/test_scoped_ask_runtime.py` and
`tests/test_espn_dev_source.py`. These cover actual JSON/SSE routes with synthetic
SDK transport, integrity/scope failures and budget exhaustion. A separate main-UI
request using the real provider establishes live integration.
