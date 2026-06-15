# Public Service

The FastAPI service is intended for Cloud Run and serves HTML plus JSON from the
same process.

## HTML Routes

- `/`
- `/players/{player_id}`
- `/ask`
- `/performance`
- `/similarity-map`
- `/compare`

`/` redirects to `/ask`. `/visualize` redirects to `/performance` because the
old Visualize page has been removed.

## JSON Routes

- `/api/leaderboard`
- `/api/trends`
- `/api/analysis/latest`
- `/api/recommendations`
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
- `/api/health`

## Data Access

The service reads only from gold, agent, and metadata datasets. It is public
read-only for v1 and does not include auth.

Freshness is reported from the latest successful run in
`nba_metadata.pipeline_run_log` and evaluated against the configured freshness
threshold.

## Player and Compare Experience

Player pages render game logs, recent trend context, percentile summaries,
archetype context, and similar-player panels when the sample is stable.

The compare page supports direct `player_a_id` links and first-player search
when no initial player is provided. Compare responses can include a similarity
block with pair score, shared traits, and contrasting traits.

The performance page shows 2025-26 playoff player game rows against each
player's own season baseline, excluding players below one minute. It exposes
date and game filters, status sorting, minutes, FG%, FT%, 3PM, percentile ranges
for the selected row, and a 30-day game trend drawn from
`gold.recent_performance_workbench`. The default performance payload and health
status are prewarmed on app startup when `PERFORMANCE_CACHE_PREWARM_ENABLED`
is true.

## OpenAI Stats Agent

`/ask` adds an OpenAI-backed stats agent over curated warehouse outputs. Set
`OPENAI_API_KEY` to enable it. The blocking JSON endpoint is `/api/agent/ask`;
the streaming SSE endpoint is `/api/agent/ask/stream`.

Player resolution starts from `BQ_DATASET_AGENT.agent_player_search`, a
dedicated BigQuery table built by dbt for agent search. It contains qualified
player identity, searchable text, season averages, percentiles, trend state,
availability context, and a compact answer context. Deeper tool calls still use
allowlisted gold read models for game logs, trends, rankings, percentiles, and
similarity.

The agent can call allowlisted application tools for:

- player resolution
- game logs, including optional inclusive `start_date` / `end_date` filters
- trends, including optional inclusive `start_date` / `end_date` filters
- percentiles
- rankings
- similarity
- metric leaderboards

The agent does not receive BigQuery credentials and cannot run arbitrary SQL.

`OPENAI_AGENT_MODEL` defaults to `gpt-5.4-mini`, and `AGENT_MAX_TOOL_CALLS`
bounds one request's tool loop. The agent first builds a bounded query plan,
uses the deterministic router as fallback, resolves player mentions, and builds
an evidence bundle through allowlisted tools before asking the model to write the
final answer. Under-specified or ambiguous questions return clarification
options instead of calling more tools.

Every Ask request emits one JSON log line with `event_name:
agent_request_summary`, request id, route, confidence, model, tool calls with
arguments/result summaries/timing, token usage, latency, and final outcome. The
API also returns `request_id` in the JSON payload and `X-Request-ID` response
header.

Rate limiting uses `AGENT_RATE_LIMIT_REDIS_URL` when set, which should point to
Redis or Memorystore for horizontally scaled Cloud Run. The Redis path uses
`EXPIRE key seconds NX`, which requires Redis server 7.0 or newer (Memorystore
for Redis 7.x). Local and test runs fall back to an in-memory store. `AGENT_RATE_LIMIT_PER_MINUTE` and
`AGENT_RATE_LIMIT_DAILY` control per-IP ceilings, and
`AGENT_QUESTION_MAX_CHARS` caps prompt size before OpenAI is called.

OpenAI calls use `OPENAI_AGENT_TIMEOUT_SECONDS`,
`OPENAI_AGENT_MAX_RETRIES`, and `OPENAI_AGENT_RETRY_BASE_DELAY_SECONDS` for
bounded retries on transient 429/5xx/timeout failures. Raw exception text is
logged server-side only; clients receive generic availability or generation
failure messages.

Conversation memory is keyed by `conversation_id` and stores recent user/answer
turns in a pluggable in-memory backend for local use. `AGENT_CONVERSATION_MAX_TURNS`
caps replayed history; set it to `0` to disable replay memory. `AGENT_CACHE_TTL_SECONDS`
controls safe process-local caching for semantic catalog-derived metric lists
and player resolution.

Agent metrics are defined in `app/agent/semantic_catalog.yml`. Base metrics map
to curated gold fields. Derived metrics use safe arithmetic formulas over
approved stat keys, such as:

```yaml
formula: "pts + ast * 2"
```
