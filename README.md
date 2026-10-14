# NBA Stats Desk

[![CI](https://github.com/mikeh-studio/nba-stats-desk/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/mikeh-studio/nba-stats-desk/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Explore NBA players, ask basketball questions, and investigate changes in performance.
NBA Stats Desk connects a read-only web application to a governed data platform,
with explicit season scope, evidence-backed answers, and interactive charts.

Under the hood it follows a modern AI data stack: an ELT pipeline with data
contracts, dbt models and a semantic layer in BigQuery, and a tool-calling agent
that answers from governed metrics instead of writing its own SQL.

![Ask: Jalen Brunson's 2025–26 playoff performance, with an overview, key takeaways, and percentile-ranked metrics against his regular season](docs/images/ask.jpg)

## Explore

| Experience | What it does |
| --- | --- |
| **Ask** (`/ask`) | Resolves players and dates, gathers evidence through bounded tools, and presents answers with comparison context and charts. See the [Ask contract](docs/ask-workspace.md). |
| **Players** (`/players`) | Browse and search players, then open profiles with trends, game logs, archetypes, percentiles, and [research breakdowns](docs/pages.md#players-and-research-breakdowns). Reached from the header search. |
| **Performance** (`/performance`) | Review each playoff game against every player's own season baseline. |
| **Trending** (`/what-changed`) | Compare top performers and production changes across recent game windows. See [Trending](docs/pages.md#trending). |
| **Profiles** (`/similarity-map`) | Explore a 3D projection of statistical profiles, compare grouping models, and trace a player's nearest matches. See [profile similarity](docs/pages.md#statistical-profile-similarity). |

<table>
  <tr>
    <td width="50%"><img src="docs/images/performance.jpg" alt="Performance: June 13, 2026 playoff player games compared with each player's season baseline"></td>
    <td width="50%"><img src="docs/images/trending.jpg" alt="Trending: production change versus minutes change for the last four team games"></td>
  </tr>
  <tr>
    <td align="center"><b>Performance</b>: playoff games against season baselines</td>
    <td align="center"><b>Trending</b>: production and minutes changes</td>
  </tr>
  <tr>
    <td colspan="2"><img src="docs/images/profiles.jpg" alt="Profiles: Jalen Brunson's nearest statistical matches in the 3D archetype space"></td>
  </tr>
  <tr>
    <td colspan="2" align="center"><b>Profiles</b>: archetype space and nearest matches</td>
  </tr>
</table>

Pages default to the latest supported season, currently **2025–26**. Ask supports
explicit historical questions within the available **2023–24 through 2025–26**
coverage. Availability varies by question and source; teammate comparisons describe
observed differences and do not establish causation.

## How Ask answers a question

1. **Resolve scope.** Player names, dates, season, and regular-season/playoff
   phase are resolved explicitly. Ambiguous names ask for clarification instead
   of guessing.
2. **Gather evidence.** The planner chooses from allowlisted, read-only tools with
   bounded queries. Models never write or run SQL.
3. **Calculate in code.** Statistics follow the [semantic contract](docs/semantic-contract.md):
   pooled shooting ratios, valid-game denominators, and missing games kept
   separate from zero production.
4. **Answer from evidence.** Prose, tables, and charts share the same evidence.
   A chart model may only pick among server-built candidates; application code
   supplies every value.
5. **Decline what the data can't support.** For example, *"How did Jalen Brunson
   play in the 2026 NBA Finals?"* returns an explanation that individual playoff
   rounds aren't available yet. It does not quietly substitute the full playoff run.

## The stack

```text
NBA API + injury reports
  → Airflow + source contracts
  → GCS + BigQuery
  → dbt models + semantic layer
  → FastAPI: Ask agent (tool calling over governed metrics),
             Players, Performance, Trending, Profiles
```

| Layer | What it does here |
| --- | --- |
| **Ingest and orchestrate** | A daily Airflow run pulls six source domains (game logs, schedule, game line scores, player reference, shot locations, and injury reports) into GCS and BigQuery. |
| **Data contracts** | Sources are checked against [contracts](contracts/), rejected rows are quarantined with audit evidence, and validated outputs publish atomically so a failed refresh keeps the last good version. |
| **Semantic layer** | dbt models plus the [semantic contract](docs/semantic-contract.md) give Ask and every page the same metric definitions, scopes, and missing-data rules. |
| **Agentic layer** | Ask plans with tool calling over allowlisted, read-only tools and schema-validated plans. It runs on OpenAI, Anthropic, or OpenRouter and never writes SQL. A visualization specialist picks among server-built charts; application code supplies every value. |
| **Evals and guardrails** | An Ask endpoint harness over frozen evidence, human review, request-stage telemetry, and bounded model calls and streaming. A public evaluation showcase is not published yet; private runs are not evidence of general model superiority or production readiness. |

**Profiles** adds a public similarity baseline and interactive model comparisons on
top of the same gold tables.

Every pull request runs in CI:

- `ruff` linting and formatting, and `mypy` type checks
- 1,500+ Python tests, plus JavaScript unit tests
- Semantic-contract fixtures and an Ask endpoint harness over frozen evidence
- Browser checks of populated Ask flows
- `dbt parse`, Terraform validation, and the public/private boundary check

## Documentation

| Guide | Covers |
| --- | --- |
| [Architecture](docs/architecture.md) | Pipeline flow, warehouse layout, publication, and serving |
| [Semantic contract](docs/semantic-contract.md) | Metric definitions, aggregation, qualification, and evidence boundaries |
| [Ask workspace](docs/ask-workspace.md) | How Ask plans, scopes, compares, and presents answers |
| [Evaluation](docs/evaluation.md) | Frozen inputs, the endpoint harness, and human review |
| [Pages](docs/pages.md) | Routes and the Players, Performance, Trending, and Profiles contracts |
| [Operations](docs/operations.md) | Local Airflow, source contracts, validation, backfills, and model catalog checks |
| [Public/private boundary](docs/public-private-boundary.md) | What stays in Git and what stays private |

## Repository map

| Path | Contents |
| --- | --- |
| `app/` | FastAPI app, Ask agent and tools, BigQuery repository, templates, and static assets |
| `dags/` | Airflow pipeline: extraction, staging, data-quality checks, merges, and publication |
| `dbt/` | Bronze, silver, gold, and Ask-specific models with tests |
| `contracts/` | Source contracts for the six ingested domains |
| `infra/terraform/` | GCP infrastructure |
| `scripts/` | Backfills, study builders, evaluators, and repository checks |
| `tests/`, `tests_js/` | Python and JavaScript test suites and frozen fixtures |

## How the project evolved

The project grew one layer at a time.

1. **Notebook to ELT pipeline (Feb–Mar 2026):** a notebook that loaded game logs
   into BigQuery and had Claude write a recap became an Airflow, GCS, BigQuery,
   dbt, and Terraform pipeline. See the [initial notebook](https://github.com/mikeh-studio/nba-stats-desk/commit/4d37b31)
   and [2025–26 production stack](https://github.com/mikeh-studio/nba-stats-desk/pull/1).
2. **Data contracts and quality (Apr–May 2026):** source contracts, quarantine,
   freshness tracking, and atomic publication. Analysis stayed in deterministic SQL
   until this foundation was solid. See [source contracts](https://github.com/mikeh-studio/nba-stats-desk/pull/19)
   and [atomic optional assets](https://github.com/mikeh-studio/nba-stats-desk/pull/45).
3. **Semantic layer (Sep 2026):** Ask and game logs moved onto one governed semantic
   contract, two more seasons were backfilled, and the old snapshot pipeline was
   retired. See [semantic contract](https://github.com/mikeh-studio/nba-stats-desk/pull/49)
   and [historical seasons](https://github.com/mikeh-studio/nba-stats-desk/pull/48).
4. **Agentic layer (Jun–Sep 2026):** Ask brought the LLM back as a tool-calling
   planner on top of the semantic layer, with clarification for ambiguous names
   and refusals for unsupported scope. See the [first Ask agent](https://github.com/mikeh-studio/nba-stats-desk/pull/23),
   [streaming and clarification](https://github.com/mikeh-studio/nba-stats-desk/pull/31),
   and [Ask scope and comparisons](https://github.com/mikeh-studio/nba-stats-desk/pull/61).
5. **Evals and guardrails (Sep–Oct 2026):** an endpoint evaluation harness, human
   review, request telemetry, and bounded execution. The optional Redshift sync was
   removed so BigQuery is the only warehouse. See the [evaluation harness](https://github.com/mikeh-studio/nba-stats-desk/pull/65),
   [human review](https://github.com/mikeh-studio/nba-stats-desk/pull/68),
   [bounded execution](https://github.com/mikeh-studio/nba-stats-desk/pull/69), and
   [Redshift removal](https://github.com/mikeh-studio/nba-stats-desk/pull/74).

## Public code, private operations

The application, evaluation framework, and public baseline implementation are
available in this repository. Raw evaluation runs, conversations, review notes,
credentials, and proprietary model experiments stay outside Git. Only reviewed,
sanitized examples and concise summaries belong in public showcases.
See the [public/private boundary](docs/public-private-boundary.md).

The app supports Cloud Run deployment; this repository does not imply an active
public deployment. Public serving is read-only and currently has no account system.
Deployment configuration and access boundaries are covered in the
[public service guide](docs/pages.md#public-service).

## Run locally

Use Python 3.11+ and configure access to your own populated BigQuery datasets:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Set project/dataset values and at least one provider key for Ask.
uvicorn app.main:app --host 127.0.0.1 --port 8001 --no-proxy-headers
```

Open `http://localhost:8001`. Ask accepts `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, or
`OPENROUTER_API_KEY` in the server environment. Keep `AGENT_HISTORY_ENABLED=false`
for public deployments. OpenRouter is covered by mocked tests only; see its
[current limits](docs/ask-workspace.md#openrouter).

For pipeline setup and checks, see [Local Airflow](docs/operations.md#local-airflow),
[historical seasons](docs/operations.md#historical-seasons), and [validation](docs/operations.md#validation).

## License

Code and documentation use the [MIT License](LICENSE). NBA data, logos, trademarks,
player headshots, and third-party content remain subject to their owners' terms.
