# Local Ask review with ESPN dev box scores

Set `ASK_LOCAL_ESPN_DEV=true` only for a local review instance. This opt-in reads
accepted result/box revisions from `nba_gamebook_dev_espn_only`, overlays the
existing NBA player crosswalk, and uses the governed Ask calculators. All queries
are fixed, bounded and read-only. Production serving defaults remain unchanged.
The app rejects non-loopback, forwarded, or non-local-origin requests in this mode.
Run Uvicorn on `127.0.0.1` with `--no-proxy-headers`.

The adapter preserves provider provenance and exact box/result revisions. It
revalidates each box against its retained source input and checks payload hashes,
current acceptance and dependency identity. Malformed payloads or minute values
produce `invalid_evidence` rather than an unclassified parsing failure. Game keys use
`espn:<source_game_id>`; ESPN game IDs are never represented as NBA IDs. Only
mapped NBA player IDs enter the semantic identity field. Unmapped player-game
rows are excluded and counted in every successful answer's source-coverage note.
Minutes are converted from decimal-minute or minute:second cells.

Evidence completeness means retrieval of the accepted mapped dev sample, not a
certified full league season. It carries `cohort_complete=false`. Rankings and
percentiles are blocked, summary ranks stay null, and player overviews omit
league/position peer distributions. Explicit peer/baseline and similarity requests are refused
in this preview. Two named players can be compared using their captured appearances
in the same requested dates and phases. Their individual samples remain visible;
missing components withhold differences, and no league ranks, percentiles or
among-player distributions are produced. Small samples are descriptive and are
not certified full-season results. Current-season overviews and two-player
scorecards without an explicit phase use regular
season only and state that choice; explicit playoff requests are not narrowed.
Mentioning the NBA or league in a plain player overview does not request a ranking.
The bounded real-provider runtime still rejects browser-supplied player selections
and prior context. In that profile, enter both full names in a fresh question if
short names trigger identity confirmation; this change does not relax that guard.

Accepted player box-score assists remain usable independently of PBP. Pending
PBP counts are displayed; player assist totals do not certify assist-pair edges.
The adapter does not expose pair relationships, possession durations, injuries
or similarity results. Other pages retain their original serving source; the
Ask banner and answer notes identify the local dev preview.

Historical seasons prefer their existing archive relations. For the fixed
2025–26 season only, a missing archive permits a local read from the retained
base facts, with the same explicit season filter, snapshot, bounds and evidence
validation. Permission failures, invalid evidence and empty archives do not
trigger this route. Answers disclose the retained source; it does not create or
certify an archive. Older seasons still require their own archives.

This switch does not repair missing historical archives, publish production gold models, discover
NBA mappings, fetch new games, or activate an ingestion schedule. Run the manual
[correction worker](espn-corrections-dev.md) to refresh dev facts. The semantic
cache expires after the configured TTL; restart the local app to clear it sooner.

```bash
ASK_LOCAL_ESPN_DEV=true PERFORMANCE_CACHE_PREWARM_ENABLED=false \
  uvicorn app.main:app --host 127.0.0.1 --port 8012 --no-proxy-headers
```

Use the existing private environment for credentials. Keep raw snapshots and
request/response evidence under ignored `reports/`. Disable the switch to return
to normal serving without changing any warehouse rows.

Tests: `tests/test_espn_dev_source.py`, semantic source/overview suites, streaming
capacity tests, and frozen Ask endpoint evaluation. Live provider freshness and
actual UI/endpoint results must be verified separately from synthetic tests.

The adapter regressions exercise malformed and rehashed invalid source payloads,
explicit scope and identity, and JSON/SSE requests for ordinary player overviews
and unavailable league/similarity comparisons. Conversational comparison tests
also distinguish a second player from career, league and month baselines; this
does not add support for every baseline. Static review and fixture success do
not establish live provider interpretation or production readiness.
