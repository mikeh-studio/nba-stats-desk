# Public Service

The FastAPI service is intended for Cloud Run and serves HTML plus JSON from the
same process.

## HTML Routes

- `/`
- `/players/{player_id}`
- `/compare`
- `/visualize`
- `/ask`

## JSON Routes

- `/api/leaderboard`
- `/api/trends`
- `/api/analysis/latest`
- `/api/recommendations`
- `/api/rankings`
- `/api/players/search`
- `/api/players/{player_id}`
- `/api/compare`
- `/api/agent/ask`
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

## OpenAI Stats Agent

`/ask` adds an OpenAI-backed stats agent over curated warehouse outputs. Set
`OPENAI_API_KEY` to enable it.

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
bounds one request's tool loop.

Agent metrics are defined in `app/agent/semantic_catalog.yml`. Base metrics map
to curated gold fields. Derived metrics use safe arithmetic formulas over
approved stat keys, such as:

```yaml
formula: "pts + ast * 2"
```
