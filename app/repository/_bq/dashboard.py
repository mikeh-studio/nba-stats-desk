from __future__ import annotations

import json
import time
from datetime import date
from typing import Any

from google.api_core.exceptions import GoogleAPIError as BQAPIError
from google.cloud import bigquery

from app.agent.execution import (
    CURRENT,
    bounded_timeout,
    record_data_read,
)
from app.agent.source_policy import (
    denied,
    get_source_policy,
)
from app.repository._bq.base import BigQueryBaseMixin
from app.repository._constants import (
    RECENT_PERFORMANCE_TABLE_FIELDS,
    RECENT_PERFORMANCE_TABLE_ROW_CACHE_TTL_SECONDS,
    RECENT_PERFORMANCE_TABLE_ROW_MAX_RESULTS,
    STATE_UNAVAILABLE,
)
from app.repository._helpers import (
    _format_home_date_label,
    _format_recent_performance_game,
    _format_recent_performance_row,
    _parse_iso_date,
    _to_float,
    _to_int,
    _to_iso,
)


class BigQueryDashboardMixin(BigQueryBaseMixin):
    def _fetch_recent_performance_table_rows_api(self) -> list[dict[str, Any]] | None:
        # Tabledata/metadata APIs bypass _query, and their cache predates policy
        # installation. Authorize before inspecting either cached or remote rows.
        if get_source_policy(self.source_policy) is not None:
            denied(
                "Repository tabledata and metadata reads are disabled by the source policy"
            )
        now = time.monotonic()
        with self._recent_performance_rows_lock:
            cached = self._recent_performance_rows_cache
            if cached is not None:
                cached_at, rows = cached
                if now - cached_at < RECENT_PERFORMANCE_TABLE_ROW_CACHE_TTL_SECONDS:
                    return [dict(row) for row in rows]

        assert self.client is not None
        table_id = self._recent_performance_table_id()
        # tabledata.list returns selected columns in physical schema order.
        # Supply that same order to the decoder, including after dbt rebuilds.
        policy = CURRENT.get()
        table = None
        try:
            table = (
                self.client.get_table(table_id, timeout=bounded_timeout(30))
                if policy
                else self.client.get_table(table_id)
            )
        finally:
            record_data_read(
                kind="warehouse",
                status="ok" if table is not None else "error",
                rows=0,
                bytes_processed=0,
                bytes_billed=0,
            )
        wanted = {field.name for field in RECENT_PERFORMANCE_TABLE_FIELDS}
        selected_fields = [field for field in table.schema if field.name in wanted]
        rows = []
        success = False
        try:
            if policy:
                row_iter = self.client.list_rows(
                    table_id,
                    selected_fields=selected_fields,
                    max_results=RECENT_PERFORMANCE_TABLE_ROW_MAX_RESULTS,
                    timeout=bounded_timeout(30),
                )
            else:
                row_iter = self.client.list_rows(
                    table_id,
                    selected_fields=selected_fields,
                    max_results=RECENT_PERFORMANCE_TABLE_ROW_MAX_RESULTS,
                )
            for row in row_iter:
                if policy:
                    bounded_timeout(30)
                if len(rows) >= RECENT_PERFORMANCE_TABLE_ROW_MAX_RESULTS:
                    return None
                rows.append({key: _to_iso(value) for key, value in dict(row).items()})
            success = True
        finally:
            # Metadata/tabledata reads do not create on-demand SQL query charges.
            record_data_read(
                kind="warehouse",
                status="ok" if success else "error",
                rows=len(rows),
                bytes_processed=0,
                bytes_billed=0,
            )
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

    def _fetch_dashboard_rows(
        self,
        *,
        limit: int,
        order_by: str,
        where_clause: str = "",
        extra_params: list[bigquery.ScalarQueryParameter] | None = None,
    ) -> list[dict[str, Any]]:
        params: list[bigquery.ScalarQueryParameter] = [
            bigquery.ScalarQueryParameter("season", "STRING", self.settings.season),
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
                            "season", "STRING", self.settings.season
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
            bigquery.ScalarQueryParameter("season", "STRING", self.settings.season),
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

    def get_rankings(self, limit: int = 25) -> list[dict[str, Any]]:
        return self._fetch_dashboard_rows(
            limit=limit,
            order_by="overall_rank ASC, recommendation_score DESC, player_name",
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
            "season": self.settings.season,
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
        season_rows = [row for row in rows if row.get("season") == self.settings.season]
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
                "season": self.settings.season,
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
            "season": self.settings.season,
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
            bigquery.ScalarQueryParameter("season", "STRING", self.settings.season),
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
                "season": self.settings.season,
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
            bigquery.ScalarQueryParameter("season", "STRING", self.settings.season),
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
                "season": self.settings.season,
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
                [
                    bigquery.ScalarQueryParameter(
                        "season", "STRING", self.settings.season
                    )
                ],
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
            bigquery.ScalarQueryParameter("season", "STRING", self.settings.season)
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
