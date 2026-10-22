# ESPN dev ingestion and PDF comparison

The independent `nba_espn_dev` DAG captures current-season ESPN game metadata,
player box scores and assist relationships. It starts paused. It does not write
production bronze/silver/gold, NBA API ingestion watermarks or `pipeline_run_log`.
The implementation consists of [source normalization](../scripts/espn_sources.py),
[the worker](../scripts/espn_ingestion.py), [publication](../scripts/espn_store.py)
and [the DAG](../dags/nba_espn_dev_dag.py).

For a manual ESPN-only PBP experiment while PDFs are unavailable, see
[play-by-play alternatives](play-by-play-alternatives.md). That separate local
probe does not change this DAG's PDF acceptance gate or publish warehouse facts.
The separate [ESPN-only dev publisher](espn-only-dev.md) can load reconciled facts
with independent mapping/PDF statuses into its own dev dataset; it also leaves
this DAG's contract unchanged.

## Sources and scope

Discovery requests the public ESPN scoreboard one league-local date at a time.
The default daily window is today and the preceding six days in America/New_York;
explicit windows are limited to seven days. The season is explicit; ESPN's
ending-year season value must match the configured NBA season. Only final
regular-season games are eligible. In-progress games are counted separately.
Returned duplicate IDs, conflicting dates/teams/statuses and disappearance of
previously registered finals within the window fail discovery. This denominator
is observed ESPN coverage, not independently certified league-wide completeness.

All observed finals are registered before the work cap (default 20, maximum 50).
The shared [PDF publication policy](../scripts/gamebook_publication.py) delays
selection until the next calendar day in America/New_York. Same-day finals stay
in coverage, but are reported as `not_yet_due` with their `expected_on` date and
are not passed to the combined ESPN/PDF worker. Thus this pilot's box-score
publication also waits until the next day. Discovery still observes today's
games; it does not omit them from the denominator. `deferred` counts all
unselected finals, while `deferred_by_limit` excludes publication waits.

This is a one-day operational expectation, not an NBA availability guarantee.
For example, October 21 games are eligible from midnight Eastern on October 22;
the next scheduled run processes them. Timeouts/404s after eligibility retain
normal failure and retry behavior. Explicit manual `process` and PDF diagnostic
commands can still test early availability. Existing failures are not cleared,
and the DAG remains configured to start paused.

Never-attempted games come first, then oldest checked games. Every selected game
is re-fetched, so corrections inside the seven-day window are revisited. Older
failures/corrections require an explicit bounded historical window; this pilot
does not claim an automatic whole-season correction sweep.

ESPN is an alternative provider, not the NBA Stats API. Requests have time and
response-size bounds, do not follow redirects and retain error responses. The
public endpoint's accessibility does not establish production support or data
redistribution rights. No paid account is required by the tested endpoints.

## Identity mappings

ESPN game/player IDs remain in their own fields. NBA IDs are nullable and never
filled with ESPN IDs. The PBP Stats season-game list supplies candidate NBA game
IDs. A mapping requires a unique date/home/away match, matching final score and
an NBA ID with the correct season and regular-season prefix. Both directions of
the ESPN/NBA game crosswalk are unique. Conflicts fail that game's attempt.

Verified game mappings are retained in `espn_mappings` with source evidence. A
mapping lookup outage remains recorded; a cached mapping can be reused only when
its date, teams and score still agree. Unresolved games remain `pending_mapping`.
A new live mapping cannot silently change a previously published NBA game ID.

### Reviewed NBA page and PDF verification

[`verify_espn_game_ids.py`](../scripts/verify_espn_game_ids.py) supports a bounded,
manual dev recovery path for up to 20 candidates. It requires captured ESPN
summary bytes and metadata, an operator-reviewed official NBA page observation,
and the actual downloaded PDF with its capture metadata. An NBA page URL alone
or a successful HTTP response is insufficient. The page supplies the NBA ID;
the PDF does **not** independently supply that ID.

The verifier checks the page title/date/matchup and URL against the ESPN final
game header, then verifies PDF hashes, date, explicit VISITOR/HOME headings and
printed final totals. PDF player totals must reconcile with its team totals, and
both final scores must agree with ESPN. Wrong scope, unavailable PDFs, changed
bytes, duplicate candidate IDs or score differences reject verification.

Private bundles contain `candidates.json`, an array of records with
`source_game_id`, `nba_game_id_candidate`, `season`, `date`, `away`, `home`,
`summary_attempt`, `pdf_attempt` and `page`. Evidence paths are relative to the
bundle. The `page` object contains `method: operator_reviewed_web_tool`,
timezone-aware `observed_at`, exact `title`, official `url`, `capture` path and
`sha256`. The title and URL must appear together in the captured web-tool text.
This is reviewed evidence, not an independently authenticated or automated NBA
page capture. Summary/PDF directories use the existing downloader metadata
formats. Keep all real inputs, reviews and outputs under ignored `reports/`.

