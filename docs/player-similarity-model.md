# Player Similarity Baseline

This public repo publishes player similarity through two BigQuery tables:

- `nba_gold.player_similarity_features`
- `nba_gold.player_archetypes`

The public implementation is a reference baseline, not the tuned personal model.
It exists to keep the platform runnable, testable, and reproducible without
publishing private model logic.

## Public Scope

The public code owns:

- the dbt feature input table
- the BigQuery output schemas
- deterministic baseline clustering
- equal-weight serving-time similarity
- contract tests for the public output shape

The private model package should own:

- tuned feature weights
- model-selection experiments
- thresholds and role-gating rules
- evaluation reports
- notebooks, labels, and generated grouping reports
- model artifacts and checkpoints

See [`docs/public-private-boundary.md`](public-private-boundary.md) for the
repo-level boundary.

## Feature Contract

`dbt/models/gold/player_similarity_feature_input.sql` emits one row per
`season, player_id` for players with at least three games. Players are eligible
for clustering when `sample_status` is `ready` or `limited_sample`; players with
`insufficient_sample` are excluded from training and publish outputs.

The public feature layer includes:

- box-score production
- shooting efficiency and shot profile
- aggregate shot location
- team offensive and defensive contribution shares
- recent form
- season split trend
- physical and career context

Team contribution fields are part of the public data contract because they are
basic basketball context, not private model IP. They keep role labels grounded
in a player's contribution to their own team instead of only league-wide
box-score shape.

## Baseline Training Contract

The public baseline entrypoint is
`player_similarity_model.train_player_similarity_model`. The Airflow-compatible
wrapper remains `nba_pipeline.build_player_similarity_outputs`.

The baseline path:

1. Coerces ids, dates, sample status, and numeric features.
2. Drops `insufficient_sample` rows.
3. Sorts by `season, player_id`.
4. Median-imputes missing values, with all-empty optional feature columns set
   to zero and reported in diagnostics.
5. Applies standard scaling.
6. L2-normalizes equal-weight vectors.
7. Trains deterministic KMeans with the configured cluster count.
8. Projects the L2-normalized vectors to 3D with PCA (`proj_x/proj_y/proj_z`)
   for the similarity map. PCA is used instead of t-SNE/UMAP so the published
   coordinates are deterministic and reproducible across runs. The projection
   is an approximate map only; the cosine similarity score remains the source
   of truth, and explained variance is reported in diagnostics. Each axis also
   gets its top driving features (from the PCA loadings, mapped to trait
   labels), published as a `projection_axes` JSON column so the map can label
   PC1/PC2/PC3 with what actually separates players.
9. Publishes normalized feature vectors, projection coordinates, archetype
   labels, confidence, top traits, and table diagnostics.

This baseline deliberately does not publish tuned feature weights, detailed
thresholds, experimental scoring rules, or model-selection heuristics.

## Publish Contract

`write_player_similarity_tables` deletes and rewrites the seasons included in
the model output. The feature table load allows additive schema changes so new
`norm_*` and `proj_*` fields can be published without dropping the existing
table. The 3D projection is served read-only at `/similarity-map` (Plotly
`scatter3d`) and `/api/similarity-map`. Selecting a player calls
`/api/similarity-map/neighbors/{player_id}`, which returns the player's true
cosine-nearest matches from the served similarity scoring. The map draws edges
to those matches by their projected coordinates, so an edge can point to a
player who sits visually far away — the projection is a map, the cosine score
is the truth.

To populate `proj_*` immediately (instead of waiting for the next scheduled
run), recompute and republish with the existing pipeline path:

```bash
python scripts/backfill_similarity_projection.py          # dry run (read-only)
python scripts/backfill_similarity_projection.py --write   # publish to BigQuery
```

## Validation

Local validation that does not require BigQuery:

```bash
pytest tests/test_player_similarity_model.py tests/test_incremental_pipeline.py -q
```

dbt parse validation:

```bash
dbt parse --project-dir . --profiles-dir dbt/profiles --target dev
```

Live validation, when BigQuery credentials are configured:

```bash
dbt build --project-dir . --profiles-dir dbt/profiles --target dev \
  --select fct_player_game_stats fct_player_scoring_contribution \
    player_recent_form player_category_profile player_similarity_feature_input
```
