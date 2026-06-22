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

MODEL_VERSION = "public_similarity_multi_model_v1"
RANDOM_STATE = 42
MINIMUM_ARCHETYPE_CLUSTERS = 8
KMEANS_N_INIT = 10
FEATURE_SCALER = "standard"
MODEL_VECTOR_NORMALIZATION = "l2_equal_weight"
BASELINE_MODEL_KEY = "kmeans"
SIMILARITY_MODEL_LABELS = {
    "kmeans": "KMeans baseline",
    "gmm": "Gaussian mixture",
    "agglomerative": "Hierarchy",
    "hdbscan": "Density scan",
}
SIMILARITY_MODEL_DESCRIPTIONS = {
    "kmeans": "Fast, deterministic baseline that forces every player into a role.",
    "gmm": "Soft clustering for hybrid player profiles.",
    "agglomerative": "Hierarchy-style grouping that favors explainable role families.",
    "hdbscan": "Density-based grouping that can leave unusual profiles unclassified.",
}

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
    SimilarityFeatureSpec(
        "wingspan_inches", "physical_profile", trait_label="wingspan"
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
    "Role Scorer",
    "Secondary Creator",
    "Two-Way Wing",
    "Spacing Wing",
    "Defensive Specialist",
    "Connector Wing",
    "Utility Forward",
    "Stretch Big",
    "Interior Big",
    "Emerging Role",
    "Emerging Scorer",
    "Emerging Shooter",
    "Emerging Creator",
    "Emerging Stopper",
    "Emerging Big",
    "Emerging Connector",
}
ALLOWED_ARCHETYPE_LABELS = BASE_ARCHETYPE_LABELS
BROAD_ARCHETYPE_LABELS = {"Connector Wing", "Spacing Wing", "Utility Forward"}

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


def _feature_signal(values: Dict[str, float], *feature_names: str) -> float:
    scores = []
    for feature_name in feature_names:
        value = values.get(feature_name, 0.0)
        if value in (None, ""):
            continue
        try:
            if pd.isna(value):
                continue
        except TypeError:
            pass
        scores.append(float(value))
    return max(scores) if scores else 0.0


def _role_signal_scores(values: Dict[str, float]) -> Dict[str, float]:
    scoring = _feature_signal(
        values,
        "season_avg_pts",
        "season_avg_fga",
        "recent_pts",
        "recent_points_share_of_team",
        "recent_points_share_of_game",
        "team_points_contribution_rate",
        "team_fga_contribution_rate",
        "second_half_pts_delta",
    )
    creation = _feature_signal(
        values,
        "season_avg_ast",
        "recent_ast",
        "season_ast_to_tov",
        "team_ast_contribution_rate",
    )
    spacing = _feature_signal(
        values,
        "season_avg_fg3m",
        "recent_fg3m",
        "season_fg3a_rate",
        "shot_corner3_rate",
        "shot_above_break3_rate",
        "shot_corner3_fg_pct",
        "second_half_ts_delta",
    )
    defense = _feature_signal(
        values,
        "season_avg_stl",
        "recent_stl",
        "team_stl_contribution_rate",
        "team_defense_contribution_rate",
    )
    interior = _feature_signal(
        values,
        "season_avg_reb",
        "season_avg_blk",
        "recent_reb",
        "recent_blk",
        "team_reb_contribution_rate",
        "team_blk_contribution_rate",
        "height_inches",
        "weight_lbs",
        "wingspan_inches",
    )
    offense_context = _feature_signal(
        values,
        "team_offense_contribution_rate",
        "team_points_contribution_rate",
        "team_fga_contribution_rate",
        "team_ast_contribution_rate",
    )
    return {
        "scoring": scoring,
        "creation": creation,
        "spacing": spacing,
        "defense": defense,
        "interior": interior,
        "offense_context": offense_context,
    }


