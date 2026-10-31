# Offline identity and source observations

Phase 1 normalizes explicitly scoped retained 2025–26 NBA archive games and
2026–27 ESPN summaries into a local SQLite identity registry and immutable
observations. Normalization has no network client, cloud publisher, credential
loader or application-reader registration. Existing source guards, queries,
runtime settings and application behavior remain unchanged.

The implementation is in `app/data/unified/`. The historical command is
`scripts/normalize_historical_observations.py`. The provider-aware command is
`scripts/normalize_unified_observations.py`. Both consume retained local JSON;
neither queries a running app or warehouse to populate missing inputs.

## Identity

Game UIDs are opaque `g_` plus 32 UUID hexadecimal digits. The registry allocates
one transactionally for a validated native provider game ID and explicit
league/season/phase/date/team scope. Native NBA IDs retain leading zeroes in
their own namespace. Reuse the same durable registry file across reruns and
candidate builds; a new empty registry creates new identities.

Aliases attach to existing UIDs through explicit reviewed evidence. Date/team
agreement alone cannot merge overlapping provider games. Conflicting IDs are
rejected. The registry also checks historical scopes to hold observations of
potentially duplicate rescheduled games. Date changes require a reviewed scope
revision with the expected prior scope version; immutable observations retain
their original scope. Changes to teams, phase or season are not supported by
that date-revision method.

Player IDs remain NBA person IDs. Native player aliases require explicit ID
bindings and evidence. Names and current rosters do not resolve identity.
Several players may share a name, and a player's team is recorded at each game.
The appearance key is `(game_uid, player_id)`. Reviewed aliases never rekey facts.
Each successful CLI run exports an inspectable `identity-catalog.json` with a
content version and the alias/scope evidence. It is a local snapshot, not an
active release or permission to serve those observations.

`correct_person_alias` appends a reviewed ESPN correction against the expected
prior mapping version. Original aliases and old facts remain immutable. ESPN
observations record each participant's ID and mapping version. Current reads
hold affected facts after identity or date/scope corrections until the whole
game is explicitly reprocessed. NBA person IDs cannot be reassigned. Conflicting
game alias reassignment remains held for adjudication, never an automatic merge.

## Retained ESPN observations

Phase 1 also accepts explicitly scoped 2026–27 ESPN regular-season summaries.
The [ESPN schema](../contracts/unified_data/espn-retained-game.schema.json) uses
`espn_retained_game/1`, an accepted `game`, original UTF-8 bytes in `source_body`
and their checksum/original capture receipt in `source`. The outer receipt hashes
the derived envelope separately. Unknown envelope acquisition time stays null;
original source time never becomes replay/export time.

The adapter reuses existing ESPN result/box validators and verifies their exact
revision chain. Shared payload projection is in `scripts/espn_payload.py`; legacy
imports still expose `espn_only_store.source_input`. Result/box-only validation
avoids importing cloud or dotenv through PBP; existing PBP behavior is preserved.

Participants require pre-registered explicit ESPN-to-NBA aliases. Bootstrap them
separately with the existing pinned-catalog crosswalk and retained unique-name
or reviewed duplicate-name evidence. Ambiguous/missing bindings remain unresolved
and hold the whole game; the observation adapter never resolves names itself.
Do not seed aliases from an unreviewed model guess or current team roster.

Boolean `didNotPlay` flags determine appearances. DNP rows remain participant
evidence; played zero-minute rows still count. Counts use the same arithmetic and
bounds checks as NBA observations. Whole-minute strings and `--` are supported;
other formats require a registered precision policy. Missing minutes remain null;
reported zero is tagged `reported_zero`. Overtime remains unknown in this profile.
Provider-declared completeness means parity with the retained participant list,
not independent proof of every player or every season game. Game aliases and
box acceptance never confer PBP or PDF capability.

## Retained input contract

