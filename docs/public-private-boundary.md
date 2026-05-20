# Public / Private Boundary

This repo is intended to stay public as the NBA data platform and reference
application. The personal model layer should live in a private repo or package.

## Keep Public

- source ingestion and orchestration
- source contracts
- dbt bronze, silver, gold, and agent models
- feature input tables
- public baseline similarity output contracts
- FastAPI routes and read-only app behavior
- public-safe docs and validation commands
- `.env.example` placeholders

## Keep Private

- tuned similarity model code
- feature weights, thresholds, and model-selection logic
- manual labels, evaluation sets, and scouting notes
- generated reports under `reports/`
- notebooks and local experiments
- model artifacts, checkpoints, and embeddings
- real `.env` files, service accounts, tokens, and warehouse exports

## Local Spinout

The public repo can expose feature tables such as
`nba_gold.player_similarity_feature_input`. A private package can consume those
tables, train the personal model, and write private reports or model artifacts
without changing the public contract.

Recommended private repo shape:

```text
nba-personal-model/
  models/
  notebooks/
  reports/
  evals/
  README.md
```

Keep any local bridge scripts ignored until they are safe to publish.
