from __future__ import annotations

import json
from math import sqrt
from typing import Any

from google.api_core.exceptions import GoogleAPIError as BQAPIError
from google.cloud import bigquery

from app.repository._bq.base import BigQueryBaseMixin
from app.repository._constants import (
    SIMILARITY_FEATURE_COLUMNS,
    SIMILARITY_FEATURE_WEIGHTS,
    SIMILARITY_RESULT_LIMIT,
    STATE_FRESH,
    STATE_INSUFFICIENT_SAMPLE,
    STATE_UNAVAILABLE,
)
from app.repository._helpers import (
    _contrasting_similarity_traits,
    _guard_served_archetype_label,
    _guard_served_archetype_summary,
    _shared_similarity_traits,
    _similarity_state_from_sample_status,
    _split_display_list,
    _to_float,
    _to_int,
    _weighted_similarity_vector,
    build_headshot_url,
    build_player_initials,
)


class BigQuerySimilarityMixin(BigQueryBaseMixin):
    def _similarity_distance_sql(self, anchor_alias: str, candidate_alias: str) -> str:
        terms = []
        for feature_name in SIMILARITY_FEATURE_COLUMNS:
            weight = SIMILARITY_FEATURE_WEIGHTS.get(feature_name, 1.0)
            anchor_component = (
                f"COALESCE(SAFE_DIVIDE("
                f"COALESCE({anchor_alias}.norm_{feature_name}, 0) * {weight}, "
                f"NULLIF({anchor_alias}.similarity_vector_norm, 0)), 0)"
            )
            candidate_component = (
                f"COALESCE(SAFE_DIVIDE("
                f"COALESCE({candidate_alias}.norm_{feature_name}, 0) * {weight}, "
                f"NULLIF({candidate_alias}.similarity_vector_norm, 0)), 0)"
            )
            terms.append(f"POW({candidate_component} - {anchor_component}, 2)")
        return " + ".join(terms)

    def _similarity_vector_norm_sql(self, alias: str) -> str:
        terms = []
        for feature_name in SIMILARITY_FEATURE_COLUMNS:
            weight = SIMILARITY_FEATURE_WEIGHTS.get(feature_name, 1.0)
            terms.append(f"POW(COALESCE({alias}.norm_{feature_name}, 0) * {weight}, 2)")
        return f"SQRT({' + '.join(terms)})"

    def _fetch_similarity_anchor(self, player_id: int) -> dict[str, Any] | None:
        sql = f"""
        SELECT
          *
        FROM {self._similarity_feature_table()}
        WHERE season = @season
          AND player_id = @player_id
        LIMIT 1
        """
        try:
            rows = self._query(
                sql,
                [
                    bigquery.ScalarQueryParameter(
                        "season", "STRING", self.settings.season
                    ),
                    bigquery.ScalarQueryParameter("player_id", "INT64", player_id),
                ],
            )
        except BQAPIError:
            return None
        return rows[0] if rows else None

    def _get_similar_players(
        self,
        player_id: int,
        *,
        anchor: dict[str, Any] | None = None,
        limit: int = SIMILARITY_RESULT_LIMIT,
    ) -> tuple[str, str | None, list[dict[str, Any]]]:
        if anchor is None:
            anchor = self._fetch_similarity_anchor(player_id)
        if anchor is None:
            return STATE_UNAVAILABLE, "Similarity profile is unavailable.", []

        anchor_state = _similarity_state_from_sample_status(anchor.get("sample_status"))
        if anchor_state == STATE_INSUFFICIENT_SAMPLE:
            return (
                anchor_state,
                "Not enough games are available to generate similar players yet.",
                [],
            )

        normalized_fields = ",\n          ".join(
            [
                f"candidate.norm_{feature_name}"
                for feature_name in SIMILARITY_FEATURE_COLUMNS
            ]
        )
        distance_sql = self._similarity_distance_sql("anchor", "candidate")
        sql = f"""
        WITH anchor_raw AS (
          SELECT *
          FROM {self._similarity_feature_table()}
          WHERE season = @season
            AND player_id = @player_id
          LIMIT 1
        ),
        anchor AS (
          SELECT
            anchor_raw.*,
            {self._similarity_vector_norm_sql("anchor_raw")} AS similarity_vector_norm
          FROM anchor_raw
        ),
        candidate_pool AS (
          SELECT
            candidate.*,
            {self._similarity_vector_norm_sql("candidate")} AS similarity_vector_norm
          FROM {self._similarity_feature_table()} candidate
          WHERE candidate.season = @season
            AND candidate.sample_status IN ('ready', 'limited_sample')
        ),
        scored AS (
          SELECT
            candidate.player_id,
            candidate.player_name,
            candidate.team_abbr,
            candidate.position,
            candidate.height_inches,
            candidate.weight_lbs,
            candidate.wingspan_inches,
            candidate.archetype_label,
            candidate.cluster_confidence,
            candidate.top_traits,
            candidate.contrasting_traits,
            candidate.sample_status,
            {normalized_fields},
            SQRT({distance_sql}) AS euclidean_distance
          FROM candidate_pool candidate
          CROSS JOIN anchor
          WHERE candidate.player_id != anchor.player_id
        )
        SELECT
          *,
          ROUND(1 / (1 + euclidean_distance), 4) AS similarity_score
        FROM scored
        ORDER BY euclidean_distance ASC, player_name
        LIMIT @limit
        """
        try:
            rows = self._query(
                sql,
                [
                    bigquery.ScalarQueryParameter(
                        "season", "STRING", self.settings.season
                    ),
                    bigquery.ScalarQueryParameter("player_id", "INT64", player_id),
                    bigquery.ScalarQueryParameter("limit", "INT64", limit),
                ],
            )
        except BQAPIError:
            return STATE_UNAVAILABLE, "Similarity profile is unavailable.", []

        items: list[dict[str, Any]] = []
        for row in rows:
            shared_traits = _shared_similarity_traits(anchor, row)
            if not shared_traits:
                shared_traits = [
                    trait
                    for trait in _split_display_list(anchor.get("top_traits"))
                    if trait in _split_display_list(row.get("top_traits"))
                ][:3]
            items.append(
                {
                    "player_id": _to_int(row.get("player_id")),
                    "player_name": row.get("player_name"),
                    "team_abbr": row.get("team_abbr"),
                    "headshot_url": build_headshot_url(row.get("player_id")),
                    "player_initials": build_player_initials(row.get("player_name")),
                    "similarity_score": _to_float(row.get("similarity_score")),
                    "archetype_label": _guard_served_archetype_label(
                        row.get("archetype_label"), row
                    ),
                    "shared_traits": shared_traits,
                    "contrasting_traits": _contrasting_similarity_traits(anchor, row),
                }
            )

        if not items:
            return STATE_UNAVAILABLE, "No similar-player matches are available.", []
        return STATE_FRESH, None, items

    def _get_pair_similarity(
        self, player_a_id: int, player_b_id: int
    ) -> dict[str, Any]:
        player_a = self._fetch_similarity_anchor(player_a_id)
        player_b = self._fetch_similarity_anchor(player_b_id)
        if player_a is None or player_b is None:
            return {
                "state": STATE_UNAVAILABLE,
                "score": None,
                "summary": "Similarity profile is unavailable for at least one player.",
                "same_archetype": False,
                "archetype_labels": [],
                "shared_traits": [],
                "contrasting_traits": [],
            }

        state_a = _similarity_state_from_sample_status(player_a.get("sample_status"))
        state_b = _similarity_state_from_sample_status(player_b.get("sample_status"))
        if STATE_INSUFFICIENT_SAMPLE in (state_a, state_b):
            return {
                "state": STATE_INSUFFICIENT_SAMPLE,
                "score": None,
                "summary": "One player does not have enough games for a stable similarity read yet.",
                "same_archetype": False,
                "archetype_labels": [],
                "shared_traits": [],
                "contrasting_traits": [],
            }

        player_a_vector = _weighted_similarity_vector(player_a)
        player_b_vector = _weighted_similarity_vector(player_b)
        squared_distance = 0.0
        for feature_name in SIMILARITY_FEATURE_COLUMNS:
            squared_distance += (
                player_b_vector[feature_name] - player_a_vector[feature_name]
            ) ** 2
        score = round(1 / (1 + sqrt(squared_distance)), 4)
        if score is None:
            return {
                "state": STATE_UNAVAILABLE,
                "score": None,
                "summary": "Similarity profile is unavailable for at least one player.",
                "same_archetype": False,
                "archetype_labels": [],
                "shared_traits": [],
                "contrasting_traits": [],
            }
        player_a_label = _guard_served_archetype_label(
            player_a.get("archetype_label"), player_a
        )
        player_b_label = _guard_served_archetype_label(
            player_b.get("archetype_label"), player_b
        )
        same_archetype = player_a_label is not None and player_a_label == player_b_label
        if same_archetype:
            summary = (
                f"Shared archetype: {player_a_label}. "
                f"Statistical Profile Similarity score is {score}."
            )
        else:
            summary = (
                f"Archetypes diverge: {player_a_label} vs "
                f"{player_b_label}. Statistical Profile Similarity score is {score}."
            )
        return {
            "state": STATE_FRESH,
            "score": score,
            "summary": summary,
            "same_archetype": same_archetype,
            "archetype_labels": [player_a_label, player_b_label],
            "shared_traits": _shared_similarity_traits(player_a, player_b),
            "contrasting_traits": _contrasting_similarity_traits(player_a, player_b),
        }

    def _parse_similarity_model_results(
        self, value: Any, source: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        if not value:
            return []
        try:
            parsed = json.loads(value) if isinstance(value, str) else value
        except (TypeError, ValueError):
            return []
        models = parsed.get("models") if isinstance(parsed, dict) else parsed
        if not isinstance(models, list):
            return []
        results: list[dict[str, Any]] = []
        for item in models:
            if not isinstance(item, dict):
                continue
            original_label = item.get("archetype_label")
            guarded_label = _guard_served_archetype_label(
                original_label, source or item
            )
            results.append(
                {
                    "model_key": item.get("model_key"),
                    "model_label": item.get("model_label"),
                    "description": item.get("description"),
                    "archetype_id": item.get("archetype_id"),
                    "archetype_label": guarded_label,
                    "base_archetype_label": self._base_archetype_label(guarded_label),
                    "cluster_confidence": _to_float(item.get("cluster_confidence")),
                    "top_traits": _split_display_list(item.get("top_traits")),
                    "contrasting_traits": _split_display_list(
                        item.get("contrasting_traits")
                    ),
                    "archetype_summary": _guard_served_archetype_summary(
                        item.get("archetype_summary"),
                        original_label=original_label,
                        guarded_label=guarded_label,
                    ),
                    "cluster_size": _to_int(item.get("cluster_size")),
                    "is_recommended": bool(item.get("is_recommended")),
                    "is_baseline": bool(item.get("is_baseline")),
                }
            )
        return results

    @staticmethod
    def _parse_similarity_model_evaluation(value: Any) -> dict[str, Any]:
        if not value:
            return {"recommended_model_key": None, "models": []}
        try:
            parsed = json.loads(value) if isinstance(value, str) else value
        except (TypeError, ValueError):
            return {"recommended_model_key": None, "models": []}
        if not isinstance(parsed, dict):
            return {"recommended_model_key": None, "models": []}
        models = []
        for item in parsed.get("models", []):
            if not isinstance(item, dict):
                continue
            models.append(
                {
                    "model_key": item.get("model_key"),
                    "model_label": item.get("model_label"),
                    "description": item.get("description"),
                    "score": _to_float(item.get("score")),
                    "silhouette": _to_float(item.get("silhouette")),
                    "balance_score": _to_float(item.get("balance_score")),
                    "coverage_score": _to_float(item.get("coverage_score")),
                    "cluster_count": _to_int(item.get("cluster_count")),
                    "unclassified_player_count": _to_int(
                        item.get("unclassified_player_count")
                    ),
                }
            )
        return {
            "recommended_model_key": parsed.get("recommended_model_key"),
            "baseline_model_key": parsed.get("baseline_model_key"),
            "models": models,
        }

    def _decorate_similarity_map_row(self, row: dict[str, Any]) -> dict[str, Any]:
        model_assignments = self._parse_similarity_model_results(
            row.get("model_results_json"), row
        )
        original_archetype_label = row.get("archetype_label")
        guarded_archetype_label = _guard_served_archetype_label(
            original_archetype_label, row
        )
        if not model_assignments:
            model_assignments = [
                {
                    "model_key": row.get("active_model_key") or "active",
                    "model_label": "Active model",
                    "description": "Current published archetype assignment.",
                    "archetype_id": row.get("archetype_id"),
                    "archetype_label": guarded_archetype_label or "Unclassified",
                    "base_archetype_label": self._base_archetype_label(
                        guarded_archetype_label
                    ),
                    "cluster_confidence": _to_float(row.get("cluster_confidence")),
                    "top_traits": _split_display_list(row.get("top_traits")),
                    "contrasting_traits": [],
                    "archetype_summary": _guard_served_archetype_summary(
                        row.get("archetype_summary"),
                        original_label=original_archetype_label,
                        guarded_label=guarded_archetype_label,
                    ),
                    "cluster_size": None,
                    "is_recommended": True,
                    "is_baseline": True,
                }
            ]
        return {
            "player_id": _to_int(row.get("player_id")),
            "player_name": row.get("player_name"),
            "team_abbr": row.get("team_abbr"),
            "archetype_id": row.get("archetype_id"),
            "archetype_label": guarded_archetype_label or "Unclassified",
            "cluster_confidence": _to_float(row.get("cluster_confidence")),
            "top_traits": _split_display_list(row.get("top_traits")),
            "active_model_key": row.get("active_model_key"),
            "recommended_model_key": row.get("recommended_model_key"),
            "model_assignments": model_assignments,
            "games_sampled": _to_int(row.get("games_sampled")),
            "sample_status": row.get("sample_status"),
            "x": _to_float(row.get("proj_x")),
            "y": _to_float(row.get("proj_y")),
            "z": _to_float(row.get("proj_z")),
        }

    @staticmethod
    def _parse_projection_axes(value: Any) -> list[dict[str, Any]]:
        """Decode the projection_axes JSON (axis variance + driving features)."""
        if not value:
            return []
        try:
            parsed = json.loads(value) if isinstance(value, str) else value
        except (TypeError, ValueError):
            return []
        if not isinstance(parsed, list):
            return []
        axes: list[dict[str, Any]] = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            axes.append(
                {
                    "key": item.get("key"),
                    "variance": _to_float(item.get("variance")),
                    "drivers": [str(d) for d in item.get("drivers", []) if d],
                }
            )
        return axes

    @staticmethod
    def _base_archetype_label(label: str | None) -> str:
        # archetype_label is per-player and granular (e.g. "Scoring Guard -
        # Scoring Volume / Recent Scoring"). The family before the first " - "
        # is what the map colors by, so summarize at that level.
        text = (label or "Unclassified").split(" - ", 1)[0].strip()
        return text or "Unclassified"

    @classmethod
    def _summarize_map_archetypes(
        cls, players: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        counts: dict[str, int] = {}
        for player in players:
            label = cls._base_archetype_label(player.get("archetype_label"))
            counts[label] = counts.get(label, 0) + 1
        return [
            {"archetype_label": label, "count": count}
            for label, count in sorted(
                counts.items(), key=lambda item: (-item[1], item[0])
            )
        ]

    @staticmethod
    def _similarity_model_options(
        players: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        options: dict[str, dict[str, Any]] = {}
        for player in players:
            for assignment in player.get("model_assignments", []):
                key = assignment.get("model_key")
                if not key or key in options:
                    continue
                options[key] = {
                    "model_key": key,
                    "model_label": assignment.get("model_label") or key,
                    "description": assignment.get("description"),
                    "is_recommended": bool(assignment.get("is_recommended")),
                    "is_baseline": bool(assignment.get("is_baseline")),
                }
        return list(options.values())

    def get_similarity_map(self) -> dict[str, Any]:
        """Read the precomputed 3D similarity projection for the season.

        Coordinates are a PCA map of the same vectors the cosine similarity
        uses; they are approximate. The per-player similarity score remains the
        source of truth and is surfaced on the player detail page.
        """
        sql = f"""
        SELECT
          *
        FROM {self._similarity_feature_table()}
        WHERE season = @season
          AND sample_status IN ('ready', 'limited_sample')
          AND proj_x IS NOT NULL
          AND proj_y IS NOT NULL
          AND proj_z IS NOT NULL
        ORDER BY archetype_label, player_name
        """
        try:
            rows = self._query(
                sql,
                [
                    bigquery.ScalarQueryParameter(
                        "season", "STRING", self.settings.season
                    )
                ],
            )
        except BQAPIError:
            return {
                "season": self.settings.season,
                "players": [],
                "archetypes": [],
                "models": [],
                "model_evaluation": {"recommended_model_key": None, "models": []},
                "axes": [],
            }

        players = [self._decorate_similarity_map_row(row) for row in rows]
        model_evaluation = self._parse_similarity_model_evaluation(
            rows[0].get("model_evaluation_json") if rows else None
        )
        return {
            "season": self.settings.season,
            "players": players,
            "archetypes": self._summarize_map_archetypes(players),
            "models": self._similarity_model_options(players),
            "model_evaluation": model_evaluation,
            "axes": self._fetch_projection_axes(),
        }

    def _fetch_projection_axes(self) -> list[dict[str, Any]]:
        """Read the projection axis annotations, if the column is present.

        Kept as a separate, guarded query so the map still loads players when
        the projection_axes column has not been published yet (e.g. before the
        first projection-aware pipeline run / backfill).
        """
        sql = f"""
        SELECT projection_axes
        FROM {self._similarity_feature_table()}
        WHERE season = @season
          AND projection_axes IS NOT NULL
        LIMIT 1
        """
        try:
            rows = self._query(
                sql,
                [
                    bigquery.ScalarQueryParameter(
                        "season", "STRING", self.settings.season
                    )
                ],
            )
        except BQAPIError:
            return []
        if not rows:
            return []
        return self._parse_projection_axes(rows[0].get("projection_axes"))

    def get_similarity_neighbors(
        self, player_id: int, *, limit: int = SIMILARITY_RESULT_LIMIT
    ) -> dict[str, Any]:
        """Return a player's true cosine-nearest neighbors for the map.

        These come from the served similarity scoring (distance on the
        normalized vectors), not from 3D proximity — so an edge can point to a
        player who sits visually far on the projection. That divergence is the
        honest signal the map is meant to surface.
        """
        anchor = self._fetch_similarity_anchor(player_id)
        if anchor is None:
            return {
                "state": STATE_UNAVAILABLE,
                "reason": "Similarity profile is unavailable.",
                "player_id": player_id,
                "player_name": None,
                "neighbors": [],
            }

        state, reason, items = self._get_similar_players(
            player_id, anchor=anchor, limit=limit
        )
        neighbors = [
            {
                "player_id": item.get("player_id"),
                "player_name": item.get("player_name"),
                "team_abbr": item.get("team_abbr"),
                "archetype_label": item.get("archetype_label"),
                "similarity_score": item.get("similarity_score"),
                "shared_traits": item.get("shared_traits", []),
            }
            for item in items
        ]
        return {
            "state": state,
            "reason": reason,
            "player_id": player_id,
            "player_name": anchor.get("player_name"),
            "neighbors": neighbors,
        }