def _fallback_role_label(scores: Dict[str, float]) -> str:
    scoring = scores["scoring"]
    creation = scores["creation"]
    spacing = scores["spacing"]
    defense = scores["defense"]
    interior = scores["interior"]
    offense_context = scores["offense_context"]

    if scoring >= 0.45 and creation < 0.55:
        return "Role Scorer"
    if creation >= 0.35 and offense_context >= 0.10:
        return "Secondary Creator"
    if spacing >= 0.25:
        return "Spacing Wing"
    if defense >= 0.25:
        return "Defensive Specialist"
    if interior >= 0.25:
        return "Utility Forward"
    if scoring >= 0.25:
        return "Role Scorer"

    fallback_options = [
        ("spacing", "Spacing Wing"),
        ("creation", "Secondary Creator"),
        ("defense", "Defensive Specialist"),
        ("interior", "Utility Forward"),
        ("scoring", "Role Scorer"),
    ]
    signal_name, label = max(fallback_options, key=lambda item: scores[item[0]])
    if scores[signal_name] >= 0.10:
        return label
    return "Connector Wing"


def _emerging_base_archetype_label(normalized_values: Dict[str, float]) -> str:
    scores = _role_signal_scores(normalized_values)
    emerging_options = [
        ("scoring", "Emerging Scorer"),
        ("spacing", "Emerging Shooter"),
        ("creation", "Emerging Creator"),
        ("defense", "Emerging Stopper"),
        ("interior", "Emerging Big"),
    ]
    signal_name, label = max(emerging_options, key=lambda item: scores[item[0]])
    best_score = scores[signal_name]
    if best_score >= 0.15:
        return label
    if best_score >= 0.0:
        return "Emerging Connector"
    return "Emerging Role"


def _label_cluster(
    center_values: Dict[str, float], *, allow_primary_creator: bool = True
) -> str:
    """Map a baseline cluster center to a human-readable public archetype."""
    scores = _role_signal_scores(center_values)
    scoring = scores["scoring"]
    creation = scores["creation"]
    spacing = scores["spacing"]
    defense = scores["defense"]
    interior = scores["interior"]
    offense_context = scores["offense_context"]

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
    return _fallback_role_label(scores)


def _archetype_id_suffix(label: str) -> str:
    return (
        label.lower()
        .replace(" - ", "_")
        .replace("/", "_")
        .replace(" ", "_")
        .replace("-", "_")
    )


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
    if cluster_base_label == "Primary Creator":
        team_offense_rank = _numeric_value(
            raw_values,
            "team_offense_contribution_rank",
            default=999.0,
        )
        if team_offense_rank <= 1:
            return "Primary Creator"
        return _label_cluster(normalized_values, allow_primary_creator=False)

    if cluster_base_label in BROAD_ARCHETYPE_LABELS:
        return _label_cluster(normalized_values, allow_primary_creator=False)

    return cluster_base_label


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


def _centroid_confidence(model_values: Any, labels: Any) -> List[float]:
    import numpy as np

    labels = np.asarray(labels)
    confidence = np.zeros(len(labels), dtype=float)
    for label in sorted(set(labels.tolist())):
        mask = labels == label
        if label < 0 or not mask.any():
            continue
        cluster_values = model_values[mask]
        center = cluster_values.mean(axis=0)
        distances = np.linalg.norm(cluster_values - center, axis=1)
        max_distance = float(distances.max()) if len(distances) else 0.0
        if max_distance <= 1e-9:
            confidence[mask] = 1.0
        else:
            confidence[mask] = 1.0 - distances / max_distance
    return [round(float(max(0.0, min(value, 1.0))), 4) for value in confidence]


