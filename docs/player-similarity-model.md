# Player similarity baseline

The public implementation provides reproducible similarity and archetype
comparisons through `nba_gold.player_similarity_features` and
`nba_gold.player_archetypes`. It is a reference baseline; personal tuning,
labels, experiments, and model artifacts belong outside the public repository.

## Inputs and training

`player_similarity_feature_input` has one row per season and player. Its
features cover production, shooting, recent form, team contribution, and available
physical/career attributes. Missing optional attributes remain visible in diagnostics.
Players marked `insufficient_sample` are excluded from training and publication.

`player_similarity_model.train_player_similarity_model` trains the public KMeans,
Gaussian mixture, hierarchical, and HDBSCAN comparison set on shared normalized
features. The Airflow wrapper is `nba_pipeline.build_player_similarity_outputs`.
Outputs include normalized vectors, descriptive archetypes, comparison diagnostics,
and a deterministic three-dimensional PCA projection.

The application uses equal-weight serving-time similarity. PCA coordinates are
an approximate visualization; nearest-neighbor relationships use cosine similarity
from the feature vectors. A visually distant point can therefore be a close
feature-space neighbor. Axis metadata describes the projection's feature drivers.
Archetypes are descriptive categories, not authoritative scouting grades or
measures of player value.

## Publication and serving

Validated feature and archetype candidates are published together through the
pipeline's atomic publication path. Failures preserve previously served data.
The `/similarity-map` page and its JSON endpoints read the published outputs;
switching candidate-model groupings retains the same player coordinates.

The repository includes metadata table-creation support for a similarity
lifecycle. An end-to-end versioned feature store, registry promotion workflow,
and drift-monitoring system should not be inferred from those schemas alone.
See [Architecture](architecture.md) for implemented publication behavior and
[Public/private boundary](public-private-boundary.md) for ownership boundaries.

## Run and validate

```bash
python -m pytest tests/test_player_similarity_model.py tests/test_incremental_pipeline.py -q
dbt parse --project-dir . --profiles-dir dbt/profiles --target dev
python scripts/backfill_similarity_projection.py          # read-only dry run
python scripts/backfill_similarity_projection.py --write  # publishes to BigQuery
```

The backfill commands require configured warehouse access. With the appropriate
warehouse target, validate the feature dependencies using:

```bash
dbt build --project-dir . --profiles-dir dbt/profiles --target dev \
  --select fct_player_game_stats fct_player_scoring_contribution \
    player_recent_form player_category_profile player_similarity_feature_input
```

Local test success does not establish live feature coverage or successful
warehouse publication. Keep detailed model runs and diagnostics in local reports.