```bash
.venv-app/bin/python scripts/verify_espn_game_ids.py \
  --bundle reports/game-id-review/bundle \
  --output reports/game-id-review/local-check

# Archive evidence in GCS and insert verified mappings into the guarded dev store.
.venv-app/bin/python scripts/verify_espn_game_ids.py \
  --bundle reports/game-id-review/bundle \
  --output reports/game-id-review/dev-check --publish-dev
```

Each output directory must be new. Exit 2 means at least one candidate was
rejected; partial verification never appears as total success. With
`--publish-dev`, evidence and rejection details are archived before any mapping
insertion. Existing insert-only transactions enforce uniqueness in both ID
directions; uncertain BigQuery outcomes retain the worker's query checkpoint.
Game mappings publish individually, not as an all-or-nothing multi-game batch.
This command does not replace facts, clear failed/pending health, validate assist
relationships, or change Airflow scheduling. It is not wired into the daily DAG;
unattended NBA page discovery remains a separate source-access problem.

For recovering an already-captured successful lookup, discovery supports an
explicit `--mapping-capture` directory containing the original `attempt.json`
and `response.body`. The URL/season, HTTP status and body hash must validate.
The original capture metadata is preserved and archived again; it is not labeled
as a new live response and never substitutes for live ESPN game discovery.

Player crosswalks use unique normalized full-name matches against the maintainer's
`nba_api` catalog, pinned by immutable Git commit and SHA-256 in
[`espn_player_catalog.json`](../contracts/espn_player_catalog.json). The worker
downloads only that allowlisted source, verifies the hash and reads the `players`
literal with `ast.literal_eval`; it never executes downloaded Python or upgrades
the installed package. Source bytes and the pin are retained per attempt.

An old spelling from the installed catalog can remain an alias only when its NBA
ID also exists in the pinned catalog. Alias rows and the installed package version
are captured. This preserves source-backed name changes without fuzzy suffix
stripping. Ambiguous names stay unresolved; conflicts with existing IDs fail.
PDF comparison still requires every active player to map and the participant/team
sets to agree. This is name-based resolution, not a provider-certified crosswalk.

The insert-only `espn_player_mappings` table stores provider ID, NBA ID, normalized
name, method, catalog hash and evidence URI. A conflict in either ID direction
rejects the whole batch. `espn_identity_attempts` records publication, no-op and
rejection. Fact publication cannot silently replace or null an accepted player
mapping. Failed PDF retrieval does not erase independently resolved identity.

`espn_resolved_player_box_scores` and `espn_resolved_assists` overlay the crosswalk
on existing immutable facts and expose identity evidence. They preserve the
original fact revision, health and PDF status. Resolving IDs does not promote
`pending_mapping` to PDF-validated or clear a failed game. The original
`espn_current_*` views continue to show IDs as they were at fact publication.

## Validation and evidence

Player box rows retain provider IDs, NBA IDs when resolved, names, teams,
participation flags and stats. DNP stats remain null. Checks include field-goal
arithmetic, made/attempted bounds, rebounds, player-to-team scoring totals, final
play-by-play score, scoring-event totals, unique play IDs and complete periods.

Assist roles are resolved from explicit assisted-shot text and checked against
participant IDs and same-team active players. Participant array ordering is not
assumed to mean scorer/assister. Every player's extracted assist count must equal
the ESPN box score. Unsupported text or inconsistent totals rejects the game.

Mapped games download the official statsdmz PDF and use the existing parser and
full storage validator. Before acceptance, the pipeline compares each active
player's points, field goals, threes, free throws and assists. It then requires
an exact multiset match of period, game-clock seconds, team, points, scorer NBA
ID and assister NBA ID. Matched events retain the PDF event ID and page link.
This is a cross-representation check; both sources may share underlying data.
It does not establish independent human-labeled accuracy.

For a manual comparison against an already downloaded gamebook, `process` also
accepts `--pdf-capture <game capture directory>`. It verifies the original HTTP
status, exact game scope, official URL and content hash, then preserves the
original retrieval timestamps in the evidence. The same full parser and event
comparison run again. This is explicitly frozen evidence, not a successful live
PDF access test. The scheduled DAG does not supply this option.

The existing PDF parser supports regulation games only. Unmapped identities,
unavailable PDFs, overtime or mismatches do not become zero-assist validations.
Mapped-game failures retain raw ESPN/PDF evidence but do not replace accepted
facts. Games with missing mappings can publish reconciled ESPN-only facts with
`pending_mapping`; their Airflow task remains unsuccessful. A failed replacement
retains the previous facts with visible failed health. A no-op does not clear it.

## Dev storage and correction contract

Default destination: `nba_gamebook_dev_espn`, with the existing private GCS prefix
`gamebooks/dev/<dataset>/`. ESPN targets must start with `nba_gamebook_dev_espn`;
the underlying gamebook publisher additionally enforces its dev dataset naming,
location and non-public bucket checks. Setup reuses the existing gamebook storage
schema checks, but ESPN records are written only to separate `espn_*` tables.