def _evaluate_cluster_labels(model_values: Any, labels: Any) -> Dict[str, Any]:
    import numpy as np
    from sklearn.metrics import silhouette_score

    labels = np.asarray(labels)
    row_count = int(len(labels))
    assigned_mask = labels >= 0
    assigned_count = int(assigned_mask.sum())
    coverage = assigned_count / row_count if row_count else 0.0
    assigned_labels = labels[assigned_mask]
    cluster_counts = {
        str(int(label)): int(count)
        for label, count in zip(
            *np.unique(assigned_labels, return_counts=True), strict=False
        )
    }
    noise_count = int(row_count - assigned_count)
    cluster_count = len(cluster_counts)
    max_share = (
        max(cluster_counts.values()) / assigned_count
        if assigned_count and cluster_counts
        else 1.0
    )
    balance_score = max(0.0, 1.0 - max_share)

    silhouette = 0.0
    silhouette_normalized = 0.0
    if cluster_count >= 2 and assigned_count > cluster_count:
        try:
            silhouette = float(
                silhouette_score(model_values[assigned_mask], assigned_labels)
            )
            silhouette_normalized = max(0.0, min((silhouette + 1.0) / 2.0, 1.0))
        except ValueError:
            silhouette = 0.0
    score = 0.55 * silhouette_normalized + 0.25 * balance_score + 0.20 * coverage
    return {
        "score": round(float(score), 4),
        "silhouette": round(float(silhouette), 4),
        "silhouette_normalized": round(float(silhouette_normalized), 4),
        "balance_score": round(float(balance_score), 4),
        "coverage_score": round(float(coverage), 4),
        "assigned_player_count": assigned_count,
        "unclassified_player_count": noise_count,
        "cluster_count": cluster_count,
        "cluster_counts": cluster_counts,
    }


