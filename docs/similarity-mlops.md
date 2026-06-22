# Player Similarity Feature Store and MLOps

This document defines the target operating contract for the player similarity
model lifecycle. The current public path remains intentionally reproducible:
dbt builds `nba_gold.player_similarity_feature_input`, Airflow trains a public
comparison set on equal-weight normalized vectors, and BigQuery serves
`nba_gold.player_similarity_features` plus `nba_gold.player_archetypes`.

The MLOps goal is to make that lifecycle versioned, auditable, and promotable
without moving tuned personal-model logic, private labels, notebooks, or model
artifacts into the public repo.

## Goals

- Reproduce any published similarity output from a feature snapshot, model
  version, run id, and git SHA.
- Separate feature production, candidate training, evaluation, promotion, and
  serving.
- Keep active public serving tables stable while allowing candidate models to
  be evaluated before promotion.
- Store public-safe metrics and decisions in BigQuery while keeping tuned
  weights, labels, artifacts, and scouting notes private.
- Prefer a BigQuery-native offline feature store until there is a real
  low-latency online-serving requirement.

## Current Baseline

Current public contract:

- Offline feature input:
  `nba_gold.player_similarity_feature_input`
- Training entrypoint:
  `player_similarity_model.train_player_similarity_model`
- Airflow-compatible wrapper:
  `nba_pipeline.build_player_similarity_outputs`
- Active serving outputs:
  `nba_gold.player_similarity_features`
  `nba_gold.player_archetypes`
- Public candidate models:
  KMeans baseline, Gaussian mixture, agglomerative hierarchy, and HDBSCAN
  density scan
- Public validation:
  deterministic training tests, schema/shape checks, projection checks, and
  dbt parse/build gates.

The public comparison deliberately publishes equal-weight vectors and aggregate
diagnostics only. The tuned personal model can consume the same feature
contract from a private package, but it must not publish private weights,
thresholds, labels, notebooks, or artifacts back into this repo.

## Offline Feature Store

Use BigQuery as the offline feature store. The first feature view should mirror
the current similarity feature input but add versioning and run lineage.

### Feature View

Name: `player_similarity_features`

Entity grain: one row per `season, player_id, as_of_date, feature_set_version`.

Entity keys:

- `season`
- `player_id`
- `as_of_date`

Required lineage fields:

- `feature_set_version`
- `feature_run_id`
- `source_run_id`
- `source_watermark`
- `dbt_invocation_id`
- `git_sha`
- `created_at`

Required quality fields:

- `games_sampled`
- `sample_status`
- `feature_null_count`
- `feature_quality_status`
- `feature_quality_notes`

The current dbt model can stay as the latest feature view for app-serving
compatibility. A versioned snapshot table should be added when the Airflow
pipeline starts enforcing promotion gates.

### Proposed Tables

`nba_features.player_similarity_feature_values`

- Versioned feature snapshot used for model training.
- Grain: `season, player_id, as_of_date, feature_set_version`.
- Partition by `as_of_date`; cluster by `season, player_id`.
- Stores the public feature columns plus lineage and quality fields.

`nba_features.feature_view_registry`

- One row per feature view version.
- Tracks owner, entity grain, source dbt refs, freshness SLA, feature columns,
  expected ranges, and compatibility status.

`nba_ml.similarity_model_runs`

- One row per candidate or active training run.
- Tracks `model_run_id`, `model_version`, `feature_set_version`, train time,
  input row counts, cluster count, code version, artifact URI, and run status.

`nba_ml.similarity_eval_results`

- Public-safe evaluation metrics for each `model_run_id`.
- Tracks feature coverage, duplicate-key checks, null-rate checks, cluster
  distribution, PCA explained variance, nearest-neighbor stability, archetype
  distribution, and promotion gate results.

`nba_ml.similarity_model_registry`

- Promotion state for model versions.
- Valid statuses: `candidate`, `shadow`, `active`, `retired`, `rejected`.
- At most one `active` row should exist per serving surface and season.

`nba_ml.similarity_drift_checks`

- Periodic checks comparing the active model's current input distribution
  against the promoted feature snapshot.
- Tracks feature drift, player coverage drift, archetype distribution drift,
  and nearest-neighbor churn.

The initial BigQuery table contract is implemented in
`nba_pipeline.create_similarity_mlops_tables`. Airflow wiring and row-level
writers should call that helper before writing feature snapshots, model runs,
evaluations, registry rows, or drift checks.

