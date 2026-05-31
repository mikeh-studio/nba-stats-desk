"""Public baseline player similarity model.

This module intentionally keeps only a reference implementation for the public
repo. It preserves the BigQuery output contract while leaving tuned feature
weights, thresholds, evaluation reports, and experimental model logic for a
private model package.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

import pandas as pd

MODEL_VERSION = "public_similarity_baseline_v1"
RANDOM_STATE = 42
MINIMUM_ARCHETYPE_CLUSTERS = 8
KMEANS_N_INIT = 10
FEATURE_SCALER = "standard"
MODEL_VECTOR_NORMALIZATION = "l2_equal_weight"

# 3D map projection. PCA is deterministic and reproducible across pipeline runs
# (unlike t-SNE/UMAP), so the published coordinates are stable. The projection
# is an approximate map for navigation; the cosine similarity score stays the
# source of truth for "how similar".
PROJECTION_METHOD = "pca"
PROJECTION_COMPONENTS = 3
PROJECTION_COLUMNS: Tuple[str, ...] = ("proj_x", "proj_y", "proj_z")


@dataclass(frozen=True)
class SimilarityFeatureSpec:
    name: str
    family: str
    weight: float = 1.0
    trait_label: str | None = None


@dataclass(frozen=True)
class SimilarityTrainingResult:
    features: pd.DataFrame
    archetypes: pd.DataFrame
    diagnostics: Dict[str, Any]


SIMILARITY_FEATURE_SPECS: Tuple[SimilarityFeatureSpec, ...] = (
    SimilarityFeatureSpec("season_avg_pts", "scoring", trait_label="scoring volume"),
    SimilarityFeatureSpec("season_avg_fga", "scoring", trait_label="shot volume"),
    SimilarityFeatureSpec(
        "season_fg_pct", "efficiency", trait_label="field-goal efficiency"
    ),
    SimilarityFeatureSpec("season_ts_pct", "efficiency", trait_label="true shooting"),
    SimilarityFeatureSpec(
        "season_fg3a_rate", "shot_profile", trait_label="three-point diet"
    ),
    SimilarityFeatureSpec(
        "season_fta_rate", "shot_profile", trait_label="rim pressure"
    ),
    SimilarityFeatureSpec("season_ast_to_tov", "creation", trait_label="ball security"),
    SimilarityFeatureSpec(
        "team_points_contribution_rate",
        "team_context",
        trait_label="team scoring share",
    ),
    SimilarityFeatureSpec(
        "team_fga_contribution_rate",
        "team_context",
        trait_label="team shot share",
    ),
    SimilarityFeatureSpec(
        "team_ast_contribution_rate",
        "team_context",
        trait_label="team assist share",
    ),
    SimilarityFeatureSpec(
        "team_tov_contribution_rate",
        "team_context",
        trait_label="team turnover load",
    ),
    SimilarityFeatureSpec(
        "team_offense_contribution_rate",
        "team_context",
        trait_label="team offense ownership",
    ),
    SimilarityFeatureSpec(
        "team_reb_contribution_rate",
        "team_context",
        trait_label="team rebounding share",
    ),
    SimilarityFeatureSpec(
        "team_stl_contribution_rate",
        "team_context",
        trait_label="team steal share",
    ),
    SimilarityFeatureSpec(
        "team_blk_contribution_rate",
        "team_context",
        trait_label="team block share",
    ),
    SimilarityFeatureSpec(
        "team_defense_contribution_rate",
        "team_context",
        trait_label="team defensive event share",
    ),
    SimilarityFeatureSpec(
        "shot_rim_rate", "shot_location", trait_label="rim shot diet"
    ),
    SimilarityFeatureSpec(
        "shot_paint_non_ra_rate",
        "shot_location",
        trait_label="paint shot diet",
    ),
    SimilarityFeatureSpec(
        "shot_midrange_rate",
        "shot_location",
        trait_label="midrange diet",
    ),
    SimilarityFeatureSpec(
        "shot_corner3_rate",
        "shot_location",
        trait_label="corner-three diet",
    ),
    SimilarityFeatureSpec(
        "shot_above_break3_rate",
        "shot_location",
        trait_label="above-break three diet",
    ),
    SimilarityFeatureSpec(
        "shot_rim_fg_pct",
        "shot_location",
        trait_label="rim finishing",
    ),
    SimilarityFeatureSpec(
        "shot_corner3_fg_pct",
        "shot_location",
        trait_label="corner-three efficiency",
    ),
    SimilarityFeatureSpec("season_avg_reb", "box_score", trait_label="rebounding"),
    SimilarityFeatureSpec("season_avg_ast", "box_score", trait_label="playmaking"),
    SimilarityFeatureSpec("season_avg_stl", "box_score", trait_label="steals pressure"),
    SimilarityFeatureSpec("season_avg_blk", "box_score", trait_label="rim protection"),
    SimilarityFeatureSpec(
        "season_avg_fg3m", "box_score", trait_label="three-point volume"
    ),
    SimilarityFeatureSpec("season_avg_tov", "box_score", trait_label="creation load"),
    SimilarityFeatureSpec("season_avg_min", "role_context", trait_label="minutes load"),
    SimilarityFeatureSpec("height_inches", "physical_profile", trait_label="height"),
    SimilarityFeatureSpec(
        "weight_lbs", "physical_profile", trait_label="frame strength"
    ),
    SimilarityFeatureSpec("season_exp", "career_context", trait_label="experience"),
    SimilarityFeatureSpec("recent_pts", "recent_form", trait_label="recent scoring"),
    SimilarityFeatureSpec("recent_reb", "recent_form", trait_label="recent rebounding"),
    SimilarityFeatureSpec("recent_ast", "recent_form", trait_label="recent playmaking"),
    SimilarityFeatureSpec("recent_stl", "recent_form", trait_label="recent steals"),
    SimilarityFeatureSpec("recent_blk", "recent_form", trait_label="recent blocks"),
    SimilarityFeatureSpec("recent_fg3m", "recent_form", trait_label="recent threes"),
    SimilarityFeatureSpec("recent_tov", "recent_form", trait_label="recent turnovers"),
    SimilarityFeatureSpec("recent_min", "role_context", trait_label="recent minutes"),
    SimilarityFeatureSpec(
        "recent_points_share_of_team",
        "usage",
        trait_label="usage share",
    ),
    SimilarityFeatureSpec(
        "recent_points_share_of_game",
        "usage",
        trait_label="game scoring share",
    ),
    SimilarityFeatureSpec(
        "minutes_delta_vs_season",
        "trend",
        trait_label="minutes trend",
    ),
    SimilarityFeatureSpec(
        "second_half_pts_delta",
        "season_split",
        trait_label="second-half scoring growth",
    ),
    SimilarityFeatureSpec(
        "second_half_min_delta",
        "season_split",
        trait_label="second-half role growth",
    ),
    SimilarityFeatureSpec(
        "second_half_ts_delta",
        "season_split",
        trait_label="second-half efficiency growth",
    ),
)

SIMILARITY_FEATURE_COLUMNS: List[str] = [
    feature.name for feature in SIMILARITY_FEATURE_SPECS
]
SIMILARITY_FEATURE_WEIGHTS: Dict[str, float] = {
    feature.name: 1.0 for feature in SIMILARITY_FEATURE_SPECS
}
SIMILARITY_TRAIT_LABELS: Dict[str, str] = {
    feature.name: feature.trait_label
    for feature in SIMILARITY_FEATURE_SPECS
    if feature.trait_label is not None
}

BASE_ARCHETYPE_LABELS = {
    "Primary Creator",
    "Scoring Guard",
    "Two-Way Wing",
    "Connector Wing",
    "Stretch Big",
    "Interior Big",
}
ALLOWED_ARCHETYPE_LABELS = BASE_ARCHETYPE_LABELS

OUTPUT_ID_COLUMNS = [
    "season",
    "as_of_date",
    "player_id",
    "player_name",
    "team_abbr",
    "position",
    "games_sampled",
    "sample_status",
]

TEAM_CONTEXT_COLUMNS = [
    "team_offense_contribution_rank",
    "team_defense_contribution_rank",
]


def _coerce_similarity_feature_frame(feature_df: pd.DataFrame) -> pd.DataFrame:
    """Normalize dtypes for the public similarity baseline."""
    if feature_df.empty:
        return feature_df.copy()

    working = feature_df.copy()
    working["player_id"] = pd.to_numeric(working["player_id"], errors="coerce")
    working = working.dropna(subset=["player_id"]).copy()
    working["player_id"] = working["player_id"].astype(int)
    working["games_sampled"] = (
        pd.to_numeric(working.get("games_sampled"), errors="coerce")
        .fillna(0)
        .astype(int)
    )
    working["season"] = working["season"].fillna("").astype("string")
    working["player_name"] = working["player_name"].fillna("").astype("string")
    working["team_abbr"] = working["team_abbr"].fillna("").astype("string")
    if "position" not in working:
        working["position"] = ""
    if "sample_status" not in working:
        working["sample_status"] = "insufficient_sample"
    if "as_of_date" not in working:
        working["as_of_date"] = pd.NaT
    working["position"] = working["position"].fillna("").astype("string")
    working["sample_status"] = (
        working["sample_status"].fillna("insufficient_sample").astype("string")
    )
    working["as_of_date"] = pd.to_datetime(
        working["as_of_date"], errors="coerce"
    ).dt.date

    for column in SIMILARITY_FEATURE_COLUMNS:
        if column not in working:
            working[column] = pd.NA
        working[column] = pd.to_numeric(working[column], errors="coerce")

    for column in TEAM_CONTEXT_COLUMNS:
        if column not in working:
            working[column] = pd.NA
        working[column] = (
            pd.to_numeric(working[column], errors="coerce").fillna(999).astype(int)
        )

    return working


def _prepare_feature_matrix(modeling: pd.DataFrame) -> Tuple[pd.DataFrame, List[str]]:
    feature_matrix = modeling[SIMILARITY_FEATURE_COLUMNS].copy()
    empty_columns = [
        column
        for column in SIMILARITY_FEATURE_COLUMNS
        if feature_matrix[column].isna().all()
    ]
    for column in empty_columns:
        feature_matrix[column] = 0.0
    return feature_matrix, empty_columns


def _rank_similarity_traits(
    values: Dict[str, float],
    *,
    limit: int = 3,
    positive_only: bool = False,
    negative_only: bool = False,
) -> List[str]:
    ranked: List[Tuple[str, float]] = []
    for feature_name, trait_label in SIMILARITY_TRAIT_LABELS.items():
        raw_value = values.get(feature_name)
        if raw_value in (None, ""):
            continue
        score = float(raw_value)
        if positive_only and score <= 0:
            continue
        if negative_only and score >= 0:
            continue
        ranked.append((trait_label, score))

    if negative_only:
        ranked.sort(key=lambda item: item[1])
    elif positive_only:
        ranked.sort(key=lambda item: item[1], reverse=True)
    else:
        ranked.sort(key=lambda item: abs(item[1]), reverse=True)

    return [label for label, _ in ranked[:limit]]


def _label_cluster(
    center_values: Dict[str, float], *, allow_primary_creator: bool = True
) -> str:
    """Map a baseline cluster center to a human-readable public archetype."""
    points = float(center_values.get("season_avg_pts", 0.0))
    shot_volume = float(center_values.get("season_avg_fga", 0.0))
    assists = float(center_values.get("season_avg_ast", 0.0))
    rebounds = float(center_values.get("season_avg_reb", 0.0))
    steals = float(center_values.get("season_avg_stl", 0.0))
    blocks = float(center_values.get("season_avg_blk", 0.0))
    threes = float(center_values.get("season_avg_fg3m", 0.0))
    three_rate = float(center_values.get("season_fg3a_rate", 0.0))
    usage = float(center_values.get("recent_points_share_of_team", 0.0))
    team_points = float(center_values.get("team_points_contribution_rate", 0.0))
    team_shots = float(center_values.get("team_fga_contribution_rate", 0.0))
    team_assists = float(center_values.get("team_ast_contribution_rate", 0.0))
    team_offense = float(center_values.get("team_offense_contribution_rate", 0.0))
    team_defense = float(center_values.get("team_defense_contribution_rate", 0.0))
    team_rebounds = float(center_values.get("team_reb_contribution_rate", 0.0))
    team_blocks = float(center_values.get("team_blk_contribution_rate", 0.0))
    height = float(center_values.get("height_inches", 0.0))
    weight = float(center_values.get("weight_lbs", 0.0))

    scoring = max(points, shot_volume, usage, team_points, team_shots)
    creation = max(assists, team_assists)
    spacing = max(threes, three_rate)
    defense = max(steals, team_defense)
    interior = max(rebounds, blocks, team_rebounds, team_blocks, height, weight)
    offense_context = max(team_offense, team_points, team_shots, team_assists)

    if interior >= 0.7 and spacing >= 0.15:
        return "Stretch Big"
    if interior >= 0.7:
        return "Interior Big"
    if allow_primary_creator and creation >= 0.75 and offense_context >= 0.35:
        return "Primary Creator"
    if scoring >= 0.7 and creation < 0.9:
        return "Scoring Guard"
    if defense >= 0.2 and spacing >= 0.1:
        return "Two-Way Wing"
    return "Connector Wing"


def _numeric_value(values: Dict[str, Any], name: str, default: float = 0.0) -> float:
    value = values.get(name)
    if value in (None, ""):
        return default
    try:
        if pd.isna(value):
            return default
    except TypeError:
        pass
    return float(value)


def _player_base_archetype_label(
    *,
    cluster_base_label: str,
    normalized_values: Dict[str, float],
    raw_values: Dict[str, Any],
) -> str:
    if cluster_base_label != "Primary Creator":
        return cluster_base_label

    team_offense_rank = _numeric_value(
        raw_values,
        "team_offense_contribution_rank",
        default=999.0,
    )
    if team_offense_rank <= 1:
        return "Primary Creator"
    return _label_cluster(normalized_values, allow_primary_creator=False)


def _player_archetype_display(
    *,
    base_label: str,
    normalized_values: Dict[str, float],
    cluster_index: int,
) -> Tuple[str, str, str]:
    top_traits = _rank_similarity_traits(normalized_values, limit=3, positive_only=True)
    if top_traits:
        label = f"{base_label} - {' / '.join(trait.title() for trait in top_traits)}"
    else:
        label = f"{base_label} - Profile {cluster_index + 1}"
    return label, _build_cluster_summary(label, top_traits), ", ".join(top_traits)


def _build_cluster_summary(archetype_label: str, top_traits: List[str]) -> str:
    if not top_traits:
        return archetype_label
    return f"{archetype_label} driven by {', '.join(top_traits)}."


def _validate_similarity_output_frames(
    features_df: pd.DataFrame, archetypes_df: pd.DataFrame
) -> None:
    if features_df.empty or archetypes_df.empty:
        raise ValueError("Similarity outputs must not be empty")

    feature_dupes = features_df.duplicated(subset=["season", "player_id"]).any()
    archetype_dupes = archetypes_df.duplicated(subset=["season", "player_id"]).any()
    if feature_dupes:
        raise ValueError(
            "Duplicate season/player rows found in player_similarity_features"
        )
    if archetype_dupes:
        raise ValueError("Duplicate season/player rows found in player_archetypes")

    if features_df["player_id"].isna().any() or archetypes_df["player_id"].isna().any():
        raise ValueError("player_id must not be null in similarity outputs")

    base_labels = {
        str(value).split(" - ", maxsplit=1)[0]
        for value in archetypes_df["archetype_label"].dropna()
    }
    invalid_labels = base_labels - BASE_ARCHETYPE_LABELS
    if invalid_labels:
        raise ValueError(
            f"Unexpected archetype labels detected: {sorted(invalid_labels)}"
        )


def _effective_cluster_count(
    *, requested_clusters: int, row_count: int, minimum_clusters: int
) -> int:
    if row_count <= 0:
        return 0
    requested = max(1, int(requested_clusters))
    if row_count < minimum_clusters:
        return min(requested, row_count)
    return min(max(requested, minimum_clusters), row_count)


def _candidate_cluster_counts(
    *, requested_clusters: int, row_count: int, minimum_clusters: int
) -> List[int]:
    effective = _effective_cluster_count(
        requested_clusters=requested_clusters,
        row_count=row_count,
        minimum_clusters=minimum_clusters,
    )
    return [effective] if effective else []


def _feature_display_label(feature_name: str) -> str:
    """Human-readable name for a similarity feature (for axis annotations)."""
    label = SIMILARITY_TRAIT_LABELS.get(feature_name)
    if label:
        return label
    return feature_name.replace("_", " ")


def _axis_drivers(loadings: Any, *, limit: int = 3) -> List[str]:
    """Top features (by absolute PCA loading) that define an axis."""
    import numpy as np

    order = np.argsort(np.abs(loadings))[::-1]
    drivers: List[str] = []
    for index in order:
        label = _feature_display_label(SIMILARITY_FEATURE_COLUMNS[int(index)])
        if label not in drivers:
            drivers.append(label)
        if len(drivers) >= limit:
            break
    return drivers


def _project_similarity_vectors(
    model_values: Any,
    *,
    random_state: int = RANDOM_STATE,
) -> Tuple[Any, List[Dict[str, Any]]]:
    """Project the L2-normalized similarity vectors to 3D with PCA.

    The projection runs on the same vectors the served cosine similarity uses,
    so spatial proximity on the map tracks the similarity metric. Each axis also
    gets its explained-variance share and its top driving features (from the PCA
    loadings) so the plot can label PC1/PC2/PC3 with what actually separates
    players. When there are fewer samples or features than requested components
    (small backfills, tests), the extra coordinate columns are padded with zeros
    so the output shape is stable. Coordinates are an approximate map only.
    """
    import numpy as np
    from sklearn.decomposition import PCA

    n_samples, n_features = model_values.shape
    coords = np.zeros((n_samples, PROJECTION_COMPONENTS), dtype=float)
    axes: List[Dict[str, Any]] = [
        {"key": key, "variance": 0.0, "drivers": []} for key in PROJECTION_COLUMNS
    ]
    components = min(PROJECTION_COMPONENTS, n_samples, n_features)
    if components < 1:
        return coords, axes

    pca = PCA(n_components=components, svd_solver="full", random_state=random_state)
    coords[:, :components] = pca.fit_transform(model_values)
    for index in range(components):
        axes[index]["variance"] = round(float(pca.explained_variance_ratio_[index]), 4)
        axes[index]["drivers"] = _axis_drivers(pca.components_[index])
    return np.round(coords, 5), axes


def train_player_similarity_model(
    feature_df: pd.DataFrame,
    *,
    cluster_count: int = 10,
    minimum_cluster_count: int = MINIMUM_ARCHETYPE_CLUSTERS,
) -> SimilarityTrainingResult:
    """Train the public reference archetype baseline."""
    import numpy as np
    from sklearn.cluster import KMeans
    from sklearn.impute import SimpleImputer
    from sklearn.preprocessing import StandardScaler, normalize

    working = _coerce_similarity_feature_frame(feature_df)
    if working.empty:
        raise ValueError("Cannot build similarity outputs from an empty feature frame")

    modeling = working[working["sample_status"] != "insufficient_sample"].copy()
    if modeling.empty:
        raise ValueError("No players meet the minimum sample threshold for similarity")

    modeling = modeling.sort_values(["season", "player_id"]).reset_index(drop=True)
    feature_matrix, empty_columns = _prepare_feature_matrix(modeling)
    for column in empty_columns:
        modeling[column] = 0.0

    imputer = SimpleImputer(strategy="median")
    imputed_values = imputer.fit_transform(feature_matrix)
    scaler = StandardScaler()
    scaled_values = scaler.fit_transform(imputed_values)
    model_values = normalize(scaled_values, norm="l2")

    effective_clusters = _effective_cluster_count(
        requested_clusters=cluster_count,
        row_count=len(model_values),
        minimum_clusters=minimum_cluster_count,
    )
    if effective_clusters <= 0:
        raise ValueError("Cannot choose clusters for an empty feature matrix")

    kmeans = KMeans(
        n_clusters=effective_clusters,
        n_init=KMEANS_N_INIT,
        random_state=RANDOM_STATE,
    )
    raw_cluster_ids = kmeans.fit_predict(model_values)
    distances = np.linalg.norm(
        model_values - kmeans.cluster_centers_[raw_cluster_ids], axis=1
    )

    modeling["cluster_index"] = raw_cluster_ids
    normalized_columns = {
        f"norm_{feature_name}": scaled_values[:, index]
        for index, feature_name in enumerate(SIMILARITY_FEATURE_COLUMNS)
    }
    modeling = modeling.assign(**normalized_columns)

    projection_coords, projection_axes = _project_similarity_vectors(model_values)
    for index, column in enumerate(PROJECTION_COLUMNS):
        modeling[column] = projection_coords[:, index]
    # Axis annotations are model-run metadata; store the same JSON on every row
    # so the serving layer can read it from the projection table.
    modeling["projection_axes"] = json.dumps(projection_axes)

    cluster_summaries: Dict[int, Dict[str, Any]] = {}
    for cluster_index in sorted(modeling["cluster_index"].unique()):
        cluster_rows = modeling[modeling["cluster_index"] == cluster_index]
        center = {
            feature_name: float(cluster_rows[f"norm_{feature_name}"].mean())
            for feature_name in SIMILARITY_FEATURE_COLUMNS
        }
        top_traits = _rank_similarity_traits(center, limit=3, positive_only=True)
        archetype_label = _label_cluster(center)
        cluster_summaries[int(cluster_index)] = {
            "archetype_id": f"cluster_{int(cluster_index)}",
            "base_archetype_label": archetype_label,
            "top_traits": ", ".join(top_traits),
            "center": center,
        }

    confidence_by_row: List[float] = []
    for row_index, cluster_index in enumerate(raw_cluster_ids):
        cluster_mask = raw_cluster_ids == cluster_index
        cluster_distances = distances[cluster_mask]
        max_distance = float(cluster_distances.max()) if len(cluster_distances) else 0.0
        if max_distance <= 1e-9:
            confidence = 1.0
        else:
            confidence = 1.0 - float(distances[row_index]) / max_distance
        confidence_by_row.append(round(max(0.0, min(confidence, 1.0)), 4))

    player_top_traits: List[str] = []
    player_bottom_traits: List[str] = []
    archetype_ids: List[str] = []
    archetype_labels: List[str] = []
    archetype_summaries: List[str] = []
    for _, row in modeling.iterrows():
        normalized_values = {
            feature_name: float(row[f"norm_{feature_name}"])
            for feature_name in SIMILARITY_FEATURE_COLUMNS
        }
        raw_values = {
            feature_name: row.get(feature_name)
            for feature_name in SIMILARITY_FEATURE_COLUMNS
        }
        raw_values.update({column: row.get(column) for column in TEAM_CONTEXT_COLUMNS})
        player_bottom_traits.append(
            ", ".join(
                _rank_similarity_traits(normalized_values, limit=2, negative_only=True)
            )
        )
        cluster_summary = cluster_summaries[int(row["cluster_index"])]
        base_label = _player_base_archetype_label(
            cluster_base_label=str(cluster_summary["base_archetype_label"]),
            normalized_values=normalized_values,
            raw_values=raw_values,
        )
        label, summary, top_traits = _player_archetype_display(
            base_label=base_label,
            normalized_values=normalized_values,
            cluster_index=int(row["cluster_index"]),
        )
        player_top_traits.append(top_traits)
        archetype_ids.append(cluster_summary["archetype_id"])
        archetype_labels.append(label)
        archetype_summaries.append(summary)

    modeling["cluster_confidence"] = confidence_by_row
    modeling["top_traits"] = player_top_traits
    modeling["contrasting_traits"] = player_bottom_traits
    modeling["archetype_id"] = archetype_ids
    modeling["archetype_label"] = archetype_labels
    modeling["archetype_summary"] = archetype_summaries

    features_df = modeling[
        [
            *OUTPUT_ID_COLUMNS,
            *TEAM_CONTEXT_COLUMNS,
            "archetype_id",
            "archetype_label",
            "cluster_confidence",
            "top_traits",
            "contrasting_traits",
            "archetype_summary",
            *SIMILARITY_FEATURE_COLUMNS,
            *[f"norm_{feature_name}" for feature_name in SIMILARITY_FEATURE_COLUMNS],
            *PROJECTION_COLUMNS,
            "projection_axes",
        ]
    ].copy()

    archetypes_df = modeling[
        [
            *OUTPUT_ID_COLUMNS,
            *TEAM_CONTEXT_COLUMNS,
            "archetype_id",
            "archetype_label",
            "cluster_confidence",
            "top_traits",
            "archetype_summary",
        ]
    ].copy()

    diagnostics = {
        "model_version": MODEL_VERSION,
        "model_scope": "public_reference_baseline",
        "input_rows": int(len(working)),
        "trained_rows": int(len(modeling)),
        "requested_clusters": int(cluster_count),
        "minimum_clusters": int(minimum_cluster_count),
        "effective_clusters": int(effective_clusters),
        "candidate_cluster_counts": _candidate_cluster_counts(
            requested_clusters=cluster_count,
            row_count=len(modeling),
            minimum_clusters=minimum_cluster_count,
        ),
        "random_state": RANDOM_STATE,
        "feature_columns": list(SIMILARITY_FEATURE_COLUMNS),
        "feature_weighting": "equal",
        "feature_scaler": FEATURE_SCALER,
        "vector_normalization": MODEL_VECTOR_NORMALIZATION,
        "projection_method": PROJECTION_METHOD,
        "projection_components": int(min(PROJECTION_COMPONENTS, len(modeling))),
        "projection_explained_variance": [axis["variance"] for axis in projection_axes],
        "projection_axes": projection_axes,
        "empty_imputed_features": empty_columns,
        "cluster_counts": {
            str(cluster_index): int(count)
            for cluster_index, count in modeling["cluster_index"].value_counts().items()
        },
        "kmeans_n_init": int(KMEANS_N_INIT),
    }

    _validate_similarity_output_frames(features_df, archetypes_df)
    return SimilarityTrainingResult(
        features=features_df,
        archetypes=archetypes_df,
        diagnostics=diagnostics,
    )


def build_player_similarity_outputs(
    feature_df: pd.DataFrame,
    *,
    cluster_count: int = 10,
) -> Dict[str, pd.DataFrame]:
    """Cluster players into public baseline archetypes."""
    result = train_player_similarity_model(feature_df, cluster_count=cluster_count)
    return {"features": result.features, "archetypes": result.archetypes}
