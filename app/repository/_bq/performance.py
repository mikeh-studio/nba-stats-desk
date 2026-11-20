from __future__ import annotations

import json
from typing import Any

from google.api_core.exceptions import GoogleAPIError as BQAPIError
from google.cloud import bigquery

from app.repository._bq.dashboard import BigQueryDashboardMixin
from app.repository._constants import (
    RECENT_PERFORMANCE_STAT_CONFIG,
)
from app.repository._helpers import (
    _format_recent_performance_row,
    _format_recent_performance_trend,
    _merge_recent_performance_detail_summary,
    _parse_iso_date,
    _recent_performance_detail_needs_hydration,
    _recent_performance_trend_needs_hydration,
    _to_int,
)


class BigQueryPerformanceMixin(BigQueryDashboardMixin):
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
            bigquery.ScalarQueryParameter("season", "STRING", self.settings.season),
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
                    bigquery.ScalarQueryParameter(
                        "season", "STRING", self.settings.season
                    ),
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
            if row.get("season") != self.settings.season:
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
                bigquery.ScalarQueryParameter("season", "STRING", self.settings.season),
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
                    bigquery.ScalarQueryParameter(
                        "season", "STRING", self.settings.season
                    ),
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