The [input schema](../contracts/unified_data/nba-archive-game.schema.json) describes
one game bundle, with `schema_version: nba_archive_game/1`, `provider: nba_api`,
`season: 2025-26`, `phase: Regular Season`, `game` and `appearances`.

`game` uses archive `dim_game` fields: `game_id`, `game_date`, `season`,
`home_team_abbr`, `away_team_abbr`, `home_team_pts`, `away_team_pts`, and optional
nullable `is_overtime`. The phase declaration must come from the retained export
scope; a native ID pattern alone is insufficient phase authority.

Each appearance uses `fct_player_game_stats` field names, including game/player
IDs, date, season/type, team/opponent, HOME/AWAY designation, reported minutes,
and box counts. Names, provider percentages, derived fantasy values and load
timestamps are not canonical statistical inputs. Ratios must eventually be
derived from compatible components rather than copied provider averages.

The adapter requires both teams and matching player/team scoring totals. It
validates required scoring components, integral finite counts, source bounds,
made/attempted splits and known rebound arithmetic. Missing optional metrics
stay null. Fractional minutes are rejected by this specific audited whole-minute
profile instead of truncated. Reported zeros remain zero and played appearances
still count toward GP; zero-minute origin is explicitly unverified. No DNP is
inferred from a missing row or zero minute. Unknown overtime stays unknown.

## Membership and corrections

An ordinary archive bundle produces `observed_sample`. Matching team totals
cannot establish participant completeness: omitting a zero-point player can
preserve those totals.

An optional `nba_participants/1` membership manifest declares all participants,
their exact NBA IDs, teams and explicit played flags, with evidence receipt,
original timezone-aware capture time, matching scope and `input_game_hash`.
The hash binds the canonical game bundle with `membership` omitted. The
manifest's played participant set must exactly equal normalized appearances;
explicit nonparticipants remain in its evidence and do not become appearances.

This is a trusted producer's retained participant attestation. The offline
adapter verifies its bindings and membership consistency, not the authenticity
or completeness of the upstream capture. It cannot reconstruct that lineage
from an aggregate archive audit. The manifest supports the local
`provider_declared_complete` state; it never grants league completeness or
runtime eligibility. Synthetic evidence stays labelled synthetic.

Initial valid observed samples can be recorded. Replacing a current game
requires a complete participant manifest, the expected prior fact revision,
and all validation checks. Whole games replace atomically; rows are never
appended into aggregate totals. An incomplete or invalid replacement leaves the
prior pointer and identity state unchanged. Replaying an old historical fact
revision cannot roll back the current game. An explicit rollback/adjudication
workflow is outside this increment.

### Reviewed historical played dates

An explicit `nba_archive_date_review/1` envelope can resolve a retained game-date
conflict. It carries the exact original archive bytes/receipt and a
`reviewed_nba_played_date/1` review. The review binds the native game ID, original
checksum, old date, played date, season/phase/teams, and a retained official NBA
game-page observation. The adapter checks the exact NBA URL, matchup/title,
displayed calendar date, observation hash and timezone-aware review clock. Every
player-log date must agree. Only the projected game date changes; all ordinary
archive identity, scope, arithmetic, minute and membership checks still run.

This narrow authority method is labelled
`operator_reviewed_nba_game_page_web_tool_v1`. It retains a trusted reviewed page
observation, not an authenticated HTTP capture or proof that a 403 succeeded.
Hashes detect changed retained evidence; they do not authenticate its origin.
Original acquisition clocks stay unchanged. The review clock describes the page
review, not original NBA statistical acquisition. Raw pages/reviews stay private.

The normalized observation retains review lineage and a versioned date basis.
This does not confer participant completeness, rankings, PBP or PDF capability.
An ordinary conflicting archive bundle continues to fail; there is no automatic
preference for player-log dates. Already accepted fact replacement still needs
its expected revision and complete participant evidence; this date-only review
does not bypass those requirements or rebind a participant manifest.

