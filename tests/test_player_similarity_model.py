from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dags"))

import player_similarity_model as model


def _feature_row(
    player_id: int,
    player_name: str,
    *,
    pts: float,
    fga: float,
    ts_pct: float,
    fg3a_rate: float,
    fta_rate: float,
    ast: float,
    reb: float,
    stl: float,
    blk: float,
    usage: float,
    height_inches: float = 78,
    weight_lbs: float = 210,
    season_exp: float = 4,
    team_abbr: str = "TST",
    team_offense_rank: int = 99,
    team_defense_rank: int = 99,
    team_points_share: float | None = None,
    team_fga_share: float | None = None,
    team_ast_share: float | None = None,
    team_offense_share: float | None = None,
    team_defense_share: float | None = None,
    second_half_pts_delta: float = 0.0,
    second_half_min_delta: float = 0.0,
    second_half_ts_delta: float = 0.0,
) -> dict:
    row = {
        "season": "2025-26",
        "as_of_date": "2026-02-11",
        "player_id": player_id,
        "player_name": player_name,
        "team_abbr": team_abbr,
        "position": "G",
        "games_sampled": 24,
        "sample_status": "ready",
        "team_offense_contribution_rank": team_offense_rank,
        "team_defense_contribution_rank": team_defense_rank,
    }
    row.update({feature_name: 0.0 for feature_name in model.SIMILARITY_FEATURE_COLUMNS})
    row.update(
        {
            "season_avg_pts": pts,
            "season_avg_fga": fga,
            "season_fg_pct": ts_pct - 0.05,
            "season_ts_pct": ts_pct,
            "season_fg3a_rate": fg3a_rate,
            "season_fta_rate": fta_rate,
            "shot_rim_rate": max(fta_rate, 0.05),
            "shot_paint_non_ra_rate": 0.12,
            "shot_midrange_rate": 0.18,
            "shot_corner3_rate": fg3a_rate / 3,
            "shot_above_break3_rate": fg3a_rate,
            "shot_rim_fg_pct": min(ts_pct + 0.08, 0.85),
            "shot_corner3_fg_pct": 0.38,
            "season_ast_to_tov": max(ast / 2.0, 0.1),
            "team_points_contribution_rate": team_points_share
            if team_points_share is not None
            else usage,
            "team_fga_contribution_rate": team_fga_share
            if team_fga_share is not None
            else min(fga / 90.0, 0.45),
            "team_ast_contribution_rate": team_ast_share
            if team_ast_share is not None
            else min(ast / 28.0, 0.45),
            "team_tov_contribution_rate": min(max(ast / 3.0, 0.4) / 15.0, 0.45),
            "team_offense_contribution_rate": team_offense_share
            if team_offense_share is not None
            else usage,
            "team_reb_contribution_rate": min(reb / 45.0, 0.45),
            "team_stl_contribution_rate": min(stl / 9.0, 0.45),
            "team_blk_contribution_rate": min(blk / 7.0, 0.45),
            "team_defense_contribution_rate": team_defense_share
            if team_defense_share is not None
            else min((reb / 45.0 + stl / 9.0 + blk / 7.0) / 3.0, 0.45),
            "season_avg_reb": reb,
            "season_avg_ast": ast,
            "season_avg_stl": stl,
            "season_avg_blk": blk,
            "season_avg_fg3m": fg3a_rate * 4,
            "season_avg_tov": max(ast / 3.0, 0.4),
            "season_avg_min": 30 + usage * 10,
            "height_inches": height_inches,
            "weight_lbs": weight_lbs,
            "season_exp": season_exp,
            "recent_pts": pts + 1,
            "recent_reb": reb,
            "recent_ast": ast,
            "recent_stl": stl,
            "recent_blk": blk,
            "recent_fg3m": fg3a_rate * 4,
            "recent_tov": max(ast / 3.0, 0.4),
            "recent_min": 31 + usage * 10,
            "recent_points_share_of_team": usage,
            "recent_points_share_of_game": usage / 2,
            "minutes_delta_vs_season": 0.2,
            "second_half_pts_delta": second_half_pts_delta,
            "second_half_min_delta": second_half_min_delta,
            "second_half_ts_delta": second_half_ts_delta,
        }
    )
    return row


