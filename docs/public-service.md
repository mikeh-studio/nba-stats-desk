# Public Service

The FastAPI service is intended for Cloud Run and serves HTML plus JSON from the
same process.

## HTML Routes

- `/`
- `/players/{player_id}`
- `/ask`
- `/performance`
- `/what-changed`
- `/similarity-map`
- `/compare`

`/` redirects to `/ask`. `/visualize` redirects to `/performance` because the
old Visualize page has been removed.

## JSON Routes

- `/api/what-changed` (`period=four_games|week`, `season_type=Regular Season|Playoffs`, optional `as_of=YYYY-MM-DD`)
- `/api/rankings`
- `/api/players/search`
- `/api/players/{player_id}`
- `/api/compare`
- `/api/performance/dates`
- `/api/performance/games`
- `/api/performance/initial`
- `/api/performance/players`
- `/api/performance/players/{player_id}`
- `/api/similarity-map`
- `/api/similarity-map/neighbors/{player_id}`
- `/api/agent/ask`
- `/api/agent/ask/stream`
- `/api/agent/history` (local-only history, empty unless enabled)
- `/api/health`

The retired `/api/leaderboard`, `/api/trends`, `/api/analysis/latest`, and
`/api/recommendations` endpoints return 404. Clients should use the retained
endpoints above; no stale compatibility responses are served.

## Data Access

The service reads only from gold, agent, and metadata datasets. It is public
read-only for v1 and does not include auth.

`/api/health` reports stats, injuries, and similarity independently from
`nba_metadata.pipeline_run_log`. Each asset includes its last successful
publication time, latest source data date, and last attempted publication status.
Skipped/no-change runs do not reset publication time or clear a previous failure.
Historical logs are supported; unavailable historical source dates remain unknown.

An optional asset failure produces `partially_updated` while valid core stats
remain available. The expandable data-status panel appears on every app page;
it identifies retained previous versions and their last successful refresh.

Freshness uses an explicit offseason window in `America/New_York`:

- `NBA_FRESHNESS_OFFSEASON_START` defaults to `2026-06-20`, after the last
  scheduled possible Finals game.
- `NBA_NEXT_REGULAR_SEASON_START` defaults to `2026-10-20`, the next opening day.
  Calendar source: [NBA key dates](https://gleague.nba.com/key-dates).
- During that window, existing data is an `offseason` archive, with
  `updates_expected=false` and `is_fresh=null`. Actual failed refresh attempts
  remain visible. Preseason and Summer League do not end the pause.
- Starting on opening day, `awaiting_refresh` allows the configured freshness
  threshold (default 36 hours) for the first ingestion. After that, old source
  data is overdue even if a recent pipeline run completed successfully.

This policy does not change the supported ingestion season or restart the DAG:
the current DAG still ends on July 1, 2026, and the SQL/app still serve 2025-26.
Season rollover must update those contracts separately. Refreshing the calendar
alone cannot make old-season data current. Update the two calendar settings
from the official schedule when rolling the pipeline forward.

## Player and Compare Experience

Player pages render game logs, recent trend context, percentile summaries,
archetype context, and similar-player panels when the sample is stable.

The compare page supports direct `player_a_id` links and first-player search
when no initial player is provided. Compare responses can include a similarity
block with pair score, shared traits, and contrasting traits. Supported windows
include last-game presets plus regular season, first-half regular season,
second-half regular season, and playoffs.

The performance page shows 2025-26 playoff player game rows against each
player's own season baseline, excluding players below one minute. It exposes
date and game filters, status sorting, signed P-Rating, minutes, FG%, FT%, and
3PM. Clicking a row opens a lightweight Player View modal from the loaded row
data; the full player page remains the detail destination. The default
performance payload is prewarmed on app startup when
`PERFORMANCE_CACHE_PREWARM_ENABLED` is true.

## Provider-Selectable LLM Stats Agent

`/ask` is an LLM-backed stats agent over curated warehouse outputs. Set
`OPENAI_API_KEY` and/or `ANTHROPIC_API_KEY` to enable the OpenAI API and Claude
API providers. Use `/api/agent/ask` for blocking JSON responses and
`/api/agent/ask/stream` for SSE streaming.

The request path is intentionally bounded:

1. Build a query plan, with deterministic routing as fallback.
2. Resolve player names from `BQ_DATASET_AGENT.agent_player_search`.
3. Gather evidence through allowlisted application tools.
4. Ask the selected provider/model to write the final answer from that evidence.

Allowed tools cover player resolution, game logs, trends, opponent splits,
percentiles, rankings, similarity, and metric leaderboards. The agent does not
receive BigQuery credentials and cannot run arbitrary SQL.

Trend questions preserve the user's requested window. `last 30 days` and
`past 10 weeks` anchor to the latest available game date in the supported
season, choose a game/week/month breakdown from the question, and compare
against the previous equivalent period unless the user asks for league baseline.
Structured trend evidence includes period summaries, bucketed rows, comparison
deltas, and chart payloads.

Operational controls:

- `OPENAI_AGENT_MODEL` and `ANTHROPIC_AGENT_MODEL` set provider defaults.
- `AGENT_MAX_TOOL_CALLS` bounds one request's evidence loop.
- `AGENT_RATE_LIMIT_PER_MINUTE`, `AGENT_RATE_LIMIT_DAILY`, and
  `AGENT_QUESTION_MAX_CHARS` protect public endpoints.
- `AGENT_RATE_LIMIT_REDIS_URL` enables Redis-backed limits for scaled Cloud Run;
  local and test runs use an in-memory fallback.
- Browser history stays in `localStorage`; optional server JSONL history is
  disabled by default and should stay under ignored `local_notes/`.

Governed metric queries use `app/agent/semantic_contract.yml`; see
[Metric semantics](semantic-contract.md). Legacy analytical tools retain
`app/agent/semantic_catalog.yml` presentation and alias metadata. Base metrics map
to curated gold fields. Derived metrics use safe arithmetic formulas over
approved stat keys, such as:

```yaml
formula: "pts + ast * 2"
```

Shooting efficiency is exposed as derived percentage metrics scaled to 0-100,
so their deltas read as percentage points: `fg_pct` (`fg_pct * 100`), `fg3_pct`
(`fg3m / fg3a * 100`), and `ts_pct` (`pts / (2 * (fga + 0.44 * fta)) * 100`).
A metric's `unit` (`count` or `percent`) drives formatting and keeps percentage
lines off the counting-stat chart axis. `plus_minus` records observed on-court point differential;
it is not isolated
player impact.

Each metric has a `tier` (1-4). A vague "stats" question resolves to the
default cohort — tiers 1-2, the traditional box score
(`catalog.default_metric_keys()`) — used for game logs and charts. The
"how have their stats changed" and "who did they struggle against" tools
instead default to a curated impact set (`catalog.analysis_metric_keys()`:
scoring, rebounds, assists, blocks, shooting efficiency, and plus-minus).

"Struggled against" is intentionally not a raw-volume sort: the toughest
opponent is the lowest composite **struggle score**, a weighted blend of
shooting efficiency and plus-minus expressed as z-scores against the player's
own window average. A team can therefore grade as the toughest matchup on
efficiency and impact even when raw points look fine, and the tool returns a
per-game drill-down (shooting line, TS%, plus-minus) for that opponent.