`resolve_date_quarantine` appends a resolution linking the exact rejected input
and receipt to the current reviewed observation. The rejected original and reason
remain immutable. `unresolved_quarantines` excludes resolved entries; repeated
resolution/replay is idempotent. Exporting a repaired offline copy does not repair
the original warehouse or switch an application reader.

## Immutable observations

The [normalized schema](../contracts/unified_data/observation.schema.json) records
scope, UID, team-at-game appearances, box components, minute precision,
membership level, provenance and conservative capability flags.

The fact revision hashes normalized business data, dependency versions and
membership level. Input row order, load timestamps and capture timestamps do
not enter this statistical hash. A separate observation ID binds the fact
revision to the exact retained input checksum, native ID, participant evidence
and original capture time. Revalidation can add evidence without changing facts
or rewriting a previous receipt's clock.

SQLite transactions enforce uniqueness and compare expected revisions. History
tables have update/delete rejection triggers, and reads recheck content hashes.
Identical replay inserts neither duplicate appearances nor duplicate audit
events. Local log/summary helpers inspect the selected offline revisions and
report metric-valid counts. They are not connected to Ask or Player.

Every observation sets `runtime_eligible`, `league_ranks`, `exact_seconds` and
`pdf_corroborated` to false. No serving release or active source policy is created.

NBA and ESPN maintain separate current pointers per UID. One provider cannot
replace another's revision. Default inspection refuses overlapping provider
games to prevent double contribution; explicit provider selection is required.
Automatic provider precedence is Phase 2 work. Offline `player_log` and
`player_summary` accept optional season/provider filters and expose source/season
metadata. These helpers remain disconnected from Ask and Player.

## Run

Keep input captures, manifests, SQLite files and outputs private and outside Git.
Create `manifest.json` in the input bundle:

```json
{
  "schema_version": "nba_archive_bundle/1",
  "captures": [
    {
      "path": "game.json",
      "sha256": "SHA256_OF_ORIGINAL_FILE_BYTES",
      "captured_at": null,
      "kind": "archive_snapshot"
    }
  ]
}
```

Use an actual lowercase SHA-256 value. `captured_at: null` preserves unknown
original acquisition time; export/load time must not be substituted. Capture
kinds for box input are `archive_snapshot`, `provider_capture` or
`synthetic_fixture`. Identity-only evidence cannot authorize statistical input.
A capture can additionally specify `expected_revision`
when submitting a reviewed correction.

```sh
PYTHON_DOTENV_DISABLED=1 python scripts/normalize_historical_observations.py \
  --bundle reports/historical-input \
  --store reports/unified-local/registry.sqlite \
  --output reports/historical-normalized-first
```

The store and new output directory must remain under private `reports/`; the
store must be outside the retained input and output directories. Only bounded
relative JSON captures are read. Input checksums, path traversal, duplicate
keys, duplicate games, invalid numeric values and size bounds are checked.
A multi-game batch is transactional. JSONL and report files are diagnostic
exports after commit, not a serving publication; an export failure can be
recovered by replaying against the same store with a new output directory.

For either provider, the same capture-list structure also accepts
`unified_retained_bundle/1`. Use the pre-seeded persistent registry for ESPN:

```sh
PYTHON_DOTENV_DISABLED=1 python scripts/normalize_unified_observations.py \
  --bundle reports/retained-input \
  --store reports/unified-local/registry.sqlite \
  --output reports/normalized-first --allow-held
```

Without `--allow-held`, an invalid game rolls back the whole batch. With it, that
game rolls back its own identities/facts and is retained in immutable quarantine
with payload, checksum, receipt and reason. Reports identify every hold; the CLI
exits **2** if any game is held. Replay does not duplicate quarantine. Accepted
games remain queryable; holds are never reported as a fully successful refresh.

Validation includes synthetic failure/correction cases and actual retained-row
replay. Complete-scope certification still requires game/participant authority
evidence. Per-game normalization and retained inventories do not independently
establish a complete league cohort. Acquisition is a separate authorized step;
no warehouse read or write is implied by these offline commands.