def _training_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            _feature_row(
                1,
                "Creator A",
                pts=29,
                fga=21,
                ts_pct=0.61,
                fg3a_rate=0.38,
                fta_rate=0.34,
                ast=8,
                reb=4,
                stl=1.1,
                blk=0.3,
                usage=0.32,
            ),
            _feature_row(
                2,
                "Creator B",
                pts=27,
                fga=20,
                ts_pct=0.59,
                fg3a_rate=0.35,
                fta_rate=0.31,
                ast=7,
                reb=4,
                stl=0.9,
                blk=0.2,
                usage=0.30,
            ),
            _feature_row(
                3,
                "Wing A",
                pts=15,
                fga=11,
                ts_pct=0.58,
                fg3a_rate=0.58,
                fta_rate=0.12,
                ast=2,
                reb=5,
                stl=1.4,
                blk=0.7,
                usage=0.17,
            ),
            _feature_row(
                4,
                "Wing B",
                pts=13,
                fga=10,
                ts_pct=0.57,
                fg3a_rate=0.55,
                fta_rate=0.10,
                ast=2,
                reb=6,
                stl=1.3,
                blk=0.8,
                usage=0.16,
            ),
            _feature_row(
                5,
                "Big A",
                pts=14,
                fga=9,
                ts_pct=0.66,
                fg3a_rate=0.04,
                fta_rate=0.45,
                ast=1,
                reb=11,
                stl=0.5,
                blk=2.1,
                usage=0.18,
                height_inches=83,
                weight_lbs=245,
            ),
            _feature_row(
                6,
                "Big B",
                pts=12,
                fga=8,
                ts_pct=0.64,
                fg3a_rate=0.03,
                fta_rate=0.42,
                ast=1,
                reb=10,
                stl=0.4,
                blk=2.0,
                usage=0.16,
                height_inches=84,
                weight_lbs=250,
            ),
        ]
    )


def test_train_player_similarity_model_returns_diagnostics_and_new_style_features():
    result = model.train_player_similarity_model(_training_frame(), cluster_count=3)

    assert result.diagnostics["model_version"] == model.MODEL_VERSION
    assert result.diagnostics["effective_clusters"] == 3
    assert result.diagnostics["candidate_cluster_counts"] == [3]
    assert (
        result.diagnostics["vector_normalization"] == model.MODEL_VECTOR_NORMALIZATION
    )
    assert result.diagnostics["feature_scaler"] == model.FEATURE_SCALER
    assert result.diagnostics["kmeans_n_init"] == model.KMEANS_N_INIT
    assert "season_ts_pct" in result.diagnostics["feature_columns"]
    assert "shot_rim_rate" in result.diagnostics["feature_columns"]
    assert "height_inches" in result.diagnostics["feature_columns"]
    assert "second_half_pts_delta" in result.diagnostics["feature_columns"]
    assert result.features["player_id"].tolist() == [1, 2, 3, 4, 5, 6]
    assert "norm_season_ts_pct" in result.features.columns
    assert "norm_season_fg3a_rate" in result.features.columns
    assert "norm_shot_rim_rate" in result.features.columns
    assert "norm_height_inches" in result.features.columns
    assert "norm_second_half_pts_delta" in result.features.columns
    assert "team_offense_contribution_rank" in result.features.columns
    assert "team_defense_contribution_rank" in result.archetypes.columns
    assert result.features["top_traits"].str.len().gt(0).all()
    assert set(
        result.archetypes["archetype_label"].str.split(" - ", n=1).str[0]
    ).issubset(model.BASE_ARCHETYPE_LABELS)


def test_train_player_similarity_model_enforces_cluster_floor_when_supported():
    training_rows = []
    for index in range(12):
        training_rows.append(
            _feature_row(
                100 + index,
                f"Player {index}",
                pts=8 + index * 1.7,
                fga=6 + index * 1.2,
                ts_pct=0.50 + (index % 6) * 0.025,
                fg3a_rate=0.05 + (index % 5) * 0.12,
                fta_rate=0.08 + (index % 4) * 0.08,
                ast=1 + (index % 7) * 0.9,
                reb=2 + (index % 6) * 1.4,
                stl=0.2 + (index % 4) * 0.25,
                blk=0.1 + (index % 5) * 0.35,
                usage=0.08 + (index % 8) * 0.025,
                height_inches=72 + index,
                weight_lbs=180 + index * 6,
                season_exp=index % 10,
                second_half_pts_delta=(index % 5) - 2,
                second_half_min_delta=(index % 7) - 3,
                second_half_ts_delta=((index % 5) - 2) * 0.015,
            )
        )

    result = model.train_player_similarity_model(
        pd.DataFrame(training_rows),
        cluster_count=4,
    )

    assert result.diagnostics["candidate_cluster_counts"] == [
        model.MINIMUM_ARCHETYPE_CLUSTERS
    ]
    assert result.diagnostics["effective_clusters"] == model.MINIMUM_ARCHETYPE_CLUSTERS
    assert result.features["archetype_id"].nunique() == model.MINIMUM_ARCHETYPE_CLUSTERS


def test_label_cluster_requires_team_offensive_context_for_primary_creator():
    label = model._label_cluster(
        {
            "season_avg_pts": 0.8,
            "season_avg_fga": 0.8,
            "season_avg_ast": 0.8,
            "recent_points_share_of_team": 0.2,
            "team_points_contribution_rate": 0.1,
            "team_fga_contribution_rate": 0.1,
            "team_ast_contribution_rate": 0.1,
            "team_offense_contribution_rate": 0.1,
            "season_ast_to_tov": 0.2,
            "season_fta_rate": 0.2,
        }
    )

    assert label != "Primary Creator"


def test_label_cluster_uses_team_offensive_ownership_for_primary_creator():
    label = model._label_cluster(
        {
            "season_avg_pts": 0.8,
            "season_avg_fga": 0.7,
            "season_avg_ast": 0.8,
            "recent_points_share_of_team": 0.6,
            "team_points_contribution_rate": 0.75,
            "team_fga_contribution_rate": 0.7,
            "team_ast_contribution_rate": 0.8,
            "team_offense_contribution_rate": 0.85,
            "season_ast_to_tov": 0.2,
        }
    )

    assert label == "Primary Creator"