- `espn_coverage`: observed final games, current revision and health.
- `espn_revisions`: immutable normalized JSON snapshots and evidence URI.
- `espn_attempts`: publication/no-op/rejection outcomes and evidence.
- `espn_mappings`: evidence-backed ESPN-to-NBA game crosswalk.
- `espn_player_mappings` and `espn_identity_attempts`: player crosswalk and its audit.
- `espn_current_games`, `espn_current_player_box_scores`, `espn_current_assists`
  and `espn_current_pdf_matches`: typed, read-only current projections.
- `espn_resolved_player_box_scores`, `espn_resolved_assists`: identity-enriched
  projections with unchanged fact health and PDF status.

For this small pilot, revisions store complete normalized snapshots; views expose
analysis rows with numeric columns and null semantics. Raw response bytes and
PDFs remain private immutable GCS objects. Retrieval times and unrelated ESPN
news/media are excluded from revision identity. The normalized semantic payload
is hashed, so identical observations are no-ops. Normalization behavior is
versioned by the payload schema; runtime/catalog evidence is archived separately.

A single BigQuery transaction inserts the new revision and success audit and
advances the current pointer. A control-row mutation serializes writers; the
expected previous revision is checked inside the transaction. Stale publishers,
historical rollback and validated-to-unvalidated downgrade are rejected. Rejected
candidates and confirmed transaction failures are audited separately. Unknown
query outcomes keep the durable GCS job checkpoint and stop execution for
inspection; they are never assumed to be rolled back. Query cost is bounded at
1 GB billed per job. Use one automated writer for a destination.

## Run and test

Use the existing app environment and private `.env` credentials. Configure:

```dotenv
ESPN_DEV_DATASET=nba_gamebook_dev_espn
ESPN_MAX_GAMES_PER_RUN=20
# Optional worker interpreter override for Airflow:
# ESPN_PYTHON=/absolute/path/to/.venv-app/bin/python
```

The source provider does not need an API key for these tested reads; GCS/BigQuery
still require the existing dev application-default credentials. No IAM changes
are made by this workflow.

```bash
.venv-app/bin/python scripts/espn_ingestion.py discover \
  --season 2026-27 --start-date 2026-10-20 --end-date 2026-10-21 \
  --output reports/espn-discovery-unique
```

To enrich already accepted dev games without re-downloading their PDFs:

```bash
.venv-app/bin/python scripts/espn_ingestion.py refresh-player-mappings \
  --season 2026-27 --output reports/espn-player-mappings-unique
```

This command captures the pinned catalog, reads bounded current-season snapshots,
archives its inputs and publishes only the identity crosswalk. Repeating it is a
no-op when the mapping set is unchanged. New ingestion attempts use the same
catalog automatically. To refresh the pin later, inspect the maintainer's catalog
commit, verify the downloaded file hash, compare old mappings and run the identity
tests before changing the pin. Never point the worker at mutable `master` content.

`result.json` contains the bounded work items. Pass each entry to `process
--game-json '<entry>' --output <new private directory>` or use Airflow's mapped
worker. Exit 0 requires accepted, healthy, PDF-matched data; exit 1 is retryable
source/runtime failure; exit 2 is validation rejection or incomplete mapping;
exit 3 requires query-outcome inspection. Partial ESPN facts may therefore exist
when a task/run is failed. Inspect health and PDF status, not row presence alone.

```bash
.venv-app/bin/python -m pytest tests/test_espn_pilot.py -q
.venv-app/bin/python -m pytest tests/test_espn_identity.py -q
AIRFLOW__CORE__DAGS_FOLDER="$PWD/dags" \
  .venv-airflow/bin/python -m pytest tests/test_espn_dag.py -q
# Real cloud writes, synthetic sources, fresh separate dev test dataset:
.venv-app/bin/python tests/espn_cloud_scenarios.py \
  --output reports/espn-cloud-scenarios-unique
.venv-app/bin/python tests/espn_identity_cloud_scenarios.py \
  --output reports/espn-identity-cloud-unique
```

The cloud scenarios cover initial publication, identical replay, a valid synthetic
correction, invalid replacement, failure inside the transaction, retained failure
health and rejection of an older revision. Synthetic corrections are not presented
as changes actually made by ESPN or the NBA.

Before scheduling, require complete expected-game coverage, resolved mappings,
PDF validation and healthy current data for the pilot window, plus a successful
manual Airflow execution. A bounded successful mapped sample is insufficient
when other expected finals remain missing or unvalidated. Use isolated Airflow
metadata for tests; inspect shared scheduler state and existing runs before starting it. Production
season rollover remains separate from this dev dataset. Do not use `make airflow-unpause`, which targets production.

Run manual ingestion and the DAG serially against the same destination. Concurrent
writers can cause a confirmed BigQuery transaction abort; this should remain a
failed attempt and be retried after the other writer finishes. The DAG serializes
mapped ingestion tasks and limits active runs, but cannot stop an unrelated CLI
process from writing to its dataset.