def _fit_similarity_model_candidates(
    model_values: Any,
    *,
    effective_clusters: int,
) -> Dict[str, Dict[str, Any]]:
    import numpy as np
    from sklearn.cluster import HDBSCAN, AgglomerativeClustering, KMeans
    from sklearn.mixture import GaussianMixture

    candidates: Dict[str, Dict[str, Any]] = {}

    kmeans = KMeans(
        n_clusters=effective_clusters,
        n_init=KMEANS_N_INIT,
        random_state=RANDOM_STATE,
    )
    kmeans_labels = kmeans.fit_predict(model_values)
    distances = np.linalg.norm(
        model_values - kmeans.cluster_centers_[kmeans_labels], axis=1
    )
    confidence_by_row: List[float] = []
    for row_index, cluster_index in enumerate(kmeans_labels):
        cluster_distances = distances[kmeans_labels == cluster_index]
        max_distance = float(cluster_distances.max()) if len(cluster_distances) else 0.0
        if max_distance <= 1e-9:
            confidence = 1.0
        else:
            confidence = 1.0 - float(distances[row_index]) / max_distance
        confidence_by_row.append(round(max(0.0, min(confidence, 1.0)), 4))
    candidates["kmeans"] = {
        "labels": kmeans_labels,
        "confidence": confidence_by_row,
        "metadata": {"n_init": KMEANS_N_INIT},
    }

    gmm = GaussianMixture(
        n_components=effective_clusters,
        covariance_type="full",
        n_init=KMEANS_N_INIT,
        random_state=RANDOM_STATE,
    )
    gmm_labels = gmm.fit_predict(model_values)
    gmm_probs = gmm.predict_proba(model_values).max(axis=1)
    candidates["gmm"] = {
        "labels": gmm_labels,
        "confidence": [round(float(value), 4) for value in gmm_probs],
        "metadata": {"n_components": effective_clusters},
    }

    agglomerative = AgglomerativeClustering(n_clusters=effective_clusters)
    agglomerative_labels = agglomerative.fit_predict(model_values)
    candidates["agglomerative"] = {
        "labels": agglomerative_labels,
        "confidence": _centroid_confidence(model_values, agglomerative_labels),
        "metadata": {"n_clusters": effective_clusters},
    }

    min_cluster_size = max(2, min(8, len(model_values) // 6 or 2))
    hdbscan = HDBSCAN(min_cluster_size=min_cluster_size, min_samples=1)
    hdbscan_labels = hdbscan.fit_predict(model_values)
    probabilities = getattr(hdbscan, "probabilities_", None)
    if probabilities is None:
        hdbscan_confidence = [
            0.0 if int(label) < 0 else 1.0 for label in hdbscan_labels
        ]
    else:
        hdbscan_confidence = [round(float(value), 4) for value in probabilities]
    candidates["hdbscan"] = {
        "labels": hdbscan_labels,
        "confidence": hdbscan_confidence,
        "metadata": {"min_cluster_size": min_cluster_size, "min_samples": 1},
    }

    for key, payload in candidates.items():
        payload["evaluation"] = _evaluate_cluster_labels(
            model_values, payload["labels"]
        )
        payload["evaluation"]["model_key"] = key
        payload["evaluation"]["model_label"] = SIMILARITY_MODEL_LABELS[key]
        payload["evaluation"]["description"] = SIMILARITY_MODEL_DESCRIPTIONS[key]

    return candidates


def _select_recommended_model_key(candidates: Dict[str, Dict[str, Any]]) -> str:
    order = list(SIMILARITY_MODEL_LABELS)
    return max(
        candidates,
        key=lambda key: (
            candidates[key]["evaluation"]["score"],
            -order.index(key),
        ),
    )


def _build_model_assignment_results(
    modeling: pd.DataFrame,
    *,
    labels: Any,
    confidences: List[float],
    model_key: str,
) -> List[Dict[str, Any]]:
    import numpy as np

    labels = np.asarray(labels)
    cluster_summaries: Dict[int, Dict[str, Any]] = {}
    for cluster_index in sorted(set(labels.tolist())):
        cluster_rows = modeling[labels == cluster_index]
        center = {
            feature_name: float(cluster_rows[f"norm_{feature_name}"].mean())
            for feature_name in SIMILARITY_FEATURE_COLUMNS
        }
        top_traits = _rank_similarity_traits(center, limit=3, positive_only=True)
        base_label = "Emerging Role" if cluster_index < 0 else _label_cluster(center)
        cluster_summaries[int(cluster_index)] = {
            "archetype_id": f"{model_key}_{'unclassified' if cluster_index < 0 else int(cluster_index)}",
            "base_archetype_label": base_label,
            "top_traits": ", ".join(top_traits),
            "center": center,
            "cluster_size": int(len(cluster_rows)),
        }

    results: List[Dict[str, Any]] = []
    for row_index, (_, row) in enumerate(modeling.iterrows()):
        cluster_index = int(labels[row_index])
        normalized_values = {
            feature_name: float(row[f"norm_{feature_name}"])
            for feature_name in SIMILARITY_FEATURE_COLUMNS
        }
        raw_values = {
            feature_name: row.get(feature_name)
            for feature_name in SIMILARITY_FEATURE_COLUMNS
        }
        raw_values.update({column: row.get(column) for column in TEAM_CONTEXT_COLUMNS})
        cluster_summary = cluster_summaries[cluster_index]
        if cluster_index < 0:
            base_label = _emerging_base_archetype_label(normalized_values)
        else:
            base_label = _player_base_archetype_label(
                cluster_base_label=str(cluster_summary["base_archetype_label"]),
                normalized_values=normalized_values,
                raw_values=raw_values,
            )
        label, summary, top_traits = _player_archetype_display(
            base_label=base_label,
            normalized_values=normalized_values,
            cluster_index=cluster_index,
        )
        if cluster_index < 0:
            summary = (
                f"{label} is an outlier profile for this model; it did not find "
                "a dense enough peer group."
            )
            archetype_id = f"{model_key}_{_archetype_id_suffix(base_label)}"
        else:
            archetype_id = cluster_summary["archetype_id"]
        results.append(
            {
                "model_key": model_key,
                "model_label": SIMILARITY_MODEL_LABELS[model_key],
                "description": SIMILARITY_MODEL_DESCRIPTIONS[model_key],
                "archetype_id": archetype_id,
                "archetype_label": label,
                "base_archetype_label": label.split(" - ", maxsplit=1)[0],
                "cluster_confidence": round(float(confidences[row_index]), 4),
                "top_traits": top_traits,
                "contrasting_traits": ", ".join(
                    _rank_similarity_traits(
                        normalized_values, limit=2, negative_only=True
                    )
                ),
                "archetype_summary": summary,
                "cluster_size": cluster_summary["cluster_size"],
            }
        )
    return results


def train_player_similarity_model(
    feature_df: pd.DataFrame,
    *,
    cluster_count: int = 10,
    minimum_cluster_count: int = MINIMUM_ARCHETYPE_CLUSTERS,
) -> SimilarityTrainingResult:
    """Train the public reference archetype baseline."""
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

    candidates = _fit_similarity_model_candidates(
        model_values,
        effective_clusters=effective_clusters,
    )
    recommended_model_key = _select_recommended_model_key(candidates)
    model_evaluations = [
        candidates[key]["evaluation"] for key in SIMILARITY_MODEL_LABELS
    ]
    model_evaluation_json = json.dumps(
        {
            "recommended_model_key": recommended_model_key,
            "baseline_model_key": BASELINE_MODEL_KEY,
            "models": model_evaluations,
        }
    )

    per_model_assignments = {
        model_key: _build_model_assignment_results(
            modeling,
            labels=payload["labels"],
            confidences=payload["confidence"],
            model_key=model_key,
        )
        for model_key, payload in candidates.items()
    }
    active_assignments = per_model_assignments[recommended_model_key]
    modeling["active_model_key"] = recommended_model_key
    modeling["recommended_model_key"] = recommended_model_key
    modeling["model_evaluation_json"] = model_evaluation_json
    modeling["model_results_json"] = [
        json.dumps(
            {
                "recommended_model_key": recommended_model_key,
                "baseline_model_key": BASELINE_MODEL_KEY,
                "models": [
                    {
                        **per_model_assignments[model_key][row_index],
                        "is_recommended": model_key == recommended_model_key,
                        "is_baseline": model_key == BASELINE_MODEL_KEY,
                    }
                    for model_key in SIMILARITY_MODEL_LABELS
                ],
            }
        )
        for row_index in range(len(modeling))
    ]

    modeling["cluster_confidence"] = [
        item["cluster_confidence"] for item in active_assignments
    ]
    modeling["top_traits"] = [item["top_traits"] for item in active_assignments]
    modeling["contrasting_traits"] = [
        item["contrasting_traits"] for item in active_assignments
    ]
    modeling["archetype_id"] = [item["archetype_id"] for item in active_assignments]
    modeling["archetype_label"] = [
        item["archetype_label"] for item in active_assignments
    ]
    modeling["archetype_summary"] = [
        item["archetype_summary"] for item in active_assignments
    ]

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
            "active_model_key",
            "recommended_model_key",
            "model_results_json",
            "model_evaluation_json",
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
            "active_model_key",
            "recommended_model_key",
            "model_results_json",
            "model_evaluation_json",
        ]
    ].copy()

    diagnostics = {
        "model_version": MODEL_VERSION,
        "model_scope": "public_reference_multi_model",
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
        "baseline_model_key": BASELINE_MODEL_KEY,
        "candidate_models": list(SIMILARITY_MODEL_LABELS),
        "recommended_model_key": recommended_model_key,
        "model_evaluations": model_evaluations,
        "projection_method": PROJECTION_METHOD,
        "projection_components": int(min(PROJECTION_COMPONENTS, len(modeling))),
        "projection_explained_variance": [axis["variance"] for axis in projection_axes],
        "projection_axes": projection_axes,
        "empty_imputed_features": empty_columns,
        "cluster_counts": candidates[recommended_model_key]["evaluation"][
            "cluster_counts"
        ],
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