## Versioning Contract

Feature versions should be explicit and boring:

```text
feature_set_version = player_similarity_features_v1
model_version       = public_similarity_multi_model_v1
model_run_id        = similarity_YYYYMMDDTHHMMSSZ_<short_sha>
```

When a feature definition changes, increment `feature_set_version`. When model
logic changes without changing the feature contract, increment `model_version`.
When only the data refreshes, keep both versions stable and create a new
`model_run_id`.

Every published active output should be traceable to:

- `feature_set_version`
- `model_version`
- `model_run_id`
- `source_run_id`
- `git_sha`
- Airflow DAG run id

## Training and Evaluation Flow

Target Airflow flow:

```text
dbt build player_similarity_feature_input
  -> write versioned feature snapshot
  -> train candidate similarity model
  -> write candidate run + diagnostics
  -> evaluate candidate against gates
  -> promote or reject candidate
  -> publish active gold serving tables
  -> record drift baseline and run metadata
```

The current best-effort similarity publish can remain non-blocking for the core
data refresh. Promotion should be blocking for replacing active similarity
outputs: a failed candidate should leave the previous active model in place.

## Promotion Gates

Minimum gates before a candidate can become active:

- No duplicate `season, player_id` rows in feature input or outputs.
- No null `player_id`, `season`, `as_of_date`, or `sample_status`.
- Eligible player count is above the configured season threshold.
- `insufficient_sample` rows are excluded from training.
- Feature null rates and all-empty feature columns are within expected bounds.
- Effective cluster count matches the configured range for the player count.
- Cluster distribution has no collapsed dominant cluster unless explicitly
  approved.
- PCA projection emits all expected coordinate columns and axis metadata.
- Neighbor stability versus the current active model stays within the expected
  churn band.
- Archetype label distribution is explainable from top traits.
- Public-safe manual review aggregate, if available, is pass or waived with a
  reason.

Promotion decisions should be stored in `nba_ml.similarity_eval_results` with
the gate name, status, metric value, threshold, and failure reason.

## Serving Contract

The app should keep reading only active public serving tables:

- `nba_gold.player_similarity_features`
- `nba_gold.player_archetypes`

Candidate and shadow outputs should not be read by the public FastAPI service.
Promotion should be the only step that rewrites active serving rows. The
registry table records which model run produced the current active rows.

Recommended future serving metadata fields on active outputs:

- `feature_set_version`
- `model_version`
- `model_run_id`
- `promoted_at`

These can be added through BigQuery additive schema updates without breaking
existing app queries.

## Drift Monitoring

Daily or per-refresh checks should compare the current feature snapshot against
the active model's promoted snapshot:

- player coverage count and missing-player list
- per-feature null-rate deltas
- per-feature distribution deltas
- archetype distribution deltas
- nearest-neighbor churn for stable players
- PCA explained-variance movement
- sample-status mix

Drift alerts should be informational at first. They become blocking only after
stable thresholds are learned from several real refreshes.

## Public and Private Boundary

Public repo can contain:

- feature view definitions and docs
- public baseline model code
- public-safe model run metadata schemas
- aggregate evaluation metrics
- promotion gate code
- active serving table contracts

Private package should contain:

- tuned feature weights
- custom similarity scoring logic
- manual labels and evaluation sets
- notebooks and exploratory reports
- model artifacts and checkpoints
- private scouting notes

The private package may write private evaluation reports or model artifacts, but
public active serving outputs should remain explainable by public-safe metadata.

## Implementation Phases

1. Document the lifecycle contract and link it from architecture and similarity
   docs.
2. Add BigQuery table-creation helpers for `nba_features` and `nba_ml`
   lifecycle tables.
3. Add an Airflow task to snapshot `player_similarity_feature_input` into
   `nba_features.player_similarity_feature_values`.
4. Persist candidate `model_run_id` diagnostics into
   `nba_ml.similarity_model_runs`.
5. Add promotion-gate evaluation and leave previous active rows untouched when
   a candidate fails.
6. Add active serving metadata fields to `player_similarity_features` and
   `player_archetypes`.
7. Add drift checks and expose the latest model lifecycle status in operational
   docs or a lightweight internal query.

This path raises the project from a reproducible baseline model to an operated
model system while staying aligned with the current GCP, dbt, Airflow, and
FastAPI architecture.
