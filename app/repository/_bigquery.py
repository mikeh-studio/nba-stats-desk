from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from math import sqrt
from threading import Lock
from typing import Any

from google.api_core.exceptions import GoogleAPIError as BQAPIError
from google.cloud import bigquery

from app.config import SUPPORTED_SEASON, Settings
from app.repository._constants import (
    COMPARE_FOCUS_CONFIG,
    RECENT_PERFORMANCE_STAT_CONFIG,
    RECENT_PERFORMANCE_TABLE_FIELDS,
    RECENT_PERFORMANCE_TABLE_ROW_CACHE_TTL_SECONDS,
    RECENT_PERFORMANCE_TABLE_ROW_MAX_RESULTS,
    SIMILARITY_FEATURE_COLUMNS,
    SIMILARITY_FEATURE_WEIGHTS,
    SIMILARITY_RESULT_LIMIT,
    STATE_FRESH,
    STATE_INSUFFICIENT_SAMPLE,
    STATE_UNAVAILABLE,
    TREND_STAT_ORDER,
    CompareFocus,
    CompareWindow,
)
from app.repository._helpers import (
    _build_compare_metric_rows,
    _build_sample_payload,
    _build_top_improvement_chips,
    _compare_window_expected_games,
    _compare_window_label,
    _contrasting_similarity_traits,
    _default_recent_form,
    _empty_compare_metrics,
    _format_category_profile,
    _format_chart_baselines,
    _format_game_log_row,
    _format_home_date_label,
    _format_recent_performance_game,
    _format_recent_performance_row,
    _format_recent_performance_trend,
    _format_stat_percentiles,
    _format_trend_row,
    _get_agent_metric_leader_config,
    _has_chart_baselines,
    _merge_recent_performance_detail_summary,
    _opportunity_state_from_row,
    _parse_iso_date,
    _recent_performance_detail_needs_hydration,
    _recent_performance_trend_needs_hydration,
    _sanitize_category_list,
    _shared_similarity_traits,
    _similarity_state_from_sample_status,
    _split_display_list,
    _to_bool,
    _to_float,
    _to_int,
    _to_iso,
    _trend_direction,
    _weighted_similarity_vector,
    _window_reason,
    _window_state,
    build_analysis_payload,
    build_freshness_payload,
    build_headshot_url,
    build_player_initials,
    build_reason_summary,
    build_season_coverage_payload,
)