def test_primary_creator_label_is_limited_to_top_team_offensive_hub():
    training_frame = _training_frame()
    contribution_updates = {
        1: {
            "team_offense_contribution_rank": 1,
            "team_points_contribution_rate": 0.29,
            "team_fga_contribution_rate": 0.27,
            "team_ast_contribution_rate": 0.34,
            "team_offense_contribution_rate": 0.30,
        },
        2: {
            "team_offense_contribution_rank": 2,
            "team_points_contribution_rate": 0.27,
            "team_fga_contribution_rate": 0.25,
            "team_ast_contribution_rate": 0.30,
            "team_offense_contribution_rate": 0.27,
        },
    }
    for player_id, updates in contribution_updates.items():
        mask = training_frame["player_id"] == player_id
        for column, value in updates.items():
            training_frame.loc[mask, column] = value

    result = model.train_player_similarity_model(training_frame, cluster_count=3)
    primary_creators = result.features[
        result.features["archetype_label"].str.startswith("Primary Creator")
    ]

    assert primary_creators["player_id"].tolist() == [1]
    assert primary_creators["team_offense_contribution_rank"].tolist() == [1]


def test_train_player_similarity_model_is_deterministic_after_input_shuffle():
    training_frame = _training_frame()
    shuffled_frame = training_frame.sample(frac=1, random_state=99)

    first = model.train_player_similarity_model(
        training_frame, cluster_count=3
    ).features
    second = model.train_player_similarity_model(
        shuffled_frame, cluster_count=3
    ).features

    compare_columns = [
        "player_id",
        "archetype_id",
        "archetype_label",
        "top_traits",
        "norm_season_ts_pct",
        "norm_season_fg3a_rate",
        "norm_recent_points_share_of_team",
    ]
    pd.testing.assert_frame_equal(
        first[compare_columns].reset_index(drop=True),
        second[compare_columns].reset_index(drop=True),
        check_dtype=False,
    )


def test_label_cluster_prefers_interior_profile_before_scoring_guard():
    label = model._label_cluster(
        {
            "season_avg_pts": 0.8,
            "season_avg_fga": 0.7,
            "season_avg_fg3m": -0.3,
            "season_fg3a_rate": -0.4,
            "season_fta_rate": 0.2,
            "season_ts_pct": 0.1,
            "season_avg_reb": 1.1,
            "season_avg_ast": -0.2,
            "season_avg_stl": 0.0,
            "season_avg_blk": 1.2,
            "recent_points_share_of_team": 0.6,
            "season_ast_to_tov": -0.1,
        }
    )

    assert label == "Interior Big"


def test_label_cluster_uses_physical_size_as_interior_context():
    label = model._label_cluster(
        {
            "season_avg_pts": 0.0,
            "season_avg_fga": 0.0,
            "season_avg_fg3m": -0.4,
            "season_fg3a_rate": -0.5,
            "season_fta_rate": 0.0,
            "season_ts_pct": 0.0,
            "season_avg_reb": 0.1,
            "season_avg_ast": -0.2,
            "season_avg_stl": 0.0,
            "season_avg_blk": 0.2,
            "height_inches": 1.5,
            "weight_lbs": 1.2,
            "recent_points_share_of_team": 0.0,
            "season_ast_to_tov": -0.1,
        }
    )

    assert label == "Interior Big"


def test_train_player_similarity_model_emits_projection_columns() -> None:
    result = model.train_player_similarity_model(_training_frame(), cluster_count=3)

    for column in model.PROJECTION_COLUMNS:
        assert column in result.features.columns
        assert result.features[column].notna().all()

    assert result.diagnostics["projection_method"] == model.PROJECTION_METHOD
    variance = result.diagnostics["projection_explained_variance"]
    assert len(variance) == model.PROJECTION_COMPONENTS
    assert all(0.0 <= value <= 1.0 for value in variance)


def test_train_player_similarity_model_projection_is_deterministic() -> None:
    first = model.train_player_similarity_model(_training_frame(), cluster_count=3)
    second = model.train_player_similarity_model(_training_frame(), cluster_count=3)

    columns = list(model.PROJECTION_COLUMNS)
    assert first.features[columns].equals(second.features[columns])


def test_train_player_similarity_model_emits_axis_drivers() -> None:
    result = model.train_player_similarity_model(_training_frame(), cluster_count=3)

    raw = result.features["projection_axes"].iloc[0]
    axes = json.loads(raw)
    assert [axis["key"] for axis in axes] == list(model.PROJECTION_COLUMNS)
    # Every populated component reports a variance share and human-readable
    # driving features (mapped from the model's trait labels).
    assert all(0.0 <= axis["variance"] <= 1.0 for axis in axes)
    assert axes[0]["drivers"], "first component should have driving features"
    assert all(isinstance(name, str) for name in axes[0]["drivers"])
    assert result.diagnostics["projection_axes"] == axes
