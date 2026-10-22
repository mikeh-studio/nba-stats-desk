# Local gamebook retrieval

`scripts/fetch_gamebooks.py` probes a small explicit manifest against the official
NBA gamebook PDF host. It saves original response bodies and per-game attempt
records under ignored `reports/`. It does not extract assist relationships,
write to the warehouse, or schedule ingestion.

## Input and execution

Use a JSON array with one object per game. Required string fields are `game_id`
(a ten-digit string), `date` (ISO date), `season` (`YYYY-YY`), `away`, `home`
(official three-letter filename abbreviations), `away_name` and `home_name`
(full team names as shown in the gamebook). Dates and season must agree.
The script accepts at most ten unique games and constructs URLs only on
`https://statsdmz.nba.com/pdfs/`.

Using the configured application environment:

```sh
.venv-app/bin/python scripts/fetch_gamebooks.py \
  --manifest reports/gamebook-pilot/development-manifest.json \
  --runtime-label local-cli \
  --timeout 20
```

The manifest is a private operational input, not a checked-in fixture. The default
output is a new timestamped directory with a random suffix. `--output` can choose
a new directory inside `reports/`; existing output directories are refused.
`--runtime-label` records the caller's description, not verified deployment identity.

Each game gets one request without automatic retries or redirect following.
`--timeout` sets connect and socket-read timeouts (at most 60 seconds each).
The response size cap is 20 MiB; an elapsed-time check between read chunks also
stops slow transfers. These are not a strict whole-process deadline: PDF inspection
and a streaming read can take longer than a socket timeout. Use an external job
deadline when scheduling this tool. A new run is a new attempt, never an overwrite.

## Evidence and status

The output contains the frozen `manifest.json`, its hash in `report.json`, a
readable `report.md`, and a directory per game with `attempt.json` and a
content-hashed response file. Attempt records include URL, timestamps, HTTP status,
content metadata, byte count, hash, elapsed time and validation results. Failed
and interrupted responses are retained, with partial bodies marked incomplete.
Transport failures without any response have no fabricated HTTP status or hash.

| Status | Meaning |
| --- | --- |
| `available` | HTTP 200, readable PDF, first-page date/team matches and PBP markers |
| `http_blocked` | HTTP 401, 403 or 429 |
| `not_found` | HTTP 404 |
| `http_error` | Other non-200 response, including redirects |
| `network_error` | Connection or streaming exception; check recorded exception type |
| `invalid_pdf` | HTML, malformed, encrypted or unreadable PDF |
| `identity_unverified` | PDF readable but expected date/team text not verified |
| `pbp_unverified` | Identity matched but no recognized play-by-play marker |
| `size_limit` / `duration_limit` | HTTP 200 capture stopped at a transfer limit |

The manifest supplies the game ID; the document check corroborates date and team
names, not an embedded NBA game ID. PBP marker pages are discovery hints, not a
completeness audit. A readable text layer does not prove every event or table can
be parsed correctly. Scanned documents need separate inspection/OCR rather than
being treated as valid empty datasets.

Exit code 0 means every manifest game is `available`; 1 means at least one is not;
2 indicates invalid CLI input. A local success establishes only access from that
runtime at that time. Repeat the bounded probe from the intended ingestion runtime
before relying on scheduled access.

## Validation

```sh
.venv-app/bin/python -m pytest tests/test_gamebook_retrieval.py -q
.venv-app/bin/python -m ruff check scripts/fetch_gamebooks.py tests/test_gamebook_retrieval.py
```

Tests generate synthetic PDFs and fake HTTP responses. They cover content and
identity checks, HTTP/transport failures, incomplete captures, transfer limits,
manifest validation, and refusing to overwrite a run. See the
[public/private boundary](public-private-boundary.md) for artifact handling.

## Offline assist extraction and pair report

After retrieval, `scripts/parse_gamebooks.py` reads the frozen capture and local
identity snapshots. It makes no network requests and publishes no warehouse data.

```sh
.venv-app/bin/python scripts/parse_gamebooks.py \
  --capture reports/gamebook-pilot/<access-run> \
  --identity-dir reports/gamebook-pilot/identity-snapshots
```

The identity directory must contain `<game_id>-stats.json` snapshots in the
PBP Stats game-stats response format: matching date, season and home/away team
abbreviations, plus `stats.Home.FullGame` and `stats.Away.FullGame` entries with
`Name` and `EntityId`. Only identity fields are used; provider assist totals and
pair corrections are not imported. Keep the original capture metadata beside
these files. Active players must resolve uniquely against the PDF box-score
names; DNPs remain nonparticipants with unknown box-stat components.

The parser uses the installed `pypdf` text visitor and coordinate transforms.
It reads the final box-score columns by their headers, identifies PBP team
columns from their labels, and groups wrapped action text around clock positions.
It supports fractional sub-minute clocks and period-end summary spillovers.
Only made-shot syntax can create an assist. Foul officials, missed attempts and
free throws cannot produce assist links. Unrecognized actions or ambiguous
identities reject the game rather than becoming guessed relationships.

For each game it checks:

- The PDF byte hash matches the retrieval record and the frozen manifest matches
  the capture's expected games.
- Every active player's extracted AST, FGM, 3PM, FTM and points equal the final
  box-score row, and box-score player totals equal each team total.
- Cumulative scoring equals every parsed running-score entry and period-end score.
- Periods are contiguous with one start and one end marker each.

These are **same-document reconciliation checks**, not independently labeled
precision/recall, a claim that every event type is parsed, or warehouse validation.
The first implementation targets the native-text regular-season gamebook layout;
rotated pages/text and unsupported layouts fail closed. Scans, novel clock formats,
score corrections and overtime still need explicit examples and validation before
claiming support. Evidence bounding boxes describe padded action-column regions
in PDF points from the top-left, not exact glyph outlines.

Each new run produces `snapshot.json`, individual game audit JSON files,
`assists.csv`, `report.md`, and `index.html`. The HTML includes both teams' pair
counts, every counted play, per-player reconciliation and links to local copies
of the exact PDFs at their page numbers. Rejected games remain in coverage but
are excluded from pair summaries. Opponents have unequal game coverage and must
not be treated as a league ranking.

Extraction revisions include parser source hash/version, schema version,
`pypdf` version, document hash and identity-snapshot hash. Event IDs are stable
within that extraction revision, not across corrected documents or parser
changes. `run.json` holds timing separately from the deterministic snapshot.
Output directories must be new and inside ignored `reports/`. Exit code 1 means
at least one game was rejected.

Open `index.html` directly or serve that one output directory on localhost:

```sh
.venv-app/bin/python -m http.server 8769 --bind 127.0.0.1 \
  --directory reports/gamebook-pilot/<pairs-run>
```

Synthetic tests exercise complete PDF extraction and replay, box-score mismatch
rejection, document tampering, wrapped assists, period endings, identity ambiguity,
short shot descriptions, foul/miss exclusion and HTML escaping:

```sh
.venv-app/bin/python -m pytest tests/test_gamebook_assists.py tests/test_gamebook_retrieval.py -q
```

The next local stage is the [revisioned storage converter](gamebook-storage.md),
which separates scoring events and assist relationships, preserves evidence,
and exercises replay/correction/failure publication before warehouse integration.