@dataclass
class BigQueryWarehouseRepository:
    settings: Settings
    client: bigquery.Client | None = None
    _recent_performance_rows_cache: tuple[float, list[dict[str, Any]]] | None = field(
        default=None,
        init=False,
        repr=False,
    )
    _recent_performance_rows_lock: Lock = field(
        default_factory=Lock,
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        if self.client is None:
            self.client = bigquery.Client(project=self.settings.project_id or None)

    def _query(
        self, sql: str, params: list[bigquery.ScalarQueryParameter] | None = None
    ) -> list[dict[str, Any]]:
        job_config = None
        if params:
            job_config = bigquery.QueryJobConfig(query_parameters=params)
        # __post_init__ always sets the client; assert documents the invariant.
        assert self.client is not None
        result = self.client.query(sql, job_config=job_config).result()
        rows: list[dict[str, Any]] = []
        for row in result:
            rows.append({key: _to_iso(value) for key, value in dict(row).items()})
        return rows

    def _dashboard_table(self) -> str:
        return f"`{self.settings.project_id}.{self.settings.gold_dataset}.workbench_dashboard`"

    def _home_dashboard_table(self) -> str:
        return f"`{self.settings.project_id}.{self.settings.gold_dataset}.workbench_home_dashboard`"

    def _detail_table(self) -> str:
        return f"`{self.settings.project_id}.{self.settings.gold_dataset}.workbench_player_detail`"

    def _agent_player_search_table(self) -> str:
        return f"`{self.settings.project_id}.{self.settings.agent_dataset}.agent_player_search`"

    def _player_search_index_table(self) -> str:
        return f"`{self.settings.project_id}.{self.settings.gold_dataset}.player_search_index`"

    def _compare_table(self) -> str:
        return f"`{self.settings.project_id}.{self.settings.gold_dataset}.workbench_compare`"

    def _similarity_feature_table(self) -> str:
        return f"`{self.settings.project_id}.{self.settings.gold_dataset}.player_similarity_features`"

    def _archetype_table(self) -> str:
        return f"`{self.settings.project_id}.{self.settings.gold_dataset}.player_archetypes`"

    def _dim_player_table(self) -> str:
        return f"`{self.settings.project_id}.{self.settings.gold_dataset}.dim_player`"

    def _fct_game_stats_table(self) -> str:
        return f"`{self.settings.project_id}.{self.settings.gold_dataset}.fct_player_game_stats`"

    def _dim_game_table(self) -> str:
        return f"`{self.settings.project_id}.{self.settings.gold_dataset}.dim_game`"

    def _recent_performance_table_id(self) -> str:
        return (
            f"{self.settings.project_id}."
            f"{self.settings.gold_dataset}.recent_performance_workbench"
        )

    def _recent_performance_table(self) -> str:
        return f"`{self._recent_performance_table_id()}`"

    def _fetch_recent_performance_table_rows_api(self) -> list[dict[str, Any]] | None:
        now = time.monotonic()
        with self._recent_performance_rows_lock:
            cached = self._recent_performance_rows_cache
            if cached is not None:
                cached_at, rows = cached
                if now - cached_at < RECENT_PERFORMANCE_TABLE_ROW_CACHE_TTL_SECONDS:
                    return [dict(row) for row in rows]

        assert self.client is not None
        row_iter = self.client.list_rows(
            self._recent_performance_table_id(),
            selected_fields=RECENT_PERFORMANCE_TABLE_FIELDS,
            max_results=RECENT_PERFORMANCE_TABLE_ROW_MAX_RESULTS,
        )
        rows = [
            {key: _to_iso(value) for key, value in dict(row).items()}
            for row in row_iter
        ]
        total_rows = getattr(row_iter, "total_rows", None)
        if isinstance(total_rows, int) and total_rows > len(rows):
            # list_rows has no ordering guarantee, so a truncated page would
            # silently drop arbitrary dates/games/players. Refuse the row
            # cache and let callers fall back to the query path.
            return None
        with self._recent_performance_rows_lock:
            self._recent_performance_rows_cache = (
                time.monotonic(),
                [dict(row) for row in rows],
            )
        return rows

    def _try_fetch_recent_performance_table_rows(self) -> list[dict[str, Any]] | None:
        # Only the BigQuery call is guarded here: bugs in downstream row
        # formatting must surface instead of silently failing over to the
        # expensive query path.
        try:
            return self._fetch_recent_performance_table_rows_api()
        except (AttributeError, BQAPIError):
            return None

    def _player_trends_table(self) -> str:
        return (
            f"`{self.settings.project_id}.{self.settings.gold_dataset}.player_trends`"
        )

    def _category_profile_table(self) -> str:
        return f"`{self.settings.project_id}.{self.settings.gold_dataset}.player_category_profile`"

    def _decorate_dashboard_row(self, row: dict[str, Any]) -> dict[str, Any]:
        item = dict(row)
        item["category_strengths"] = _sanitize_category_list(
            row.get("category_strengths")
        )
        item["category_risks"] = _sanitize_category_list(row.get("category_risks"))
        item["reason_summary"] = build_reason_summary(row)
        item["opportunity_state"] = _opportunity_state_from_row(row)
        item["headshot_url"] = build_headshot_url(row.get("player_id"))
        item["player_initials"] = build_player_initials(row.get("player_name"))
        item["top_improvements"] = _build_top_improvement_chips(row)
        item["trend_direction"] = _trend_direction(row.get("trend_status"))
        return item

    def _decorate_search_player_row(self, row: dict[str, Any]) -> dict[str, Any]:
        item = dict(row)
        item["player_id"] = _to_int(row.get("player_id"))
        item["games_sampled"] = _to_int(row.get("games_sampled"))
        item["qualification_games"] = _to_int(row.get("qualification_games")) or 5
        item["is_qualified"] = bool(_to_bool(row.get("is_qualified")))
        item["overall_rank"] = _to_int(row.get("overall_rank"))
        item["recommendation_score"] = _to_float(row.get("recommendation_score"))
        item["headshot_url"] = build_headshot_url(row.get("player_id"))
        item["player_initials"] = build_player_initials(row.get("player_name"))
        return item

    def _fetch_dashboard_rows(
        self,
        *,
        limit: int,
        order_by: str,
        where_clause: str = "",
        extra_params: list[bigquery.ScalarQueryParameter] | None = None,
    ) -> list[dict[str, Any]]:
        params: list[bigquery.ScalarQueryParameter] = [
            bigquery.ScalarQueryParameter("season", "STRING", SUPPORTED_SEASON),
            bigquery.ScalarQueryParameter("limit", "INT64", limit),
        ]
        if extra_params:
            params.extend(extra_params)
        sql = f"""
        SELECT
          season,
          as_of_date,
          player_id,
          player_name,
          latest_team_abbr,
          latest_game_date,
          overall_rank,
          recommendation_score,
          recommendation_tier,
          category_strengths,
          category_risks,
          last_5_games,
          prior_5_games,
          last_10_games,
          fantasy_proxy_last_5,
          fantasy_proxy_prior_5,
          fantasy_proxy_last_10,
          trend_delta,
          trend_pct_change,
          trend_status,
          next_game_date,
          next_opponent_abbr,
          games_next_7d,
          back_to_backs_next_7d,
          opportunity_score,
          reason_primary_code,
          reason_primary_value,
          reason_secondary_code,
          reason_secondary_value,
          reason_context_code,
          reason_context_value
        FROM {self._dashboard_table()}
        WHERE season = @season
          {where_clause}
        ORDER BY {order_by}
        LIMIT @limit
        """
        return [self._decorate_dashboard_row(row) for row in self._query(sql, params)]

    def _fetch_home_date_options(self) -> list[str]:
        sql = f"""
        SELECT DISTINCT as_of_date
        FROM {self._home_dashboard_table()}
        WHERE season = @season
        ORDER BY as_of_date DESC
        LIMIT 7
        """
        try:
            return [
                str(row["as_of_date"])
                for row in self._query(
                    sql,
                    [
                        bigquery.ScalarQueryParameter(
                            "season", "STRING", SUPPORTED_SEASON
                        )
                    ],
                )
            ]
        except BQAPIError:
            return []

    def _resolve_home_as_of_date(
        self, requested: str | None
    ) -> tuple[str | None, list[str]]:
        options = self._fetch_home_date_options()
        if not options:
            return None, []
        if requested and requested in options:
            return requested, options
        parsed_requested = _parse_iso_date(requested)
        if parsed_requested is not None:
            normalized = parsed_requested.isoformat()
            if normalized in options:
                return normalized, options
        return options[0], options

    def _fetch_home_dashboard_rows(
        self,
        *,
        as_of_date: str,
        limit: int,
        order_by: str,
        where_clause: str = "",
    ) -> list[dict[str, Any]]:
        parsed_as_of_date = _parse_iso_date(as_of_date)
        params: list[bigquery.ScalarQueryParameter] = [
            bigquery.ScalarQueryParameter("season", "STRING", SUPPORTED_SEASON),
            bigquery.ScalarQueryParameter("as_of_date", "DATE", parsed_as_of_date),
            bigquery.ScalarQueryParameter("limit", "INT64", limit),
        ]
        sql = f"""
        SELECT
          season,
          as_of_date,
          player_id,
          player_name,
          latest_team_abbr,
          latest_game_date,
          overall_rank,
          recommendation_score,
          recommendation_tier,
          category_strengths,
          category_risks,
          last_5_games,
          prior_5_games,
          last_10_games,
          fantasy_proxy_last_5,
          fantasy_proxy_prior_5,
          fantasy_proxy_last_10,
          trend_delta,
          trend_pct_change,
          trend_status,
          next_game_date,
          next_opponent_abbr,
          games_next_7d,
          back_to_backs_next_7d,
          opportunity_score,
          pts_delta,
          reb_delta,
          ast_delta,
          stl_delta,
          blk_delta,
          fg3m_delta,
          min_delta,
          reason_primary_code,
          reason_primary_value,
          reason_secondary_code,
          reason_secondary_value,
          reason_context_code,
          reason_context_value
        FROM {self._home_dashboard_table()}
        WHERE season = @season
          AND as_of_date = @as_of_date
          {where_clause}
        ORDER BY {order_by}
        LIMIT @limit
        """
        return [self._decorate_dashboard_row(row) for row in self._query(sql, params)]

    def _fetch_player_identity_from_search_table(
        self, table: str, player_id: int
    ) -> list[dict[str, Any]]:
        sql = f"""
        SELECT
          player_id,
          player_name,
          latest_season,
          latest_team_abbr,
          latest_game_date,
          games_sampled,
          qualification_games,
          is_qualified,
          sample_status,
          sample_warning,
          overall_rank,
          recommendation_score,
          last_seen_at_utc
        FROM {table}
        WHERE latest_season = @season
          AND player_id = @player_id
        LIMIT 1
        """
        params = [
            bigquery.ScalarQueryParameter("season", "STRING", SUPPORTED_SEASON),
            bigquery.ScalarQueryParameter("player_id", "INT64", player_id),
        ]
        return self._query(sql, params)

    def _fetch_player_identity(self, player_id: int) -> dict[str, Any] | None:
        try:
            rows = self._fetch_player_identity_from_search_table(
                self._agent_player_search_table(), player_id
            )
        except BQAPIError:
            try:
                rows = self._fetch_player_identity_from_search_table(
                    self._player_search_index_table(), player_id
                )
            except BQAPIError:
                rows = self._fetch_player_identity_from_game_stats(player_id)
        return rows[0] if rows else None

    def _fetch_player_identity_from_game_stats(
        self, player_id: int
    ) -> list[dict[str, Any]]:
        sql = f"""
        WITH qualified AS (
          SELECT
            season,
            player_id,
            ANY_VALUE(player_name) AS player_name,
            ARRAY_AGG(
              team_abbr IGNORE NULLS
              ORDER BY game_date DESC, ingested_at_utc DESC
              LIMIT 1
            )[SAFE_OFFSET(0)] AS latest_team_abbr,
            MAX(game_date) AS latest_game_date,
            COUNT(*) AS games_sampled,
            MAX(ingested_at_utc) AS last_seen_at_utc
          FROM {self._fct_game_stats_table()}
          WHERE season = @season
            AND player_id = @player_id
          GROUP BY season, player_id
          HAVING COUNT(*) >= 5
        )
        SELECT
          player_id,
          player_name,
          season AS latest_season,
          latest_team_abbr,
          latest_game_date,
          games_sampled,
          5 AS qualification_games,
          TRUE AS is_qualified,
          CASE
            WHEN games_sampled >= 10 THEN 'ready'
            ELSE 'limited_sample'
          END AS sample_status,
          CASE
            WHEN games_sampled >= 10 THEN NULL
            ELSE 'Limited sample: percentiles are available after the dbt model is rebuilt.'
          END AS sample_warning,
          NULL AS overall_rank,
          NULL AS recommendation_score,
          last_seen_at_utc
        FROM qualified
        LIMIT 1
        """
        try:
            return self._query(
                sql,
                [
                    bigquery.ScalarQueryParameter("season", "STRING", SUPPORTED_SEASON),
                    bigquery.ScalarQueryParameter("player_id", "INT64", player_id),
                ],
            )
        except BQAPIError:
            return []

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
                    bigquery.ScalarQueryParameter("season", "STRING", SUPPORTED_SEASON),
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
                    bigquery.ScalarQueryParameter("season", "STRING", SUPPORTED_SEASON),
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
                    "archetype_label": row.get("archetype_label"),
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
        same_archetype = player_a.get("archetype_label") is not None and player_a.get(
            "archetype_label"
        ) == player_b.get("archetype_label")
        if same_archetype:
            summary = (
                f"Shared archetype: {player_a.get('archetype_label')}. "
                f"Current stat-profile similarity is {score}."
            )
        else:
            summary = (
                f"Archetypes diverge: {player_a.get('archetype_label')} vs "
                f"{player_b.get('archetype_label')}. Current stat-profile similarity is {score}."
            )
        return {
            "state": STATE_FRESH,
            "score": score,
            "summary": summary,
            "same_archetype": same_archetype,
            "archetype_labels": [
                player_a.get("archetype_label"),
                player_b.get("archetype_label"),
            ],
            "shared_traits": _shared_similarity_traits(player_a, player_b),
            "contrasting_traits": _contrasting_similarity_traits(player_a, player_b),
        }

    def _fetch_player_game_log_payload(
        self,
        identity: dict[str, Any],
        limit: int = 30,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict[str, Any]:
        player_id = _to_int(identity.get("player_id"))
        filters = ["season = @season", "player_id = @player_id"]
        params: list[bigquery.ScalarQueryParameter] = [
            bigquery.ScalarQueryParameter("season", "STRING", SUPPORTED_SEASON),
            bigquery.ScalarQueryParameter("player_id", "INT64", player_id),
            bigquery.ScalarQueryParameter("limit", "INT64", limit),
        ]
        parsed_start_date = _parse_iso_date(start_date)
        parsed_end_date = _parse_iso_date(end_date)
        if parsed_start_date is not None:
            filters.append("game_date >= @start_date")
            params.append(
                bigquery.ScalarQueryParameter("start_date", "DATE", parsed_start_date)
            )
        if parsed_end_date is not None:
            filters.append("game_date <= @end_date")
            params.append(
                bigquery.ScalarQueryParameter("end_date", "DATE", parsed_end_date)
            )
        sql = f"""
        SELECT
          game_id,
          season,
          game_date,
          player_id,
          player_name,
          team_abbr,
          opponent_abbr,
          home_away,
          matchup,
          wl,
          min,
          pts,
          reb,
          ast,
          stl,
          blk,
          tov,
          fg3m,
          fg3a,
          fgm,
          fga,
          fg_pct,
          ftm,
          fta,
          ft_pct,
          plus_minus,
          fantasy_points_simple
        FROM {self._fct_game_stats_table()}
        WHERE {" AND ".join(filters)}
        ORDER BY game_date DESC, game_id DESC
        LIMIT @limit
        """
        try:
            rows = self._query(sql, params)
        except BQAPIError:
            rows = []
        rows.reverse()
        games = [
            _format_game_log_row(row, game_number=index + 1)
            for index, row in enumerate(rows)
        ]
        return {
            "player_id": player_id,
            "player_name": identity.get("player_name"),
            "season": SUPPORTED_SEASON,
            "games": games,
            "games_returned": len(games),
            "limit": limit,
            "order": "chronological",
            "date_range": {
                "start_date": parsed_start_date.isoformat()
                if parsed_start_date is not None
                else None,
                "end_date": parsed_end_date.isoformat()
                if parsed_end_date is not None
                else None,
            },
        }

    def _fetch_player_trends(self, player_id: int) -> list[dict[str, Any]]:
        sql = f"""
        SELECT
          stat,
          recent_games,
          prior_games,
          recent_avg,
          prior_avg,
          delta,
          pct_change
        FROM {self._player_trends_table()}
        WHERE season = @season
          AND player_id = @player_id
          AND stat IN ('PTS', 'REB', 'AST', 'STL', 'BLK', 'TOV', 'MIN', 'FANTASY_POINTS_SIMPLE')
        """
        try:
            rows = self._query(
                sql,
                [
                    bigquery.ScalarQueryParameter("season", "STRING", SUPPORTED_SEASON),
                    bigquery.ScalarQueryParameter("player_id", "INT64", player_id),
                ],
            )
        except BQAPIError:
            return []
        rows.sort(key=lambda item: TREND_STAT_ORDER.get(str(item.get("stat")), 99))
        return [_format_trend_row(row) for row in rows]

    def _fetch_chart_baseline_row(self) -> dict[str, Any]:
        sql = f"""
        WITH player_means AS (
          SELECT
            season,
            player_id,
            AVG(pts) AS avg_pts,
            AVG(reb) AS avg_reb,
            AVG(ast) AS avg_ast,
            AVG(stl) AS avg_stl,
            AVG(blk) AS avg_blk,
            AVG(tov) AS avg_tov,
            COUNT(*) AS games_sampled
          FROM {self._fct_game_stats_table()}
          WHERE season = @season
          GROUP BY season, player_id
          HAVING COUNT(*) >= 5
        )
        SELECT
          ROUND(AVG(avg_pts), 2) AS league_avg_pts,
          ROUND(AVG(avg_reb), 2) AS league_avg_reb,
          ROUND(AVG(avg_ast), 2) AS league_avg_ast,
          ROUND(AVG(avg_stl), 2) AS league_avg_stl,
          ROUND(AVG(avg_blk), 2) AS league_avg_blk,
          ROUND(AVG(avg_tov), 2) AS league_avg_tov
        FROM player_means
        WHERE season = @season
        """
        try:
            rows = self._query(
                sql,
                [bigquery.ScalarQueryParameter("season", "STRING", SUPPORTED_SEASON)],
            )
        except BQAPIError:
            return {}
        return rows[0] if rows else {}

    def _fetch_player_detail_row(self, player_id: int) -> dict[str, Any] | None:
        params = [
            bigquery.ScalarQueryParameter("season", "STRING", SUPPORTED_SEASON),
            bigquery.ScalarQueryParameter("player_id", "INT64", player_id),
        ]
        sql = f"""
        SELECT
          season,
          as_of_date,
          player_id,
          player_name,
          latest_team_abbr,
          latest_game_date,
          overall_rank,
          recommendation_score,
          recommendation_tier,
          category_strengths,
          category_risks,
          trend_delta,
          trend_pct_change,
          trend_status,
          next_game_date,
          next_opponent_abbr,
          games_next_7d,
          back_to_backs_next_7d,
          opportunity_score,
          reason_primary_code,
          reason_primary_value,
          reason_secondary_code,
          reason_secondary_value,
          reason_context_code,
          reason_context_value,
          games_sampled,
          qualification_games,
          is_qualified,
          sample_status,
          sample_warning,
          z_pts,
          z_reb,
          z_ast,
          z_stl,
          z_blk,
          z_fg3m,
          z_tov,
          pts_percentile,
          reb_percentile,
          ast_percentile,
          stl_percentile,
          blk_percentile,
          tov_percentile,
          season_avg_pts,
          season_avg_reb,
          season_avg_ast,
          season_avg_stl,
          season_avg_blk,
          season_avg_tov,
          league_avg_pts,
          league_avg_reb,
          league_avg_ast,
          league_avg_stl,
          league_avg_blk,
          league_avg_tov,
          category_score_7cat,
          category_coverage_status,
          last_5_games,
          last_5_avg_min,
          last_5_avg_pts,
          last_5_avg_reb,
          last_5_avg_ast,
          last_5_avg_stl,
          last_5_avg_blk,
          last_5_avg_fg3m,
          last_5_avg_tov,
          last_5_fantasy_proxy,
          prior_5_games,
          prior_5_avg_min,
          prior_5_avg_pts,
          prior_5_avg_reb,
          prior_5_avg_ast,
          prior_5_avg_stl,
          prior_5_avg_blk,
          prior_5_avg_fg3m,
          prior_5_avg_tov,
          prior_5_fantasy_proxy,
          last_10_games,
          last_10_avg_min,
          last_10_avg_pts,
          last_10_avg_reb,
          last_10_avg_ast,
          last_10_avg_stl,
          last_10_avg_blk,
          last_10_avg_fg3m,
          last_10_avg_tov,
          last_10_fantasy_proxy
        FROM {self._detail_table()}
        WHERE season = @season
          AND player_id = @player_id
        LIMIT 1
        """
        try:
            rows = self._query(sql, params)
        except BQAPIError:
            rows = self._fetch_legacy_player_detail_row(player_id)
        return rows[0] if rows else None

    def _fetch_legacy_player_detail_row(self, player_id: int) -> list[dict[str, Any]]:
        sql = f"""
        SELECT
          season,
          as_of_date,
          player_id,
          player_name,
          latest_team_abbr,
          latest_game_date,
          overall_rank,
          recommendation_score,
          recommendation_tier,
          category_strengths,
          category_risks,
          trend_delta,
          trend_pct_change,
          trend_status,
          next_game_date,
          next_opponent_abbr,
          games_next_7d,
          back_to_backs_next_7d,
          opportunity_score,
          reason_primary_code,
          reason_primary_value,
          reason_secondary_code,
          reason_secondary_value,
          reason_context_code,
          reason_context_value,
          z_pts,
          z_reb,
          z_ast,
          z_stl,
          z_blk,
          z_fg3m,
          z_tov,
          category_score_7cat,
          category_coverage_status,
          last_5_games,
          last_5_avg_min,
          last_5_avg_pts,
          last_5_avg_reb,
          last_5_avg_ast,
          last_5_avg_stl,
          last_5_avg_blk,
          last_5_avg_fg3m,
          last_5_avg_tov,
          last_5_fantasy_proxy,
          prior_5_games,
          prior_5_avg_min,
          prior_5_avg_pts,
          prior_5_avg_reb,
          prior_5_avg_ast,
          prior_5_avg_stl,
          prior_5_avg_blk,
          prior_5_avg_fg3m,
          prior_5_avg_tov,
          prior_5_fantasy_proxy,
          last_10_games,
          last_10_avg_min,
          last_10_avg_pts,
          last_10_avg_reb,
          last_10_avg_ast,
          last_10_avg_stl,
          last_10_avg_blk,
          last_10_avg_fg3m,
          last_10_avg_tov,
          last_10_fantasy_proxy
        FROM {self._detail_table()}
        WHERE season = @season
          AND player_id = @player_id
        LIMIT 1
        """
        try:
            return self._query(
                sql,
                [
                    bigquery.ScalarQueryParameter("season", "STRING", SUPPORTED_SEASON),
                    bigquery.ScalarQueryParameter("player_id", "INT64", player_id),
                ],
            )
        except BQAPIError:
            return []

    def _build_player_detail_payload(
        self,
        *,
        identity: dict[str, Any],
        row: dict[str, Any] | None,
        archetype_row: dict[str, Any] | None,
        similarity_state: str,
        similarity_reason: str | None,
        similar_players: list[dict[str, Any]],
        game_log: dict[str, Any],
        trends: list[dict[str, Any]],
        chart_baselines: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        archetype_state = STATE_UNAVAILABLE
        archetype_payload: dict[str, Any] = {
            "state": STATE_UNAVAILABLE,
            "archetype_id": None,
            "archetype_label": None,
            "cluster_confidence": None,
            "top_traits": [],
            "summary": None,
            "active_model_key": None,
            "recommended_model_key": None,
        }
        if archetype_row is not None:
            archetype_state = _similarity_state_from_sample_status(
                archetype_row.get("sample_status")
            )
            archetype_payload = {
                "state": archetype_state,
                "archetype_id": archetype_row.get("archetype_id"),
                "archetype_label": archetype_row.get("archetype_label"),
                "cluster_confidence": _to_float(
                    archetype_row.get("cluster_confidence")
                ),
                "top_traits": _split_display_list(archetype_row.get("top_traits")),
                "summary": archetype_row.get("archetype_summary"),
                "active_model_key": archetype_row.get("active_model_key"),
                "recommended_model_key": archetype_row.get("recommended_model_key"),
            }

        similarity_models = self._parse_similarity_model_results(
            archetype_row.get("model_results_json") if archetype_row else None
        )
        similarity_model_evaluation = self._parse_similarity_model_evaluation(
            archetype_row.get("model_evaluation_json") if archetype_row else None
        )

        game_log_state = STATE_FRESH if game_log.get("games") else STATE_UNAVAILABLE
        trends_state = STATE_FRESH if trends else STATE_UNAVAILABLE

        if row is None:
            sample = _build_sample_payload(identity)
            return {
                "player": {
                    "season": identity.get("latest_season", SUPPORTED_SEASON),
                    "player_id": identity.get("player_id"),
                    "player_name": identity.get("player_name"),
                    "headshot_url": build_headshot_url(identity.get("player_id")),
                    "player_initials": build_player_initials(
                        identity.get("player_name")
                    ),
                    "team_abbr": identity.get("latest_team_abbr"),
                    "latest_game_date": identity.get("latest_game_date"),
                    "overall_rank": _to_int(identity.get("overall_rank")),
                    "recommendation_score": _to_float(
                        identity.get("recommendation_score")
                    ),
                    "recommendation_tier": None,
                    "category_strengths": None,
                    "category_risks": None,
                    "is_ranked": False,
                    "games_sampled": sample["games_sampled"],
                    "sample_status": sample["sample_status"],
                    "is_qualified": sample["is_qualified"],
                },
                "sample": sample,
                "availability_state": STATE_UNAVAILABLE,
                "availability_reason": "Not currently ranked",
                "reason_summary": None,
                "trend": {
                    "status": STATE_UNAVAILABLE,
                    "delta": None,
                    "pct_change": None,
                },
                "panel_states": {
                    "recent_form": STATE_UNAVAILABLE,
                    "category_profile": STATE_UNAVAILABLE,
                    "stat_percentiles": STATE_UNAVAILABLE,
                    "game_log": game_log_state,
                    "trends": trends_state,
                    "opportunity": STATE_UNAVAILABLE,
                    "archetype": archetype_state,
                    "similarity": similarity_state,
                },
                "recent_form": _default_recent_form(),
                "category_profile": [],
                "stat_percentiles": [],
                "chart_baselines": chart_baselines,
                "game_log": game_log,
                "trends": trends,
                "opportunity": None,
                "archetype": archetype_payload,
                "similarity_models": similarity_models,
                "similarity_model_evaluation": similarity_model_evaluation,
                "similarity_reason": similarity_reason,
                "similar_players": similar_players,
            }

        recent_form = []
        for key, label, expected_games in (
            ("last_5", "Last 5", 5),
            ("prior_5", "Prior 5", 5),
            ("last_10", "Last 10", 10),
        ):
            games_in_window = _to_int(row.get(f"{key}_games"))
            state = _window_state(games_in_window, expected_games)
            recent_form.append(
                {
                    "window_key": key,
                    "window_label": label,
                    "games_in_window": games_in_window,
                    "window_games_expected": expected_games,
                    "state": state,
                    "state_reason": _window_reason(state, label),
                    "avg_pts": row.get(f"{key}_avg_pts"),
                    "avg_reb": row.get(f"{key}_avg_reb"),
                    "avg_ast": row.get(f"{key}_avg_ast"),
                    "avg_stl": row.get(f"{key}_avg_stl"),
                    "avg_blk": row.get(f"{key}_avg_blk"),
                    "avg_fg3m": row.get(f"{key}_avg_fg3m"),
                    "avg_tov": row.get(f"{key}_avg_tov"),
                    "avg_minutes": row.get(f"{key}_avg_min"),
                    "fantasy_proxy": row.get(f"{key}_fantasy_proxy"),
                }
            )

        category_profile = _format_category_profile(row)
        stat_percentiles = _format_stat_percentiles(row)
        sample = _build_sample_payload(row, identity)
        opportunity_state = _opportunity_state_from_row(row)
        recent_form_state = (
            STATE_FRESH
            if any(item["state"] == STATE_FRESH for item in recent_form)
            else STATE_INSUFFICIENT_SAMPLE
            if any(item["state"] == STATE_INSUFFICIENT_SAMPLE for item in recent_form)
            else STATE_UNAVAILABLE
        )
        category_profile_state = STATE_FRESH if category_profile else STATE_UNAVAILABLE
        stat_percentiles_state = STATE_FRESH if stat_percentiles else STATE_UNAVAILABLE
        opportunity = None
        if opportunity_state != STATE_UNAVAILABLE:
            opportunity = {
                "games_next_7d": row.get("games_next_7d"),
                "back_to_backs_next_7d": row.get("back_to_backs_next_7d"),
                "next_opponent": row.get("next_opponent_abbr"),
                "next_game_date": row.get("next_game_date"),
                "opportunity_score": row.get("opportunity_score"),
            }

        return {
            "player": {
                "season": row.get("season"),
                "player_id": row.get("player_id"),
                "player_name": row.get("player_name"),
                "headshot_url": build_headshot_url(row.get("player_id")),
                "player_initials": build_player_initials(row.get("player_name")),
                "team_abbr": row.get("latest_team_abbr"),
                "latest_game_date": row.get("latest_game_date"),
                "overall_rank": row.get("overall_rank"),
                "recommendation_score": row.get("recommendation_score"),
                "recommendation_tier": row.get("recommendation_tier"),
                "category_strengths": _sanitize_category_list(
                    row.get("category_strengths")
                ),
                "category_risks": _sanitize_category_list(row.get("category_risks")),
                "is_ranked": row.get("overall_rank") is not None,
                "games_sampled": sample["games_sampled"],
                "sample_status": sample["sample_status"],
                "is_qualified": sample["is_qualified"],
            },
            "sample": sample,
            "availability_state": (
                STATE_FRESH
                if row.get("overall_rank") is not None
                else STATE_UNAVAILABLE
            ),
            "availability_reason": (
                None if row.get("overall_rank") is not None else "Not currently ranked"
            ),
            "reason_summary": build_reason_summary(row),
            "trend": {
                "status": row.get("trend_status"),
                "delta": row.get("trend_delta"),
                "pct_change": row.get("trend_pct_change"),
            },
            "panel_states": {
                "recent_form": recent_form_state,
                "category_profile": category_profile_state,
                "stat_percentiles": stat_percentiles_state,
                "game_log": game_log_state,
                "trends": trends_state,
                "opportunity": opportunity_state,
                "archetype": archetype_state,
                "similarity": similarity_state,
            },
            "recent_form": recent_form,
            "category_profile": category_profile,
            "stat_percentiles": stat_percentiles,
            "chart_baselines": chart_baselines,
            "game_log": game_log,
            "trends": trends,
            "opportunity": opportunity,
            "archetype": archetype_payload,
            "similarity_models": similarity_models,
            "similarity_model_evaluation": similarity_model_evaluation,
            "similarity_reason": similarity_reason,
            "similar_players": similar_players,
        }

    def get_dashboard(self, as_of_date: str | None = None) -> dict[str, Any]:
        selected_as_of_date, date_options = self._resolve_home_as_of_date(as_of_date)
        if selected_as_of_date is None:
            return {
                "selected_as_of_date": None,
                "date_options": [],
                "signals": [],
                "rankings": [],
                "trends": [],
                "opportunity": [],
            }

        rows = self._fetch_home_dashboard_rows(
            as_of_date=selected_as_of_date,
            limit=18,
            order_by="recommendation_score DESC, overall_rank ASC, player_name",
        )
        signals = rows[:6]
        rankings = sorted(
            [item for item in rows if item.get("overall_rank") is not None],
            key=lambda item: int(item["overall_rank"]),
        )[:8]
        trends = sorted(
            rows,
            key=lambda item: abs(_to_float(item.get("trend_delta")) or 0.0),
            reverse=True,
        )[:6]
        opportunity = [
            item for item in rows if item["opportunity_state"] != STATE_UNAVAILABLE
        ][:6]
        return {
            "selected_as_of_date": selected_as_of_date,
            "date_options": [
                {
                    "value": value,
                    "label": _format_home_date_label(value),
                    "is_selected": value == selected_as_of_date,
                }
                for value in date_options
            ],
            "signals": signals,
            "rankings": rankings,
            "trends": trends,
            "opportunity": opportunity,
        }

    @staticmethod
    def _parse_similarity_model_results(value: Any) -> list[dict[str, Any]]:
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
            results.append(
                {
                    "model_key": item.get("model_key"),
                    "model_label": item.get("model_label"),
                    "description": item.get("description"),
                    "archetype_id": item.get("archetype_id"),
                    "archetype_label": item.get("archetype_label"),
                    "base_archetype_label": item.get("base_archetype_label"),
                    "cluster_confidence": _to_float(item.get("cluster_confidence")),
                    "top_traits": _split_display_list(item.get("top_traits")),
                    "contrasting_traits": _split_display_list(
                        item.get("contrasting_traits")
                    ),
                    "archetype_summary": item.get("archetype_summary"),
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
            row.get("model_results_json")
        )
        if not model_assignments:
            model_assignments = [
                {
                    "model_key": row.get("active_model_key") or "active",
                    "model_label": "Active model",
                    "description": "Current published archetype assignment.",
                    "archetype_id": row.get("archetype_id"),
                    "archetype_label": row.get("archetype_label") or "Unclassified",
                    "base_archetype_label": self._base_archetype_label(
                        row.get("archetype_label")
                    ),
                    "cluster_confidence": _to_float(row.get("cluster_confidence")),
                    "top_traits": _split_display_list(row.get("top_traits")),
                    "contrasting_traits": [],
                    "archetype_summary": row.get("archetype_summary"),
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
            "archetype_label": row.get("archetype_label") or "Unclassified",
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
                [bigquery.ScalarQueryParameter("season", "STRING", SUPPORTED_SEASON)],
            )
        except BQAPIError:
            return {
                "season": SUPPORTED_SEASON,
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
            "season": SUPPORTED_SEASON,
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
                [bigquery.ScalarQueryParameter("season", "STRING", SUPPORTED_SEASON)],
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

    def get_leaderboard(self, limit: int = 10) -> list[dict[str, Any]]:
        table = f"`{self.settings.project_id}.{self.settings.gold_dataset}.daily_leaderboard`"
        sql = f"""
        SELECT
          season,
          game_date,
          pts_leader,
          pts_matchup,
          pts,
          reb_leader,
          reb,
          ast_leader,
          ast
        FROM {table}
        WHERE season = @season
        ORDER BY game_date DESC, pts DESC, pts_leader
        LIMIT @limit
        """
        return self._query(
            sql,
            [
                bigquery.ScalarQueryParameter("season", "STRING", SUPPORTED_SEASON),
                bigquery.ScalarQueryParameter("limit", "INT64", limit),
            ],
        )

    def get_trends(self, limit: int = 10) -> list[dict[str, Any]]:
        return self._fetch_dashboard_rows(
            limit=limit,
            order_by="ABS(trend_delta) DESC, player_name",
        )

    def get_recommendations(
        self, limit: int = 10, insight_type: str | None = None
    ) -> list[dict[str, Any]]:
        table = f"`{self.settings.project_id}.{self.settings.gold_dataset}.fantasy_insights`"
        filters = ["season = @season"]
        params: list[bigquery.ScalarQueryParameter] = [
            bigquery.ScalarQueryParameter("season", "STRING", SUPPORTED_SEASON),
            bigquery.ScalarQueryParameter("limit", "INT64", limit),
        ]
        if insight_type:
            filters.append("insight_type = @insight_type")
            params.append(
                bigquery.ScalarQueryParameter("insight_type", "STRING", insight_type)
            )
        sql = f"""
        SELECT
          insight_id,
          as_of_date,
          player_id,
          player_name,
          insight_type,
          priority_score,
          confidence_score,
          category_focus,
          recommendation,
          title,
          summary,
          evidence_json,
          source_label
        FROM {table}
        WHERE {" AND ".join(filters)}
        ORDER BY as_of_date DESC, priority_score DESC, confidence_score DESC, player_name
        LIMIT @limit
        """
        return self._query(sql, params)

    def get_rankings(self, limit: int = 25) -> list[dict[str, Any]]:
        return self._fetch_dashboard_rows(
            limit=limit,
            order_by="overall_rank ASC, recommendation_score DESC, player_name",
        )

    def _search_players_from_search_table(
        self, table: str, query: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        sql = f"""
        SELECT
          player_id,
          player_name,
          latest_season,
          latest_team_abbr,
          latest_game_date,
          games_sampled,
          qualification_games,
          is_qualified,
          sample_status,
          sample_warning,
          overall_rank,
          recommendation_score,
          last_seen_at_utc
        FROM {table}
        WHERE latest_season = @season
          AND search_text LIKE CONCAT('%', LOWER(@query), '%')
        ORDER BY
          CASE
            WHEN LOWER(player_name) = LOWER(@query) THEN 0
            WHEN STARTS_WITH(LOWER(player_name), LOWER(@query)) THEN 1
            ELSE 2
          END,
          overall_rank IS NULL,
          overall_rank,
          player_name
        LIMIT @limit
        """
        params = [
            bigquery.ScalarQueryParameter("season", "STRING", SUPPORTED_SEASON),
            bigquery.ScalarQueryParameter("query", "STRING", query.strip()),
            bigquery.ScalarQueryParameter("limit", "INT64", limit),
        ]
        return self._query(sql, params)

    def search_players(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        try:
            rows = self._search_players_from_search_table(
                self._agent_player_search_table(), query, limit
            )
        except BQAPIError:
            try:
                rows = self._search_players_from_search_table(
                    self._player_search_index_table(), query, limit
                )
            except BQAPIError:
                rows = self._search_players_from_game_stats(query, limit=limit)
        return [self._decorate_search_player_row(row) for row in rows]

    def list_agent_player_candidates(self, limit: int = 1000) -> list[dict[str, Any]]:
        try:
            rows = self._list_player_candidates_from_search_table(
                self._agent_player_search_table(), limit
            )
        except BQAPIError:
            try:
                rows = self._list_player_candidates_from_search_table(
                    self._player_search_index_table(), limit
                )
            except BQAPIError:
                rows = self._search_players_from_game_stats("", limit=limit)
        return [self._decorate_search_player_row(row) for row in rows]

    def _list_player_candidates_from_search_table(
        self, table: str, limit: int = 1000
    ) -> list[dict[str, Any]]:
        sql = f"""
        SELECT
          player_id,
          player_name,
          latest_season,
          latest_team_abbr,
          latest_game_date,
          games_sampled,
          qualification_games,
          is_qualified,
          sample_status,
          sample_warning,
          overall_rank,
          recommendation_score,
          last_seen_at_utc
        FROM {table}
        WHERE latest_season = @season
        ORDER BY
          overall_rank IS NULL,
          overall_rank,
          games_sampled DESC,
          player_name
        LIMIT @limit
        """
        params = [
            bigquery.ScalarQueryParameter("season", "STRING", SUPPORTED_SEASON),
            bigquery.ScalarQueryParameter("limit", "INT64", limit),
        ]
        return self._query(sql, params)

    def _search_players_from_game_stats(
        self, query: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        sql = f"""
        WITH qualified AS (
          SELECT
            season,
            player_id,
            ANY_VALUE(player_name) AS player_name,
            ARRAY_AGG(
              team_abbr IGNORE NULLS
              ORDER BY game_date DESC, ingested_at_utc DESC
              LIMIT 1
            )[SAFE_OFFSET(0)] AS latest_team_abbr,
            MAX(game_date) AS latest_game_date,
            COUNT(*) AS games_sampled,
            MAX(ingested_at_utc) AS last_seen_at_utc
          FROM {self._fct_game_stats_table()}
          WHERE season = @season
          GROUP BY season, player_id
          HAVING COUNT(*) >= 5
        )
        SELECT
          player_id,
          player_name,
          season AS latest_season,
          latest_team_abbr,
          latest_game_date,
          games_sampled,
          5 AS qualification_games,
          TRUE AS is_qualified,
          CASE
            WHEN games_sampled >= 10 THEN 'ready'
            ELSE 'limited_sample'
          END AS sample_status,
          CASE
            WHEN games_sampled >= 10 THEN NULL
            ELSE 'Limited sample: percentiles are available after the dbt model is rebuilt.'
          END AS sample_warning,
          NULL AS overall_rank,
          NULL AS recommendation_score,
          last_seen_at_utc
        FROM qualified
        WHERE @query = '' OR LOWER(player_name) LIKE CONCAT('%', LOWER(@query), '%')
        ORDER BY
          CASE
            WHEN LOWER(player_name) = LOWER(@query) THEN 0
            WHEN STARTS_WITH(LOWER(player_name), LOWER(@query)) THEN 1
            ELSE 2
          END,
          games_sampled DESC,
          player_name
        LIMIT @limit
        """
        try:
            return self._query(
                sql,
                [
                    bigquery.ScalarQueryParameter("season", "STRING", SUPPORTED_SEASON),
                    bigquery.ScalarQueryParameter("query", "STRING", query.strip()),
                    bigquery.ScalarQueryParameter("limit", "INT64", limit),
                ],
            )
        except BQAPIError:
            return []

    def get_player_detail(self, player_id: int) -> dict[str, Any] | None:
        identity = self._fetch_player_identity(player_id)
        if identity is None:
            return None

        row = self._fetch_player_detail_row(player_id)
        game_log = self._fetch_player_game_log_payload(identity, limit=30)
        trends = self._fetch_player_trends(player_id)
        baseline_fallback = (
            {} if _has_chart_baselines(row) else self._fetch_chart_baseline_row()
        )
        chart_baselines = _format_chart_baselines(row or {}, baseline_fallback)
        anchor = self._fetch_similarity_anchor(player_id)
        (
            similarity_state,
            similarity_reason,
            similar_players,
        ) = self._get_similar_players(
            player_id,
            anchor=anchor,
        )
        return self._build_player_detail_payload(
            identity=identity,
            row=row,
            archetype_row=anchor,
            similarity_state=similarity_state,
            similarity_reason=similarity_reason,
            similar_players=similar_players,
            game_log=game_log,
            trends=trends,
            chart_baselines=chart_baselines,
        )

    def get_compare(
        self,
        player_a_id: int,
        player_b_id: int,
        *,
        window: CompareWindow = "last_5",
        focus: CompareFocus = "balanced",
    ) -> dict[str, Any]:
        sql = f"""
        SELECT
          season,
          as_of_date,
          player_id,
          player_name,
          latest_team_abbr,
          latest_game_date,
          window_key,
          window_games_expected,
          games_in_window,
          has_full_window,
          avg_min,
          avg_pts,
          avg_reb,
          avg_ast,
          avg_stl,
          avg_blk,
          avg_fg3m,
          avg_tov,
          fantasy_proxy_score
        FROM {self._compare_table()}
        WHERE season = @season
          AND window_key = @window
          AND player_id IN (@player_a_id, @player_b_id)
        """
        rows = self._query(
            sql,
            [
                bigquery.ScalarQueryParameter("season", "STRING", SUPPORTED_SEASON),
                bigquery.ScalarQueryParameter("window", "STRING", window),
                bigquery.ScalarQueryParameter("player_a_id", "INT64", player_a_id),
                bigquery.ScalarQueryParameter("player_b_id", "INT64", player_b_id),
            ],
        )
        rows_by_player = {int(row["player_id"]): row for row in rows}
        pair_similarity = self._get_pair_similarity(player_a_id, player_b_id)

        def build_side(player_id: int) -> dict[str, Any]:
            row = rows_by_player.get(player_id)
            detail = self.get_player_detail(player_id)
            if detail is None:
                metrics = _empty_compare_metrics()
                return {
                    "player_id": player_id,
                    "player_name": None,
                    "headshot_url": None,
                    "player_initials": "NBA",
                    "latest_team_abbr": None,
                    "latest_game_date": None,
                    "window": window,
                    "window_label": _compare_window_label(window),
                    "state": STATE_UNAVAILABLE,
                    "state_reason": "Player not found",
                    "games_in_window": None,
                    "window_games_expected": _compare_window_expected_games(window),
                    "has_full_window": False,
                    "metrics": metrics,
                    "metric_rows": _build_compare_metric_rows(metrics, focus),
                }

            games_in_window = _to_int((row or {}).get("games_in_window"))
            expected_games = _to_int((row or {}).get("window_games_expected"))
            if expected_games is None:
                expected_games = _compare_window_expected_games(window)
            if row is None:
                state = STATE_UNAVAILABLE
                state_reason = (
                    detail["availability_reason"]
                    if detail["availability_state"] == STATE_UNAVAILABLE
                    else _window_reason(state, _compare_window_label(window))
                )
            else:
                state = _window_state(games_in_window, expected_games)
                state_reason = _window_reason(state, _compare_window_label(window))
            metrics = {
                "fantasy_proxy_score": (row or {}).get("fantasy_proxy_score"),
                "avg_min": (row or {}).get("avg_min"),
                "avg_pts": (row or {}).get("avg_pts"),
                "avg_reb": (row or {}).get("avg_reb"),
                "avg_ast": (row or {}).get("avg_ast"),
                "avg_stl": (row or {}).get("avg_stl"),
                "avg_blk": (row or {}).get("avg_blk"),
                "avg_fg3m": (row or {}).get("avg_fg3m"),
                "avg_tov": (row or {}).get("avg_tov"),
            }
            return {
                "player_id": player_id,
                "player_name": detail["player"]["player_name"],
                "headshot_url": detail["player"].get("headshot_url"),
                "player_initials": detail["player"].get("player_initials"),
                "latest_team_abbr": (row or {}).get(
                    "latest_team_abbr", detail["player"]["team_abbr"]
                ),
                "latest_game_date": (row or {}).get(
                    "latest_game_date", detail["player"]["latest_game_date"]
                ),
                "window": window,
                "window_label": _compare_window_label(window),
                "state": state,
                "state_reason": state_reason,
                "games_in_window": games_in_window,
                "window_games_expected": expected_games,
                "has_full_window": bool((row or {}).get("has_full_window")),
                "availability_state": detail["availability_state"],
                "metrics": metrics,
                "metric_rows": _build_compare_metric_rows(metrics, focus),
                "player_detail": detail,
            }

        return {
            "season": SUPPORTED_SEASON,
            "window": window,
            "window_label": _compare_window_label(window),
            "focus": focus,
            "focus_label": str(COMPARE_FOCUS_CONFIG[focus]["label"]),
            "focus_description": str(COMPARE_FOCUS_CONFIG[focus]["description"]),
            "similarity": pair_similarity,
            "comparison": {
                "player_a": build_side(player_a_id),
                "player_b": build_side(player_b_id),
            },
        }

    def get_latest_analysis(self) -> dict[str, Any] | None:
        table = f"`{self.settings.project_id}.{self.settings.gold_dataset}.analysis_snapshots`"
        sql = f"""
        SELECT
          snapshot_id,
          snapshot_date,
          created_at_utc,
          season,
          headline,
          dek,
          body,
          trend_player,
          trend_stat,
          trend_delta,
          contribution_player_id,
          contribution_player_name,
          contribution_team_abbr,
          contribution_opponent_abbr,
          contribution_matchup,
          contribution_player_pts,
          contribution_team_pts,
          contribution_opponent_team_pts,
          contribution_player_points_share_of_team,
          contribution_player_points_share_of_game,
          contribution_scoring_margin,
          contribution_team_pts_qtr1,
          contribution_team_pts_qtr2,
          contribution_team_pts_qtr3,
          contribution_team_pts_qtr4,
          contribution_team_pts_ot_total,
          contribution_game_date,
          context_player_id,
          context_player_name,
          context_team_abbr,
          context_team_name,
          context_position,
          context_height,
          context_weight,
          context_roster_status,
          context_season_exp,
          context_draft_year,
          context_draft_round,
          context_draft_number,
          freshness_ts,
          source_run_id
        FROM {table}
        WHERE season = @season
        ORDER BY snapshot_date DESC, created_at_utc DESC, snapshot_id DESC
        LIMIT 1
        """
        rows = self._query(
            sql, [bigquery.ScalarQueryParameter("season", "STRING", SUPPORTED_SEASON)]
        )
        return build_analysis_payload(rows[0]) if rows else None

    def get_latest_successful_run(self) -> dict[str, Any] | None:
        table = f"`{self.settings.project_id}.{self.settings.metadata_dataset}.pipeline_run_log`"
        sql = f"""
        SELECT
          season,
          finished_at_utc
        FROM {table}
        WHERE season = @season
          AND status = 'success'
        ORDER BY finished_at_utc DESC
        LIMIT 1
        """
        rows = self._query(
            sql, [bigquery.ScalarQueryParameter("season", "STRING", SUPPORTED_SEASON)]
        )
        return rows[0] if rows else None

    def get_season_coverage(self) -> dict[str, Any] | None:
        sql = f"""
        SELECT
          @season AS season,
          MIN(game_date) AS first_game_date,
          MAX(game_date) AS latest_game_date,
          COUNT(DISTINCT game_id) AS game_count,
          COUNT(*) AS player_game_rows,
          COUNTIF(season_type = 'Regular Season') > 0 AS has_regular_season,
          COUNTIF(season_type = 'Playoffs') > 0 AS has_playoffs
        FROM {self._fct_game_stats_table()}
        WHERE season = @season
        """
        try:
            rows = self._query(
                sql,
                [bigquery.ScalarQueryParameter("season", "STRING", SUPPORTED_SEASON)],
            )
        except BQAPIError:
            return None
        return build_season_coverage_payload(rows[0] if rows else None)

    def get_player_game_log(
        self,
        player_id: int,
        limit: int = 30,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict[str, Any] | None:
        identity = self._fetch_player_identity(player_id)
        if identity is None:
            return None
        return self._fetch_player_game_log_payload(
            identity,
            limit=limit,
            start_date=start_date,
            end_date=end_date,
        )

    def _format_recent_performance_initial_payload(
        self,
        rows: list[dict[str, Any]],
        *,
        parsed_game_date: date | None,
        game_id: str | None,
    ) -> dict[str, Any]:
        dates: list[dict[str, Any]] = []
        games: list[dict[str, Any]] = []
        players: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(str(row.get("payload") or "{}"))
            except json.JSONDecodeError:
                continue
            section = row.get("section")
            if section == "date":
                date_value = payload.get("game_date")
                if date_value is not None:
                    dates.append(
                        {
                            "value": str(date_value),
                            "label": _format_home_date_label(str(date_value)),
                        }
                    )
            elif section == "game":
                games.append(_format_recent_performance_game(payload))
            elif section == "player":
                players.append(_format_recent_performance_row(payload))

        selected_date = (
            parsed_game_date.isoformat()
            if parsed_game_date is not None
            else (dates[0]["value"] if dates else None)
        )
        return {
            "season": SUPPORTED_SEASON,
            "dates": dates,
            "selected_date": selected_date,
            "selected_game_id": game_id,
            "games": games,
            "players": players,
        }

    def _format_recent_performance_initial_table_rows(
        self,
        rows: list[dict[str, Any]],
        *,
        parsed_game_date: date | None,
        game_id: str | None,
        limit: int,
    ) -> dict[str, Any]:
        season_rows = [row for row in rows if row.get("season") == SUPPORTED_SEASON]
        date_values = sorted(
            {
                str(row["game_date"])
                for row in season_rows
                if row.get("game_date") is not None
            },
            reverse=True,
        )
        dates = [
            {"value": value, "label": _format_home_date_label(value)}
            for value in date_values
        ]
        selected_date = (
            parsed_game_date.isoformat()
            if parsed_game_date is not None
            else (date_values[0] if date_values else None)
        )
        if selected_date is None:
            return {
                "season": SUPPORTED_SEASON,
                "dates": dates,
                "selected_date": None,
                "selected_game_id": game_id,
                "games": [],
                "players": [],
            }

        selected_rows = [
            row for row in season_rows if str(row.get("game_date")) == selected_date
        ]
        games_by_id: dict[str, dict[str, Any]] = {}
        for row in selected_rows:
            row_game_id = row.get("game_id")
            if row_game_id is None:
                continue
            games_by_id.setdefault(
                str(row_game_id),
                {
                    "game_id": row_game_id,
                    "game_date": row.get("game_date"),
                    "teams": row.get("teams"),
                    "matchup": row.get("game_matchup"),
                    "home_team_abbr": row.get("home_team_abbr"),
                    "away_team_abbr": row.get("away_team_abbr"),
                    "home_team_pts": row.get("home_team_pts"),
                    "away_team_pts": row.get("away_team_pts"),
                    "players_played": row.get("players_played"),
                },
            )
        games = [
            _format_recent_performance_game(row)
            for row in sorted(
                games_by_id.values(),
                key=lambda item: (
                    str(item.get("game_date") or ""),
                    str(item["game_id"]),
                ),
                reverse=True,
            )
        ]

        player_rows = selected_rows
        if game_id:
            player_rows = [
                row for row in player_rows if str(row.get("game_id") or "") == game_id
            ]
        player_rows = sorted(
            player_rows,
            key=lambda row: (
                -(abs(_to_float(row.get("performance_score")) or 0)),
                -(_to_int(row.get("above_count")) or 0),
                str(row.get("player_name") or ""),
            ),
        )[:limit]
        players = [_format_recent_performance_row(row) for row in player_rows]
        return {
            "season": SUPPORTED_SEASON,
            "dates": dates,
            "selected_date": selected_date,
            "selected_game_id": game_id,
            "games": games,
            "players": players,
        }

    def _fetch_recent_performance_initial_table_rows(
        self,
        *,
        parsed_game_date: date | None,
        game_id: str | None,
        limit: int,
    ) -> dict[str, Any] | None:
        rows = self._try_fetch_recent_performance_table_rows()
        if rows is None:
            return None
        return self._format_recent_performance_initial_table_rows(
            rows,
            parsed_game_date=parsed_game_date,
            game_id=game_id,
            limit=limit,
        )

    def _fetch_recent_performance_initial_table(
        self,
        *,
        parsed_game_date: date | None,
        game_id: str | None,
        limit: int,
    ) -> dict[str, Any]:
        selected_date_sql = (
            "@game_date"
            if parsed_game_date is not None
            else "(SELECT MAX(game_date) FROM date_options)"
        )
        game_filter = ""
        params: list[bigquery.ScalarQueryParameter] = [
            bigquery.ScalarQueryParameter("season", "STRING", SUPPORTED_SEASON),
            bigquery.ScalarQueryParameter("limit", "INT64", limit),
        ]
        if parsed_game_date is not None:
            params.append(
                bigquery.ScalarQueryParameter("game_date", "DATE", parsed_game_date)
            )
        if game_id:
            game_filter = "AND selected_rows.game_id = @game_id"
            params.append(bigquery.ScalarQueryParameter("game_id", "STRING", game_id))

        table = self._recent_performance_table()
        sql = f"""
        WITH date_options AS (
          SELECT DISTINCT game_date
          FROM {table}
          WHERE season = @season
        ),
        selected_date AS (
          SELECT {selected_date_sql} AS game_date
        ),
        date_payloads AS (
          SELECT
            'date' AS section,
            ROW_NUMBER() OVER (ORDER BY game_date DESC) AS sort_index,
            TO_JSON_STRING(STRUCT(game_date)) AS payload
          FROM date_options
        ),
        game_rows AS (
          SELECT
            rows.game_id,
            rows.game_date,
            ANY_VALUE(rows.teams) AS teams,
            ANY_VALUE(rows.game_matchup) AS matchup,
            ANY_VALUE(rows.home_team_abbr) AS home_team_abbr,
            ANY_VALUE(rows.away_team_abbr) AS away_team_abbr,
            ANY_VALUE(rows.home_team_pts) AS home_team_pts,
            ANY_VALUE(rows.away_team_pts) AS away_team_pts,
            MAX(rows.players_played) AS players_played
          FROM {table} rows
          CROSS JOIN selected_date
          WHERE rows.season = @season
            AND selected_date.game_date IS NOT NULL
            AND rows.game_date = selected_date.game_date
          GROUP BY rows.game_id, rows.game_date
        ),
        game_payloads AS (
          SELECT
            'game' AS section,
            ROW_NUMBER() OVER (ORDER BY game_date DESC, game_id DESC) AS sort_index,
            TO_JSON_STRING(STRUCT(
              game_id,
              game_date,
              teams,
              matchup,
              home_team_abbr,
              away_team_abbr,
              home_team_pts,
              away_team_pts,
              players_played
            )) AS payload
          FROM game_rows
        ),
        selected_rows AS (
          SELECT rows.*
          FROM {table} rows
          CROSS JOIN selected_date
          WHERE rows.season = @season
            AND selected_date.game_date IS NOT NULL
            AND rows.game_date = selected_date.game_date
        ),
        ranked_rows AS (
          SELECT
            selected_rows.*,
            ROW_NUMBER() OVER (
              ORDER BY ABS(performance_score) DESC, above_count DESC, player_name
            ) AS sort_index
          FROM selected_rows
          WHERE TRUE
            {game_filter}
        ),
        player_payloads AS (
          SELECT
            'player' AS section,
            sort_index,
            TO_JSON_STRING(STRUCT(
              season_type,
              game_id,
              game_date,
              player_id,
              player_name,
              team_abbr,
              opponent_abbr,
              home_away,
              matchup,
              wl,
              min,
              pts,
              reb,
              ast,
              stl,
              blk,
              fg_pct,
              ft_pct,
              fg3m,
              games_sampled,
              avg_pts,
              avg_reb,
              avg_ast,
              avg_stl,
              avg_blk,
              avg_min,
              avg_fg_pct,
              avg_ft_pct,
              avg_fg3m,
              pts_delta,
              reb_delta,
              ast_delta,
              stl_delta,
              blk_delta,
              min_delta,
              fg_pct_delta,
              ft_pct_delta,
              fg3m_delta,
              pts_delta_pct,
              reb_delta_pct,
              ast_delta_pct,
              stl_delta_pct,
              blk_delta_pct,
              min_delta_pct,
              fg_pct_delta_pct,
              ft_pct_delta_pct,
              fg3m_delta_pct,
              performance_score,
              performance_status,
              above_count,
              below_count
            )) AS payload
          FROM ranked_rows
          WHERE sort_index <= @limit
        )
        SELECT section, sort_index, payload
        FROM date_payloads
        UNION ALL
        SELECT section, sort_index, payload
        FROM game_payloads
        UNION ALL
        SELECT section, sort_index, payload
        FROM player_payloads
        ORDER BY
          CASE section WHEN 'date' THEN 1 WHEN 'game' THEN 2 ELSE 3 END,
          sort_index
        """
        rows = self._query(sql, params)
        return self._format_recent_performance_initial_payload(
            rows,
            parsed_game_date=parsed_game_date,
            game_id=game_id,
        )

    def get_recent_performance_initial(
        self,
        *,
        game_date: str | None = None,
        game_id: str | None = None,
        limit: int = 240,
    ) -> dict[str, Any]:
        parsed_game_date = _parse_iso_date(game_date)
        if game_date is not None and parsed_game_date is None:
            return {
                "season": SUPPORTED_SEASON,
                "dates": [],
                "selected_date": None,
                "selected_game_id": game_id,
                "games": [],
                "players": [],
            }
        limit = max(1, min(500, int(limit)))
        table_rows_payload = self._fetch_recent_performance_initial_table_rows(
            parsed_game_date=parsed_game_date,
            game_id=game_id,
            limit=limit,
        )
        if table_rows_payload is not None:
            return table_rows_payload
        try:
            return self._fetch_recent_performance_initial_table(
                parsed_game_date=parsed_game_date,
                game_id=game_id,
                limit=limit,
            )
        except BQAPIError:
            pass

        selected_date_sql = (
            "@game_date"
            if parsed_game_date is not None
            else "(SELECT MAX(game_date) FROM available_dates)"
        )
        game_filter = ""
        params: list[bigquery.ScalarQueryParameter] = [
            bigquery.ScalarQueryParameter("season", "STRING", SUPPORTED_SEASON),
            bigquery.ScalarQueryParameter("limit", "INT64", limit),
        ]
        if parsed_game_date is not None:
            params.append(
                bigquery.ScalarQueryParameter("game_date", "DATE", parsed_game_date)
            )
        if game_id:
            game_filter = "AND s.game_id = @game_id"
            params.append(bigquery.ScalarQueryParameter("game_id", "STRING", game_id))

        sql = f"""
        WITH available_dates AS (
          SELECT DISTINCT
            stats.game_date
          FROM {self._fct_game_stats_table()} stats
          WHERE stats.season = @season
            AND stats.season_type = 'Playoffs'
            AND COALESCE(SAFE_CAST(stats.min AS FLOAT64), 0) >= 1
        ),
        selected_date AS (
          SELECT {selected_date_sql} AS game_date
        ),
        date_payloads AS (
          SELECT
            'date' AS section,
            ROW_NUMBER() OVER (ORDER BY game_date DESC) AS sort_index,
            TO_JSON_STRING(STRUCT(game_date)) AS payload
          FROM available_dates
        ),
        playoff_games AS (
          SELECT DISTINCT
            stats.game_id,
            stats.game_date
          FROM {self._fct_game_stats_table()} stats
          CROSS JOIN selected_date
          WHERE stats.season = @season
            AND stats.season_type = 'Playoffs'
            AND selected_date.game_date IS NOT NULL
            AND stats.game_date = selected_date.game_date
            AND COALESCE(SAFE_CAST(stats.min AS FLOAT64), 0) >= 1
        ),
        game_rollups AS (
          SELECT
            playoff_games.game_id,
            playoff_games.game_date,
            STRING_AGG(DISTINCT stats.team_abbr, ' / ' ORDER BY stats.team_abbr) AS teams,
            MIN(stats.matchup) AS matchup,
            dim_game.home_team_abbr,
            dim_game.away_team_abbr,
            dim_game.home_team_pts,
            dim_game.away_team_pts,
            COUNT(DISTINCT CASE WHEN COALESCE(SAFE_CAST(stats.min AS FLOAT64), 0) >= 1 THEN stats.player_id END) AS players_played
          FROM playoff_games
          JOIN {self._fct_game_stats_table()} stats
            ON stats.season = @season
           AND stats.season_type = 'Playoffs'
           AND stats.game_id = playoff_games.game_id
          LEFT JOIN {self._dim_game_table()} dim_game
            ON dim_game.season = @season
           AND dim_game.game_id = playoff_games.game_id
          GROUP BY
            playoff_games.game_id,
            playoff_games.game_date,
            dim_game.home_team_abbr,
            dim_game.away_team_abbr,
            dim_game.home_team_pts,
            dim_game.away_team_pts
        ),
        game_payloads AS (
          SELECT
            'game' AS section,
            ROW_NUMBER() OVER (ORDER BY game_date DESC, game_id DESC) AS sort_index,
            TO_JSON_STRING(STRUCT(
              game_id,
              game_date,
              teams,
              matchup,
              home_team_abbr,
              away_team_abbr,
              home_team_pts,
              away_team_pts,
              players_played
            )) AS payload
          FROM game_rollups
        ),
        selected_players AS (
          SELECT
            s.game_id,
            s.game_date,
            s.season_type,
            SAFE_CAST(s.player_id AS INT64) AS player_id,
            s.player_name,
            s.team_abbr,
            s.opponent_abbr,
            s.home_away,
            s.matchup,
            s.wl,
            s.min,
            s.pts,
            s.reb,
            s.ast,
            s.stl,
            s.blk,
            s.fg_pct,
            s.ft_pct,
            s.fg3m
          FROM {self._fct_game_stats_table()} s
          CROSS JOIN selected_date
          WHERE s.season = @season
            AND s.season_type = 'Playoffs'
            AND selected_date.game_date IS NOT NULL
            AND s.game_date = selected_date.game_date
            AND COALESCE(SAFE_CAST(s.min AS FLOAT64), 0) >= 1
            {game_filter}
        ),
        selected_player_ids AS (
          SELECT DISTINCT player_id
          FROM selected_players
          WHERE player_id IS NOT NULL
        ),
        baseline AS (
          SELECT
            SAFE_CAST(stats.player_id AS INT64) AS player_id,
            COUNT(*) AS games_sampled,
            AVG(stats.pts) AS avg_pts,
            AVG(stats.reb) AS avg_reb,
            AVG(stats.ast) AS avg_ast,
            AVG(stats.stl) AS avg_stl,
            AVG(stats.blk) AS avg_blk,
            AVG(stats.min) AS avg_min,
            AVG(stats.fg_pct) AS avg_fg_pct,
            AVG(stats.ft_pct) AS avg_ft_pct,
            AVG(stats.fg3m) AS avg_fg3m,
            STDDEV_POP(stats.pts) AS sd_pts,
            STDDEV_POP(stats.reb) AS sd_reb,
            STDDEV_POP(stats.ast) AS sd_ast,
            STDDEV_POP(stats.stl) AS sd_stl,
            STDDEV_POP(stats.blk) AS sd_blk
          FROM {self._fct_game_stats_table()} stats
          JOIN selected_player_ids
            ON selected_player_ids.player_id = SAFE_CAST(stats.player_id AS INT64)
          CROSS JOIN selected_date
          WHERE stats.season = @season
            AND stats.game_date <= selected_date.game_date
            AND COALESCE(SAFE_CAST(stats.min AS FLOAT64), 0) >= 1
          GROUP BY player_id
        ),
        metric_rows AS (
          SELECT
            selected_players.*,
            baseline.games_sampled,
            baseline.avg_pts,
            baseline.avg_reb,
            baseline.avg_ast,
            baseline.avg_stl,
            baseline.avg_blk,
            baseline.avg_min,
            baseline.avg_fg_pct,
            baseline.avg_ft_pct,
            baseline.avg_fg3m,
            ROUND(selected_players.pts - baseline.avg_pts, 1) AS pts_delta,
            ROUND(selected_players.reb - baseline.avg_reb, 1) AS reb_delta,
            ROUND(selected_players.ast - baseline.avg_ast, 1) AS ast_delta,
            ROUND(selected_players.stl - baseline.avg_stl, 1) AS stl_delta,
            ROUND(selected_players.blk - baseline.avg_blk, 1) AS blk_delta,
            ROUND(selected_players.min - baseline.avg_min, 1) AS min_delta,
            ROUND(selected_players.fg_pct - baseline.avg_fg_pct, 3) AS fg_pct_delta,
            ROUND(selected_players.ft_pct - baseline.avg_ft_pct, 3) AS ft_pct_delta,
            ROUND(selected_players.fg3m - baseline.avg_fg3m, 1) AS fg3m_delta,
            ROUND(SAFE_DIVIDE(selected_players.pts - baseline.avg_pts, NULLIF(baseline.avg_pts, 0)) * 100, 1) AS pts_delta_pct,
            ROUND(SAFE_DIVIDE(selected_players.reb - baseline.avg_reb, NULLIF(baseline.avg_reb, 0)) * 100, 1) AS reb_delta_pct,
            ROUND(SAFE_DIVIDE(selected_players.ast - baseline.avg_ast, NULLIF(baseline.avg_ast, 0)) * 100, 1) AS ast_delta_pct,
            ROUND(SAFE_DIVIDE(selected_players.stl - baseline.avg_stl, NULLIF(baseline.avg_stl, 0)) * 100, 1) AS stl_delta_pct,
            ROUND(SAFE_DIVIDE(selected_players.blk - baseline.avg_blk, NULLIF(baseline.avg_blk, 0)) * 100, 1) AS blk_delta_pct,
            ROUND(SAFE_DIVIDE(selected_players.min - baseline.avg_min, NULLIF(baseline.avg_min, 0)) * 100, 1) AS min_delta_pct,
            ROUND(SAFE_DIVIDE(selected_players.fg_pct - baseline.avg_fg_pct, NULLIF(baseline.avg_fg_pct, 0)) * 100, 1) AS fg_pct_delta_pct,
            ROUND(SAFE_DIVIDE(selected_players.ft_pct - baseline.avg_ft_pct, NULLIF(baseline.avg_ft_pct, 0)) * 100, 1) AS ft_pct_delta_pct,
            ROUND(SAFE_DIVIDE(selected_players.fg3m - baseline.avg_fg3m, NULLIF(baseline.avg_fg3m, 0)) * 100, 1) AS fg3m_delta_pct,
            CASE
              WHEN baseline.sd_pts > 0 THEN SAFE_DIVIDE(selected_players.pts - baseline.avg_pts, baseline.sd_pts)
              WHEN selected_players.pts > baseline.avg_pts THEN 1.0
              WHEN selected_players.pts < baseline.avg_pts THEN -1.0
              ELSE 0.0
            END AS z_pts,
            CASE
              WHEN baseline.sd_reb > 0 THEN SAFE_DIVIDE(selected_players.reb - baseline.avg_reb, baseline.sd_reb)
              WHEN selected_players.reb > baseline.avg_reb THEN 1.0
              WHEN selected_players.reb < baseline.avg_reb THEN -1.0
              ELSE 0.0
            END AS z_reb,
            CASE
              WHEN baseline.sd_ast > 0 THEN SAFE_DIVIDE(selected_players.ast - baseline.avg_ast, baseline.sd_ast)
              WHEN selected_players.ast > baseline.avg_ast THEN 1.0
              WHEN selected_players.ast < baseline.avg_ast THEN -1.0
              ELSE 0.0
            END AS z_ast,
            CASE
              WHEN baseline.sd_stl > 0 THEN SAFE_DIVIDE(selected_players.stl - baseline.avg_stl, baseline.sd_stl)
              WHEN selected_players.stl > baseline.avg_stl THEN 1.0
              WHEN selected_players.stl < baseline.avg_stl THEN -1.0
              ELSE 0.0
            END AS z_stl,
            CASE
              WHEN baseline.sd_blk > 0 THEN SAFE_DIVIDE(selected_players.blk - baseline.avg_blk, baseline.sd_blk)
              WHEN selected_players.blk > baseline.avg_blk THEN 1.0
              WHEN selected_players.blk < baseline.avg_blk THEN -1.0
              ELSE 0.0
            END AS z_blk,
            (
              CASE WHEN selected_players.pts > baseline.avg_pts THEN 1 ELSE 0 END
              + CASE WHEN selected_players.reb > baseline.avg_reb THEN 1 ELSE 0 END
              + CASE WHEN selected_players.ast > baseline.avg_ast THEN 1 ELSE 0 END
              + CASE WHEN selected_players.stl > baseline.avg_stl THEN 1 ELSE 0 END
              + CASE WHEN selected_players.blk > baseline.avg_blk THEN 1 ELSE 0 END
            ) AS above_count,
            (
              CASE WHEN selected_players.pts < baseline.avg_pts THEN 1 ELSE 0 END
              + CASE WHEN selected_players.reb < baseline.avg_reb THEN 1 ELSE 0 END
              + CASE WHEN selected_players.ast < baseline.avg_ast THEN 1 ELSE 0 END
              + CASE WHEN selected_players.stl < baseline.avg_stl THEN 1 ELSE 0 END
              + CASE WHEN selected_players.blk < baseline.avg_blk THEN 1 ELSE 0 END
            ) AS below_count
          FROM selected_players
          JOIN baseline
            ON baseline.player_id = selected_players.player_id
        ),
        scored AS (
          SELECT
            *,
            ROUND(z_pts + z_reb + z_ast + z_stl + z_blk, 2) AS performance_score
          FROM metric_rows
        ),
        ranked_players AS (
          SELECT
            *,
            CASE
              WHEN performance_score >= 1.0 OR (performance_score > 0 AND above_count >= 3) THEN 'above'
              WHEN performance_score <= -1.0 OR (performance_score < 0 AND below_count >= 3) THEN 'below'
              ELSE 'near'
            END AS performance_status,
            ROW_NUMBER() OVER (
              ORDER BY ABS(performance_score) DESC, above_count DESC, player_name
            ) AS player_rank
          FROM scored
        ),
        player_payloads AS (
          SELECT
            'player' AS section,
            player_rank AS sort_index,
            TO_JSON_STRING(STRUCT(
              season_type,
              game_id,
              game_date,
              player_id,
              player_name,
              team_abbr,
              opponent_abbr,
              home_away,
              matchup,
              wl,
              min,
              pts,
              reb,
              ast,
              stl,
              blk,
              fg_pct,
              ft_pct,
              fg3m,
              games_sampled,
              avg_pts,
              avg_reb,
              avg_ast,
              avg_stl,
              avg_blk,
              avg_min,
              avg_fg_pct,
              avg_ft_pct,
              avg_fg3m,
              pts_delta,
              reb_delta,
              ast_delta,
              stl_delta,
              blk_delta,
              min_delta,
              fg_pct_delta,
              ft_pct_delta,
              fg3m_delta,
              pts_delta_pct,
              reb_delta_pct,
              ast_delta_pct,
              stl_delta_pct,
              blk_delta_pct,
              min_delta_pct,
              fg_pct_delta_pct,
              ft_pct_delta_pct,
              fg3m_delta_pct,
              performance_score,
              performance_status,
              above_count,
              below_count
            )) AS payload
          FROM ranked_players
          WHERE player_rank <= @limit
        )
        SELECT section, sort_index, payload
        FROM date_payloads
        UNION ALL
        SELECT section, sort_index, payload
        FROM game_payloads
        UNION ALL
        SELECT section, sort_index, payload
        FROM player_payloads
        ORDER BY
          CASE section WHEN 'date' THEN 1 WHEN 'game' THEN 2 ELSE 3 END,
          sort_index
        """
        try:
            rows = self._query(sql, params)
        except BQAPIError:
            return {
                "season": SUPPORTED_SEASON,
                "dates": [],
                "selected_date": None,
                "selected_game_id": game_id,
                "games": [],
                "players": [],
            }

        return self._format_recent_performance_initial_payload(
            rows,
            parsed_game_date=parsed_game_date,
            game_id=game_id,
        )

    def get_recent_performance_dates(self) -> list[dict[str, Any]]:
        sql = f"""
        SELECT DISTINCT
          stats.game_date
        FROM {self._fct_game_stats_table()} stats
        WHERE stats.season = @season
          AND stats.season_type = 'Playoffs'
          AND COALESCE(SAFE_CAST(stats.min AS FLOAT64), 0) >= 1
        ORDER BY stats.game_date DESC
        """
        try:
            rows = self._query(
                sql,
                [bigquery.ScalarQueryParameter("season", "STRING", SUPPORTED_SEASON)],
            )
        except BQAPIError:
            return []
        return [
            {
                "value": str(row["game_date"]),
                "label": _format_home_date_label(str(row["game_date"])),
            }
            for row in rows
            if row.get("game_date") is not None
        ]

    def get_recent_performance_games(
        self, *, game_date: str | None = None
    ) -> list[dict[str, Any]]:
        filters = [
            "stats.season = @season",
            "stats.season_type = 'Playoffs'",
            "COALESCE(SAFE_CAST(stats.min AS FLOAT64), 0) >= 1",
        ]
        params: list[bigquery.ScalarQueryParameter] = [
            bigquery.ScalarQueryParameter("season", "STRING", SUPPORTED_SEASON)
        ]
        parsed_game_date = _parse_iso_date(game_date)
        if parsed_game_date is not None:
            filters.append("stats.game_date = @game_date")
            params.append(
                bigquery.ScalarQueryParameter("game_date", "DATE", parsed_game_date)
            )
        sql = f"""
        WITH playoff_games AS (
          SELECT DISTINCT
            stats.game_id,
            stats.game_date
          FROM {self._fct_game_stats_table()} stats
          WHERE {" AND ".join(filters)}
        )
        SELECT
          playoff_games.game_id,
          playoff_games.game_date,
          STRING_AGG(DISTINCT stats.team_abbr, ' / ' ORDER BY stats.team_abbr) AS teams,
          MIN(stats.matchup) AS matchup,
          dim_game.home_team_abbr,
          dim_game.away_team_abbr,
          dim_game.home_team_pts,
          dim_game.away_team_pts,
          COUNT(DISTINCT CASE WHEN COALESCE(SAFE_CAST(stats.min AS FLOAT64), 0) >= 1 THEN stats.player_id END) AS players_played
        FROM playoff_games
        JOIN {self._fct_game_stats_table()} stats
          ON stats.season = @season
         AND stats.season_type = 'Playoffs'
         AND stats.game_id = playoff_games.game_id
        LEFT JOIN {self._dim_game_table()} dim_game
          ON dim_game.season = @season
         AND dim_game.game_id = playoff_games.game_id
        GROUP BY
          playoff_games.game_id,
          playoff_games.game_date,
          dim_game.home_team_abbr,
          dim_game.away_team_abbr,
          dim_game.home_team_pts,
          dim_game.away_team_pts
        ORDER BY playoff_games.game_date DESC, playoff_games.game_id DESC
        """
        try:
            rows = self._query(sql, params)
        except BQAPIError:
            return []
        return [_format_recent_performance_game(row) for row in rows]

    def get_recent_performance_players(
        self,
        *,
        game_date: str,
        game_id: str | None = None,
        limit: int = 240,
    ) -> list[dict[str, Any]]:
        parsed_game_date = _parse_iso_date(game_date)
        if parsed_game_date is None:
            return []
        limit = max(1, min(500, int(limit)))
        selected_filters = [
            "s.season = @season",
            "s.season_type = 'Playoffs'",
            "s.game_date = @game_date",
            "COALESCE(SAFE_CAST(s.min AS FLOAT64), 0) >= 1",
        ]
        params: list[bigquery.ScalarQueryParameter] = [
            bigquery.ScalarQueryParameter("season", "STRING", SUPPORTED_SEASON),
            bigquery.ScalarQueryParameter("game_date", "DATE", parsed_game_date),
            bigquery.ScalarQueryParameter("limit", "INT64", limit),
        ]
        if game_id:
            selected_filters.append("s.game_id = @game_id")
            params.append(bigquery.ScalarQueryParameter("game_id", "STRING", game_id))
        sql = f"""
        WITH selected AS (
          SELECT
            s.game_id,
            s.game_date,
            s.season_type,
            s.player_id,
            s.player_name,
            s.team_abbr,
            s.opponent_abbr,
            s.home_away,
            s.matchup,
            s.wl,
            s.min,
            s.pts,
            s.reb,
            s.ast,
            s.stl,
            s.blk,
            s.fg_pct,
            s.ft_pct,
            s.fg3m
          FROM {self._fct_game_stats_table()} s
          WHERE {" AND ".join(selected_filters)}
        ),
        baseline AS (
          SELECT
            player_id,
            COUNT(*) AS games_sampled,
            AVG(pts) AS avg_pts,
            AVG(reb) AS avg_reb,
            AVG(ast) AS avg_ast,
            AVG(stl) AS avg_stl,
            AVG(blk) AS avg_blk,
            AVG(min) AS avg_min,
            AVG(fg_pct) AS avg_fg_pct,
            AVG(ft_pct) AS avg_ft_pct,
            AVG(fg3m) AS avg_fg3m,
            STDDEV_POP(pts) AS sd_pts,
            STDDEV_POP(reb) AS sd_reb,
            STDDEV_POP(ast) AS sd_ast,
            STDDEV_POP(stl) AS sd_stl,
            STDDEV_POP(blk) AS sd_blk
          FROM {self._fct_game_stats_table()}
          WHERE season = @season
            AND game_date <= @game_date
            AND COALESCE(SAFE_CAST(min AS FLOAT64), 0) >= 1
          GROUP BY player_id
        ),
        metric_rows AS (
          SELECT
            selected.*,
            baseline.games_sampled,
            baseline.avg_pts,
            baseline.avg_reb,
            baseline.avg_ast,
            baseline.avg_stl,
            baseline.avg_blk,
            baseline.avg_min,
            baseline.avg_fg_pct,
            baseline.avg_ft_pct,
            baseline.avg_fg3m,
            ROUND(selected.pts - baseline.avg_pts, 1) AS pts_delta,
            ROUND(selected.reb - baseline.avg_reb, 1) AS reb_delta,
            ROUND(selected.ast - baseline.avg_ast, 1) AS ast_delta,
            ROUND(selected.stl - baseline.avg_stl, 1) AS stl_delta,
            ROUND(selected.blk - baseline.avg_blk, 1) AS blk_delta,
            ROUND(selected.min - baseline.avg_min, 1) AS min_delta,
            ROUND(selected.fg_pct - baseline.avg_fg_pct, 3) AS fg_pct_delta,
            ROUND(selected.ft_pct - baseline.avg_ft_pct, 3) AS ft_pct_delta,
            ROUND(selected.fg3m - baseline.avg_fg3m, 1) AS fg3m_delta,
            ROUND(SAFE_DIVIDE(selected.pts - baseline.avg_pts, NULLIF(baseline.avg_pts, 0)) * 100, 1) AS pts_delta_pct,
            ROUND(SAFE_DIVIDE(selected.reb - baseline.avg_reb, NULLIF(baseline.avg_reb, 0)) * 100, 1) AS reb_delta_pct,
            ROUND(SAFE_DIVIDE(selected.ast - baseline.avg_ast, NULLIF(baseline.avg_ast, 0)) * 100, 1) AS ast_delta_pct,
            ROUND(SAFE_DIVIDE(selected.stl - baseline.avg_stl, NULLIF(baseline.avg_stl, 0)) * 100, 1) AS stl_delta_pct,
            ROUND(SAFE_DIVIDE(selected.blk - baseline.avg_blk, NULLIF(baseline.avg_blk, 0)) * 100, 1) AS blk_delta_pct,
            ROUND(SAFE_DIVIDE(selected.min - baseline.avg_min, NULLIF(baseline.avg_min, 0)) * 100, 1) AS min_delta_pct,
            ROUND(SAFE_DIVIDE(selected.fg_pct - baseline.avg_fg_pct, NULLIF(baseline.avg_fg_pct, 0)) * 100, 1) AS fg_pct_delta_pct,
            ROUND(SAFE_DIVIDE(selected.ft_pct - baseline.avg_ft_pct, NULLIF(baseline.avg_ft_pct, 0)) * 100, 1) AS ft_pct_delta_pct,
            ROUND(SAFE_DIVIDE(selected.fg3m - baseline.avg_fg3m, NULLIF(baseline.avg_fg3m, 0)) * 100, 1) AS fg3m_delta_pct,
            CASE
              WHEN baseline.sd_pts > 0 THEN SAFE_DIVIDE(selected.pts - baseline.avg_pts, baseline.sd_pts)
              WHEN selected.pts > baseline.avg_pts THEN 1.0
              WHEN selected.pts < baseline.avg_pts THEN -1.0
              ELSE 0.0
            END AS z_pts,
            CASE
              WHEN baseline.sd_reb > 0 THEN SAFE_DIVIDE(selected.reb - baseline.avg_reb, baseline.sd_reb)
              WHEN selected.reb > baseline.avg_reb THEN 1.0
              WHEN selected.reb < baseline.avg_reb THEN -1.0
              ELSE 0.0
            END AS z_reb,
            CASE
              WHEN baseline.sd_ast > 0 THEN SAFE_DIVIDE(selected.ast - baseline.avg_ast, baseline.sd_ast)
              WHEN selected.ast > baseline.avg_ast THEN 1.0
              WHEN selected.ast < baseline.avg_ast THEN -1.0
              ELSE 0.0
            END AS z_ast,
            CASE
              WHEN baseline.sd_stl > 0 THEN SAFE_DIVIDE(selected.stl - baseline.avg_stl, baseline.sd_stl)
              WHEN selected.stl > baseline.avg_stl THEN 1.0
              WHEN selected.stl < baseline.avg_stl THEN -1.0
              ELSE 0.0
            END AS z_stl,
            CASE
              WHEN baseline.sd_blk > 0 THEN SAFE_DIVIDE(selected.blk - baseline.avg_blk, baseline.sd_blk)
              WHEN selected.blk > baseline.avg_blk THEN 1.0
              WHEN selected.blk < baseline.avg_blk THEN -1.0
              ELSE 0.0
            END AS z_blk,
            (
              CASE WHEN selected.pts > baseline.avg_pts THEN 1 ELSE 0 END
              + CASE WHEN selected.reb > baseline.avg_reb THEN 1 ELSE 0 END
              + CASE WHEN selected.ast > baseline.avg_ast THEN 1 ELSE 0 END
              + CASE WHEN selected.stl > baseline.avg_stl THEN 1 ELSE 0 END
              + CASE WHEN selected.blk > baseline.avg_blk THEN 1 ELSE 0 END
            ) AS above_count,
            (
              CASE WHEN selected.pts < baseline.avg_pts THEN 1 ELSE 0 END
              + CASE WHEN selected.reb < baseline.avg_reb THEN 1 ELSE 0 END
              + CASE WHEN selected.ast < baseline.avg_ast THEN 1 ELSE 0 END
              + CASE WHEN selected.stl < baseline.avg_stl THEN 1 ELSE 0 END
              + CASE WHEN selected.blk < baseline.avg_blk THEN 1 ELSE 0 END
            ) AS below_count
          FROM selected
          JOIN baseline
            ON baseline.player_id = selected.player_id
        ),
        scored AS (
          SELECT
            *,
            ROUND(z_pts + z_reb + z_ast + z_stl + z_blk, 2) AS performance_score
          FROM metric_rows
        )
        SELECT
          *,
          CASE
            WHEN performance_score >= 1.0 OR (performance_score > 0 AND above_count >= 3) THEN 'above'
            WHEN performance_score <= -1.0 OR (performance_score < 0 AND below_count >= 3) THEN 'below'
            ELSE 'near'
          END AS performance_status
        FROM scored
        ORDER BY ABS(performance_score) DESC, above_count DESC, player_name
        LIMIT @limit
        """
        try:
            rows = self._query(sql, params)
        except BQAPIError:
            return []
        return [_format_recent_performance_row(row) for row in rows]

    def _fetch_recent_performance_trend(
        self, player_id: int, *, game_id: str
    ) -> dict[str, Any]:
        sql = f"""
        WITH selected AS (
          SELECT
            SAFE_CAST(player_id AS INT64) AS selected_player_id,
            SAFE_CAST(game_date AS DATE) AS selected_game_date
          FROM {self._fct_game_stats_table()}
          WHERE season = @season
            AND SAFE_CAST(player_id AS INT64) = @player_id
            AND game_id = @game_id
            AND COALESCE(SAFE_CAST(min AS FLOAT64), 0) >= 1
          LIMIT 1
        ),
        trend_window AS (
          SELECT
            stats.game_id,
            SAFE_CAST(stats.game_date AS DATE) AS game_date,
            stats.matchup,
            stats.min,
            stats.pts,
            stats.reb,
            stats.ast,
            stats.stl,
            stats.blk,
            stats.fg_pct,
            stats.ft_pct,
            stats.fg3m
          FROM {self._fct_game_stats_table()} stats
          JOIN selected
            ON SAFE_CAST(stats.player_id AS INT64) = selected.selected_player_id
          WHERE stats.season = @season
            AND SAFE_CAST(stats.game_date AS DATE)
              BETWEEN DATE_SUB(selected.selected_game_date, INTERVAL 29 DAY)
                  AND selected.selected_game_date
            AND COALESCE(SAFE_CAST(stats.min AS FLOAT64), 0) >= 1
        ),
        baseline AS (
          SELECT
            AVG(stats.pts) AS avg_pts,
            AVG(stats.reb) AS avg_reb,
            AVG(stats.ast) AS avg_ast,
            AVG(stats.stl) AS avg_stl,
            AVG(stats.blk) AS avg_blk,
            AVG(stats.min) AS avg_min,
            AVG(stats.fg_pct) AS avg_fg_pct,
            AVG(stats.ft_pct) AS avg_ft_pct,
            AVG(stats.fg3m) AS avg_fg3m
          FROM {self._fct_game_stats_table()} stats
          JOIN selected
            ON SAFE_CAST(stats.player_id AS INT64) = selected.selected_player_id
          WHERE stats.season = @season
            AND SAFE_CAST(stats.game_date AS DATE) <= selected.selected_game_date
            AND COALESCE(SAFE_CAST(stats.min AS FLOAT64), 0) >= 1
        )
        SELECT
          trend_window.*,
          baseline.avg_pts,
          baseline.avg_reb,
          baseline.avg_ast,
          baseline.avg_stl,
          baseline.avg_blk,
          baseline.avg_min,
          baseline.avg_fg_pct,
          baseline.avg_ft_pct,
          baseline.avg_fg3m
        FROM trend_window
        CROSS JOIN baseline
        ORDER BY trend_window.game_date, trend_window.game_id
        """
        try:
            rows = self._query(
                sql,
                [
                    bigquery.ScalarQueryParameter("season", "STRING", SUPPORTED_SEASON),
                    bigquery.ScalarQueryParameter("player_id", "INT64", player_id),
                    bigquery.ScalarQueryParameter("game_id", "STRING", game_id),
                ],
            )
        except BQAPIError:
            return _format_recent_performance_trend([])
        return _format_recent_performance_trend(rows)

    def _format_recent_performance_player_table_row(
        self, row: dict[str, Any]
    ) -> dict[str, Any]:
        item = _format_recent_performance_row(row, include_range=True)
        try:
            trend_points = json.loads(str(row.get("trend_points_json") or "[]"))
        except json.JSONDecodeError:
            trend_points = []
        if not isinstance(trend_points, list):
            trend_points = []

        trend_rows: list[dict[str, Any]] = []
        for point in trend_points:
            if not isinstance(point, dict):
                continue
            trend_row = dict(point)
            for config in RECENT_PERFORMANCE_STAT_CONFIG:
                key = config["key"]
                trend_row[f"avg_{key}"] = row.get(f"avg_{key}")
            trend_rows.append(trend_row)
        item["trend_30d"] = _format_recent_performance_trend(trend_rows)
        return item

    def _fetch_recent_performance_player_table_rows(
        self, player_id: int, *, game_id: str
    ) -> dict[str, Any] | None:
        rows = self._try_fetch_recent_performance_table_rows()
        if rows is None:
            return None
        for row in rows:
            if row.get("season") != SUPPORTED_SEASON:
                continue
            if _to_int(row.get("player_id")) != player_id:
                continue
            if str(row.get("game_id") or "") != game_id:
                continue
            return self._format_recent_performance_player_table_row(row)
        return None

    def _fetch_recent_performance_player_table(
        self, player_id: int, *, game_id: str
    ) -> dict[str, Any] | None:
        sql = f"""
        SELECT *
        FROM {self._recent_performance_table()}
        WHERE season = @season
          AND player_id = @player_id
          AND game_id = @game_id
        LIMIT 1
        """
        rows = self._query(
            sql,
            [
                bigquery.ScalarQueryParameter("season", "STRING", SUPPORTED_SEASON),
                bigquery.ScalarQueryParameter("player_id", "INT64", player_id),
                bigquery.ScalarQueryParameter("game_id", "STRING", game_id),
            ],
        )
        if not rows:
            return None
        return self._format_recent_performance_player_table_row(rows[0])

    def _hydrate_recent_performance_player_detail(
        self, item: dict[str, Any], player_id: int, *, game_id: str
    ) -> dict[str, Any]:
        if _recent_performance_detail_needs_hydration(item):
            game_date = item.get("game_date")
            if game_date:
                initial_payload = self.get_recent_performance_initial(
                    game_date=str(game_date),
                    game_id=game_id,
                    limit=500,
                )
                source_item = next(
                    (
                        player
                        for player in initial_payload.get("players", [])
                        if _to_int(player.get("player_id")) == player_id
                        and str(player.get("game_id") or "") == game_id
                    ),
                    None,
                )
                if source_item is not None:
                    item = _merge_recent_performance_detail_summary(item, source_item)

        if _recent_performance_trend_needs_hydration(item):
            trend = self._fetch_recent_performance_trend(player_id, game_id=game_id)
            if trend.get("points"):
                item["trend_30d"] = trend
        return item

    def get_recent_performance_player(
        self, player_id: int, *, game_id: str
    ) -> dict[str, Any] | None:
        table_item = self._fetch_recent_performance_player_table_rows(
            player_id,
            game_id=game_id,
        )
        if table_item is not None:
            return self._hydrate_recent_performance_player_detail(
                table_item, player_id, game_id=game_id
            )

        try:
            table_item = self._fetch_recent_performance_player_table(
                player_id,
                game_id=game_id,
            )
        except BQAPIError:
            table_item = None
        if table_item is not None:
            return self._hydrate_recent_performance_player_detail(
                table_item, player_id, game_id=game_id
            )

        sql = f"""
        WITH selected AS (
          SELECT
            s.game_id,
            s.game_date,
            s.season_type,
            s.player_id,
            s.player_name,
            s.team_abbr,
            s.opponent_abbr,
            s.home_away,
            s.matchup,
            s.wl,
            s.min,
            s.pts,
            s.reb,
            s.ast,
            s.stl,
            s.blk,
            s.fg_pct,
            s.ft_pct,
            s.fg3m
          FROM {self._fct_game_stats_table()} s
          WHERE s.season = @season
            AND s.player_id = @player_id
            AND s.game_id = @game_id
            AND s.season_type = 'Playoffs'
            AND COALESCE(SAFE_CAST(s.min AS FLOAT64), 0) >= 1
          LIMIT 1
        ),
        season_rows AS (
          SELECT stats.*
          FROM {self._fct_game_stats_table()} stats
          CROSS JOIN selected
          WHERE stats.season = @season
            AND stats.player_id = @player_id
            AND stats.game_date <= selected.game_date
            AND COALESCE(SAFE_CAST(stats.min AS FLOAT64), 0) >= 1
        ),
        baseline AS (
          SELECT
            COUNT(*) AS games_sampled,
            AVG(r.pts) AS avg_pts,
            AVG(r.reb) AS avg_reb,
            AVG(r.ast) AS avg_ast,
            AVG(r.stl) AS avg_stl,
            AVG(r.blk) AS avg_blk,
            AVG(r.min) AS avg_min,
            AVG(r.fg_pct) AS avg_fg_pct,
            AVG(r.ft_pct) AS avg_ft_pct,
            AVG(r.fg3m) AS avg_fg3m,
            STDDEV_POP(r.pts) AS sd_pts,
            STDDEV_POP(r.reb) AS sd_reb,
            STDDEV_POP(r.ast) AS sd_ast,
            STDDEV_POP(r.stl) AS sd_stl,
            STDDEV_POP(r.blk) AS sd_blk,
            STDDEV_POP(r.min) AS sd_min,
            STDDEV_POP(r.fg_pct) AS sd_fg_pct,
            STDDEV_POP(r.ft_pct) AS sd_ft_pct,
            STDDEV_POP(r.fg3m) AS sd_fg3m,
            APPROX_QUANTILES(r.pts, 100)[OFFSET(10)] AS pts_p10,
            APPROX_QUANTILES(r.pts, 100)[OFFSET(25)] AS pts_p25,
            APPROX_QUANTILES(r.pts, 100)[OFFSET(50)] AS pts_p50,
            APPROX_QUANTILES(r.pts, 100)[OFFSET(75)] AS pts_p75,
            APPROX_QUANTILES(r.pts, 100)[OFFSET(90)] AS pts_p90,
            ROUND(SAFE_DIVIDE(COUNTIF(r.pts < selected.pts) + 0.5 * COUNTIF(r.pts = selected.pts), COUNT(*)) * 100, 1) AS pts_percentile,
            APPROX_QUANTILES(r.reb, 100)[OFFSET(10)] AS reb_p10,
            APPROX_QUANTILES(r.reb, 100)[OFFSET(25)] AS reb_p25,
            APPROX_QUANTILES(r.reb, 100)[OFFSET(50)] AS reb_p50,
            APPROX_QUANTILES(r.reb, 100)[OFFSET(75)] AS reb_p75,
            APPROX_QUANTILES(r.reb, 100)[OFFSET(90)] AS reb_p90,
            ROUND(SAFE_DIVIDE(COUNTIF(r.reb < selected.reb) + 0.5 * COUNTIF(r.reb = selected.reb), COUNT(*)) * 100, 1) AS reb_percentile,
            APPROX_QUANTILES(r.ast, 100)[OFFSET(10)] AS ast_p10,
            APPROX_QUANTILES(r.ast, 100)[OFFSET(25)] AS ast_p25,
            APPROX_QUANTILES(r.ast, 100)[OFFSET(50)] AS ast_p50,
            APPROX_QUANTILES(r.ast, 100)[OFFSET(75)] AS ast_p75,
            APPROX_QUANTILES(r.ast, 100)[OFFSET(90)] AS ast_p90,
            ROUND(SAFE_DIVIDE(COUNTIF(r.ast < selected.ast) + 0.5 * COUNTIF(r.ast = selected.ast), COUNT(*)) * 100, 1) AS ast_percentile,
            APPROX_QUANTILES(r.stl, 100)[OFFSET(10)] AS stl_p10,
            APPROX_QUANTILES(r.stl, 100)[OFFSET(25)] AS stl_p25,
            APPROX_QUANTILES(r.stl, 100)[OFFSET(50)] AS stl_p50,
            APPROX_QUANTILES(r.stl, 100)[OFFSET(75)] AS stl_p75,
            APPROX_QUANTILES(r.stl, 100)[OFFSET(90)] AS stl_p90,
            ROUND(SAFE_DIVIDE(COUNTIF(r.stl < selected.stl) + 0.5 * COUNTIF(r.stl = selected.stl), COUNT(*)) * 100, 1) AS stl_percentile,
            APPROX_QUANTILES(r.blk, 100)[OFFSET(10)] AS blk_p10,
            APPROX_QUANTILES(r.blk, 100)[OFFSET(25)] AS blk_p25,
            APPROX_QUANTILES(r.blk, 100)[OFFSET(50)] AS blk_p50,
            APPROX_QUANTILES(r.blk, 100)[OFFSET(75)] AS blk_p75,
            APPROX_QUANTILES(r.blk, 100)[OFFSET(90)] AS blk_p90,
            ROUND(SAFE_DIVIDE(COUNTIF(r.blk < selected.blk) + 0.5 * COUNTIF(r.blk = selected.blk), COUNT(*)) * 100, 1) AS blk_percentile,
            APPROX_QUANTILES(r.min, 100)[OFFSET(10)] AS min_p10,
            APPROX_QUANTILES(r.min, 100)[OFFSET(25)] AS min_p25,
            APPROX_QUANTILES(r.min, 100)[OFFSET(50)] AS min_p50,
            APPROX_QUANTILES(r.min, 100)[OFFSET(75)] AS min_p75,
            APPROX_QUANTILES(r.min, 100)[OFFSET(90)] AS min_p90,
            ROUND(SAFE_DIVIDE(COUNTIF(r.min < selected.min) + 0.5 * COUNTIF(r.min = selected.min), COUNT(*)) * 100, 1) AS min_percentile,
            APPROX_QUANTILES(r.fg_pct, 100)[OFFSET(10)] AS fg_pct_p10,
            APPROX_QUANTILES(r.fg_pct, 100)[OFFSET(25)] AS fg_pct_p25,
            APPROX_QUANTILES(r.fg_pct, 100)[OFFSET(50)] AS fg_pct_p50,
            APPROX_QUANTILES(r.fg_pct, 100)[OFFSET(75)] AS fg_pct_p75,
            APPROX_QUANTILES(r.fg_pct, 100)[OFFSET(90)] AS fg_pct_p90,
            CASE
              WHEN selected.fg_pct IS NULL THEN NULL
              ELSE ROUND(SAFE_DIVIDE(COUNTIF(r.fg_pct < selected.fg_pct) + 0.5 * COUNTIF(r.fg_pct = selected.fg_pct), NULLIF(COUNTIF(r.fg_pct IS NOT NULL), 0)) * 100, 1)
            END AS fg_pct_percentile,
            APPROX_QUANTILES(r.ft_pct, 100)[OFFSET(10)] AS ft_pct_p10,
            APPROX_QUANTILES(r.ft_pct, 100)[OFFSET(25)] AS ft_pct_p25,
            APPROX_QUANTILES(r.ft_pct, 100)[OFFSET(50)] AS ft_pct_p50,
            APPROX_QUANTILES(r.ft_pct, 100)[OFFSET(75)] AS ft_pct_p75,
            APPROX_QUANTILES(r.ft_pct, 100)[OFFSET(90)] AS ft_pct_p90,
            CASE
              WHEN selected.ft_pct IS NULL THEN NULL
              ELSE ROUND(SAFE_DIVIDE(COUNTIF(r.ft_pct < selected.ft_pct) + 0.5 * COUNTIF(r.ft_pct = selected.ft_pct), NULLIF(COUNTIF(r.ft_pct IS NOT NULL), 0)) * 100, 1)
            END AS ft_pct_percentile,
            APPROX_QUANTILES(r.fg3m, 100)[OFFSET(10)] AS fg3m_p10,
            APPROX_QUANTILES(r.fg3m, 100)[OFFSET(25)] AS fg3m_p25,
            APPROX_QUANTILES(r.fg3m, 100)[OFFSET(50)] AS fg3m_p50,
            APPROX_QUANTILES(r.fg3m, 100)[OFFSET(75)] AS fg3m_p75,
            APPROX_QUANTILES(r.fg3m, 100)[OFFSET(90)] AS fg3m_p90,
            ROUND(SAFE_DIVIDE(COUNTIF(r.fg3m < selected.fg3m) + 0.5 * COUNTIF(r.fg3m = selected.fg3m), NULLIF(COUNTIF(r.fg3m IS NOT NULL), 0)) * 100, 1) AS fg3m_percentile
          FROM season_rows r
          CROSS JOIN selected
        ),
        metric_rows AS (
          SELECT
            selected.*,
            baseline.*,
            ROUND(selected.pts - baseline.avg_pts, 1) AS pts_delta,
            ROUND(selected.reb - baseline.avg_reb, 1) AS reb_delta,
            ROUND(selected.ast - baseline.avg_ast, 1) AS ast_delta,
            ROUND(selected.stl - baseline.avg_stl, 1) AS stl_delta,
            ROUND(selected.blk - baseline.avg_blk, 1) AS blk_delta,
            ROUND(selected.min - baseline.avg_min, 1) AS min_delta,
            ROUND(selected.fg_pct - baseline.avg_fg_pct, 3) AS fg_pct_delta,
            ROUND(selected.ft_pct - baseline.avg_ft_pct, 3) AS ft_pct_delta,
            ROUND(selected.fg3m - baseline.avg_fg3m, 1) AS fg3m_delta,
            ROUND(SAFE_DIVIDE(selected.pts - baseline.avg_pts, NULLIF(baseline.avg_pts, 0)) * 100, 1) AS pts_delta_pct,
            ROUND(SAFE_DIVIDE(selected.reb - baseline.avg_reb, NULLIF(baseline.avg_reb, 0)) * 100, 1) AS reb_delta_pct,
            ROUND(SAFE_DIVIDE(selected.ast - baseline.avg_ast, NULLIF(baseline.avg_ast, 0)) * 100, 1) AS ast_delta_pct,
            ROUND(SAFE_DIVIDE(selected.stl - baseline.avg_stl, NULLIF(baseline.avg_stl, 0)) * 100, 1) AS stl_delta_pct,
            ROUND(SAFE_DIVIDE(selected.blk - baseline.avg_blk, NULLIF(baseline.avg_blk, 0)) * 100, 1) AS blk_delta_pct,
            ROUND(SAFE_DIVIDE(selected.min - baseline.avg_min, NULLIF(baseline.avg_min, 0)) * 100, 1) AS min_delta_pct,
            ROUND(SAFE_DIVIDE(selected.fg_pct - baseline.avg_fg_pct, NULLIF(baseline.avg_fg_pct, 0)) * 100, 1) AS fg_pct_delta_pct,
            ROUND(SAFE_DIVIDE(selected.ft_pct - baseline.avg_ft_pct, NULLIF(baseline.avg_ft_pct, 0)) * 100, 1) AS ft_pct_delta_pct,
            ROUND(SAFE_DIVIDE(selected.fg3m - baseline.avg_fg3m, NULLIF(baseline.avg_fg3m, 0)) * 100, 1) AS fg3m_delta_pct,
            CASE
              WHEN baseline.sd_pts > 0 THEN SAFE_DIVIDE(selected.pts - baseline.avg_pts, baseline.sd_pts)
              WHEN selected.pts > baseline.avg_pts THEN 1.0
              WHEN selected.pts < baseline.avg_pts THEN -1.0
              ELSE 0.0
            END AS z_pts,
            CASE
              WHEN baseline.sd_reb > 0 THEN SAFE_DIVIDE(selected.reb - baseline.avg_reb, baseline.sd_reb)
              WHEN selected.reb > baseline.avg_reb THEN 1.0
              WHEN selected.reb < baseline.avg_reb THEN -1.0
              ELSE 0.0
            END AS z_reb,
            CASE
              WHEN baseline.sd_ast > 0 THEN SAFE_DIVIDE(selected.ast - baseline.avg_ast, baseline.sd_ast)
              WHEN selected.ast > baseline.avg_ast THEN 1.0
              WHEN selected.ast < baseline.avg_ast THEN -1.0
              ELSE 0.0
            END AS z_ast,
            CASE
              WHEN baseline.sd_stl > 0 THEN SAFE_DIVIDE(selected.stl - baseline.avg_stl, baseline.sd_stl)
              WHEN selected.stl > baseline.avg_stl THEN 1.0
              WHEN selected.stl < baseline.avg_stl THEN -1.0
              ELSE 0.0
            END AS z_stl,
            CASE
              WHEN baseline.sd_blk > 0 THEN SAFE_DIVIDE(selected.blk - baseline.avg_blk, baseline.sd_blk)
              WHEN selected.blk > baseline.avg_blk THEN 1.0
              WHEN selected.blk < baseline.avg_blk THEN -1.0
              ELSE 0.0
            END AS z_blk,
            (
              CASE WHEN selected.pts > baseline.avg_pts THEN 1 ELSE 0 END
              + CASE WHEN selected.reb > baseline.avg_reb THEN 1 ELSE 0 END
              + CASE WHEN selected.ast > baseline.avg_ast THEN 1 ELSE 0 END
              + CASE WHEN selected.stl > baseline.avg_stl THEN 1 ELSE 0 END
              + CASE WHEN selected.blk > baseline.avg_blk THEN 1 ELSE 0 END
            ) AS above_count,
            (
              CASE WHEN selected.pts < baseline.avg_pts THEN 1 ELSE 0 END
              + CASE WHEN selected.reb < baseline.avg_reb THEN 1 ELSE 0 END
              + CASE WHEN selected.ast < baseline.avg_ast THEN 1 ELSE 0 END
              + CASE WHEN selected.stl < baseline.avg_stl THEN 1 ELSE 0 END
              + CASE WHEN selected.blk < baseline.avg_blk THEN 1 ELSE 0 END
            ) AS below_count
          FROM selected
          CROSS JOIN baseline
        ),
        scored AS (
          SELECT
            *,
            ROUND(z_pts + z_reb + z_ast + z_stl + z_blk, 2) AS performance_score
          FROM metric_rows
        )
        SELECT
          *,
          CASE
            WHEN performance_score >= 1.0 OR (performance_score > 0 AND above_count >= 3) THEN 'above'
            WHEN performance_score <= -1.0 OR (performance_score < 0 AND below_count >= 3) THEN 'below'
            ELSE 'near'
          END AS performance_status
        FROM scored
        LIMIT 1
        """
        try:
            rows = self._query(
                sql,
                [
                    bigquery.ScalarQueryParameter("season", "STRING", SUPPORTED_SEASON),
                    bigquery.ScalarQueryParameter("player_id", "INT64", player_id),
                    bigquery.ScalarQueryParameter("game_id", "STRING", game_id),
                ],
            )
        except BQAPIError:
            return None
        if not rows:
            return None
        item = _format_recent_performance_row(rows[0], include_range=True)
        item["trend_30d"] = self._fetch_recent_performance_trend(
            player_id, game_id=game_id
        )
        return item

    def get_metric_leaders(self, metric: str, limit: int = 10) -> list[dict[str, Any]]:
        config = _get_agent_metric_leader_config(metric)
        if config is None:
            raise ValueError(f"Unsupported metric for leaderboard: {metric}")
        column = config["column"]
        percentile_column = config["percentile_column"]
        order = config["order"]
        sql = f"""
        SELECT
          season,
          player_id,
          player_name,
          latest_team_abbr AS team_abbr,
          games_sampled,
          sample_status,
          @metric_key AS metric_key,
          @metric_label AS metric_label,
          ROUND({column}, 2) AS metric_value,
          {percentile_column} AS percentile
        FROM {self._category_profile_table()}
        WHERE season = @season
          AND is_qualified
        ORDER BY {column} {order}, games_sampled DESC, player_name
        LIMIT @limit
        """
        try:
            rows = self._query(
                sql,
                [
                    bigquery.ScalarQueryParameter("season", "STRING", SUPPORTED_SEASON),
                    bigquery.ScalarQueryParameter("metric_key", "STRING", metric),
                    bigquery.ScalarQueryParameter(
                        "metric_label", "STRING", config["label"]
                    ),
                    bigquery.ScalarQueryParameter("limit", "INT64", limit),
                ],
            )
        except BQAPIError:
            legacy_sql = f"""
            SELECT
              season,
              player_id,
              player_name,
              latest_team_abbr AS team_abbr,
              games_sampled,
              CASE
                WHEN games_sampled >= 10 THEN 'ready'
                WHEN games_sampled >= 5 THEN 'limited_sample'
                ELSE 'insufficient_sample'
              END AS sample_status,
              @metric_key AS metric_key,
              @metric_label AS metric_label,
              ROUND({column}, 2) AS metric_value,
              NULL AS percentile
            FROM {self._category_profile_table()}
            WHERE season = @season
              AND games_sampled >= 5
              AND {column} IS NOT NULL
            ORDER BY {column} {order}, games_sampled DESC, player_name
            LIMIT @limit
            """
            try:
                rows = self._query(
                    legacy_sql,
                    [
                        bigquery.ScalarQueryParameter(
                            "season", "STRING", SUPPORTED_SEASON
                        ),
                        bigquery.ScalarQueryParameter("metric_key", "STRING", metric),
                        bigquery.ScalarQueryParameter(
                            "metric_label", "STRING", config["label"]
                        ),
                        bigquery.ScalarQueryParameter("limit", "INT64", limit),
                    ],
                )
            except BQAPIError:
                return []
        return [
            {
                "season": row.get("season"),
                "player_id": _to_int(row.get("player_id")),
                "player_name": row.get("player_name"),
                "team_abbr": row.get("team_abbr"),
                "games_sampled": _to_int(row.get("games_sampled")),
                "sample_status": row.get("sample_status"),
                "metric_key": row.get("metric_key"),
                "metric_label": row.get("metric_label"),
                "metric_value": _to_float(row.get("metric_value")),
                "percentile": _to_float(row.get("percentile")),
            }
            for row in rows
        ]

    def get_player_metric_percentile(
        self, player_id: int, metric: str, min_games: int = 5
    ) -> dict[str, Any] | None:
        config = _get_agent_metric_leader_config(metric)
        if config is None:
            raise ValueError(f"Unsupported metric for percentile: {metric}")
        column = config["column"]
        rank_order = "ASC" if config["order"] == "ASC" else "DESC"
        percentile_order = "DESC" if config["order"] == "ASC" else "ASC"
        min_games = max(1, min(200, int(min_games)))
        sql = f"""
        WITH player_rows AS (
          SELECT
            season,
            player_id,
            player_name,
            latest_team_abbr AS team_abbr,
            games_sampled,
            ROUND({column}, 2) AS metric_value
          FROM {self._category_profile_table()}
          WHERE season = @season
            AND {column} IS NOT NULL
        ),
        cohort AS (
          SELECT *
          FROM player_rows
          WHERE games_sampled >= @min_games
        ),
        ranked AS (
          SELECT
            *,
            RANK() OVER (ORDER BY metric_value {rank_order}, player_name) AS cohort_rank,
            ROUND(
              CASE
                WHEN COUNT(*) OVER () <= 1 THEN 100
                ELSE PERCENT_RANK() OVER (ORDER BY metric_value {percentile_order}) * 100
              END,
              1
            ) AS percentile
          FROM cohort
        ),
        all_summary AS (
          SELECT
            COUNT(*) AS player_count,
            MAX(games_sampled) AS max_games_sampled
          FROM player_rows
        ),
        cohort_summary AS (
          SELECT
            COUNT(*) AS cohort_size,
            ROUND(AVG(metric_value), 2) AS cohort_avg
          FROM cohort
        )
        SELECT
          @metric_key AS metric_key,
          @metric_label AS metric_label,
          @min_games AS min_games,
          p.season,
          p.player_id,
          p.player_name,
          p.team_abbr,
          p.games_sampled,
          p.metric_value,
          r.cohort_rank,
          r.percentile,
          cs.cohort_size,
          cs.cohort_avg,
          s.player_count,
          s.max_games_sampled
        FROM all_summary s
        CROSS JOIN cohort_summary cs
        LEFT JOIN player_rows p
          ON p.player_id = @player_id
        LEFT JOIN ranked r
          ON r.player_id = p.player_id
        LIMIT 1
        """
        try:
            rows = self._query(
                sql,
                [
                    bigquery.ScalarQueryParameter("season", "STRING", SUPPORTED_SEASON),
                    bigquery.ScalarQueryParameter("player_id", "INT64", player_id),
                    bigquery.ScalarQueryParameter("metric_key", "STRING", metric),
                    bigquery.ScalarQueryParameter(
                        "metric_label", "STRING", config["label"]
                    ),
                    bigquery.ScalarQueryParameter("min_games", "INT64", min_games),
                ],
            )
        except BQAPIError:
            return None
        if not rows:
            return None
        row = rows[0]
        if row.get("player_id") is None:
            return None
        return {
            "season": row.get("season"),
            "player_id": _to_int(row.get("player_id")),
            "player_name": row.get("player_name"),
            "team_abbr": row.get("team_abbr"),
            "games_sampled": _to_int(row.get("games_sampled")),
            "metric_key": row.get("metric_key"),
            "metric_label": row.get("metric_label"),
            "metric_value": _to_float(row.get("metric_value")),
            "min_games": _to_int(row.get("min_games")),
            "cohort_rank": _to_int(row.get("cohort_rank")),
            "percentile": _to_float(row.get("percentile")),
            "cohort_size": _to_int(row.get("cohort_size")),
            "cohort_avg": _to_float(row.get("cohort_avg")),
            "player_count": _to_int(row.get("player_count")),
            "max_games_sampled": _to_int(row.get("max_games_sampled")),
            "in_requested_cohort": row.get("cohort_rank") is not None,
        }

    def get_health(self) -> dict[str, Any]:
        latest_run = self.get_latest_successful_run()
        payload = build_freshness_payload(
            latest_run,
            now=datetime.now(tz=UTC),
            freshness_threshold_hours=self.settings.freshness_threshold_hours,
        )
        payload["season"] = SUPPORTED_SEASON
        coverage = self.get_season_coverage()
        if coverage is not None:
            payload["season_coverage"] = coverage
        return payload
