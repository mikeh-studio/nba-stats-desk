from __future__ import annotations

from typing import Any

from google.api_core.exceptions import GoogleAPIError as BQAPIError
from google.cloud import bigquery

from app.agent.source_policy import (
    get_source_policy,
)
from app.repository._bq.ask import BigQueryAskMixin
from app.repository._bq.base import _submit_player_query
from app.repository._bq.similarity import BigQuerySimilarityMixin
from app.repository._constants import (
    COMPARE_FOCUS_CONFIG,
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
    _compare_window_expected_games,
    _compare_window_label,
    _default_recent_form,
    _empty_compare_metrics,
    _format_category_profile,
    _format_chart_baselines,
    _format_game_log_row,
    _format_stat_percentiles,
    _format_trend_row,
    _guard_served_archetype_label,
    _guard_served_archetype_summary,
    _has_chart_baselines,
    _opportunity_state_from_row,
    _parse_iso_date,
    _sanitize_category_list,
    _similarity_state_from_sample_status,
    _split_display_list,
    _to_float,
    _to_int,
    _window_reason,
    _window_state,
    build_headshot_url,
    build_player_initials,
    build_reason_summary,
)


class BigQueryPlayerMixin(BigQuerySimilarityMixin, BigQueryAskMixin):
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
            bigquery.ScalarQueryParameter("season", "STRING", self.settings.season),
            bigquery.ScalarQueryParameter("player_id", "INT64", player_id),
        ]
        return self._query(sql, params)

    def _fetch_player_identity(self, player_id: int) -> dict[str, Any] | None:
        if get_source_policy(self.source_policy) is not None:
            rows = self._scoped_identity_rows([player_id])
            return rows[0] if rows else None
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
                    bigquery.ScalarQueryParameter(
                        "season", "STRING", self.settings.season
                    ),
                    bigquery.ScalarQueryParameter("player_id", "INT64", player_id),
                ],
            )
        except BQAPIError:
            return []

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
            bigquery.ScalarQueryParameter("season", "STRING", self.settings.season),
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
            "season": self.settings.season,
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
                    bigquery.ScalarQueryParameter(
                        "season", "STRING", self.settings.season
                    ),
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
                [
                    bigquery.ScalarQueryParameter(
                        "season", "STRING", self.settings.season
                    )
                ],
            )
        except BQAPIError:
            return {}
        return rows[0] if rows else {}

    def _fetch_player_detail_row(self, player_id: int) -> dict[str, Any] | None:
        params = [
            bigquery.ScalarQueryParameter("season", "STRING", self.settings.season),
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
                    bigquery.ScalarQueryParameter(
                        "season", "STRING", self.settings.season
                    ),
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
            original_archetype_label = archetype_row.get("archetype_label")
            guarded_archetype_label = _guard_served_archetype_label(
                original_archetype_label, archetype_row
            )
            archetype_payload = {
                "state": archetype_state,
                "archetype_id": archetype_row.get("archetype_id"),
                "archetype_label": guarded_archetype_label,
                "cluster_confidence": _to_float(
                    archetype_row.get("cluster_confidence")
                ),
                "top_traits": _split_display_list(archetype_row.get("top_traits")),
                "summary": _guard_served_archetype_summary(
                    archetype_row.get("archetype_summary"),
                    original_label=original_archetype_label,
                    guarded_label=guarded_archetype_label,
                ),
                "active_model_key": archetype_row.get("active_model_key"),
                "recommended_model_key": archetype_row.get("recommended_model_key"),
            }

        similarity_models = self._parse_similarity_model_results(
            archetype_row.get("model_results_json") if archetype_row else None,
            archetype_row,
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
                    "season": identity.get("latest_season", self.settings.season),
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

    def get_player_detail(self, player_id: int) -> dict[str, Any] | None:
        identity = self._fetch_player_identity(player_id)
        if identity is None:
            return None

        def fetch_similarity():
            anchor = self._fetch_similarity_anchor(player_id)
            state, reason, players = self._get_similar_players(player_id, anchor=anchor)
            return anchor, state, reason, players

        # Identity gates unknown IDs. Ask runs these on its caller under the
        # shared request budget; ordinary pages retain bounded pool concurrency.
        row_future = _submit_player_query(self._fetch_player_detail_row, player_id)
        game_log_future = _submit_player_query(
            self._fetch_player_game_log_payload, identity, limit=30
        )
        trends_future = _submit_player_query(self._fetch_player_trends, player_id)
        similarity_future = _submit_player_query(fetch_similarity)
        row = row_future.result()
        baseline_fallback = (
            {} if _has_chart_baselines(row) else self._fetch_chart_baseline_row()
        )
        chart_baselines = _format_chart_baselines(row or {}, baseline_fallback)
        game_log = game_log_future.result()
        trends = trends_future.result()
        anchor, similarity_state, similarity_reason, similar_players = (
            similarity_future.result()
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
                bigquery.ScalarQueryParameter("season", "STRING", self.settings.season),
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
            "season": self.settings.season,
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
