from __future__ import annotations

from typing import Any

from google.api_core.exceptions import GoogleAPIError as BQAPIError
from google.cloud import bigquery

from app.repository._bq.base import BigQueryBaseMixin
from app.what_changed import ComparisonPeriod, SeasonPhase


class BigQueryTrendingMixin(BigQueryBaseMixin):
    def get_what_changed(
        self,
        *,
        period: ComparisonPeriod = "four_games",
        season_type: SeasonPhase = "Regular Season",
        as_of: str | None = None,
    ) -> dict[str, Any]:
        from app.what_changed import WhatChangedUnavailable, build_what_changed

        if period not in {"four_games", "week"} or season_type not in {
            "Regular Season",
            "Playoffs",
        }:
            raise ValueError("Unsupported comparison window or season type")
        params = [
            bigquery.ScalarQueryParameter("season", "STRING", self.settings.season),
            bigquery.ScalarQueryParameter("season_type", "STRING", season_type),
            bigquery.ScalarQueryParameter("as_of", "DATE", as_of),
        ]
        sql = f"""
        WITH anchor AS (
          SELECT MAX(game_date) AS end_date FROM {self._fct_game_stats_table()}
          WHERE season = @season AND season_type = @season_type
            AND (@as_of IS NULL OR game_date <= @as_of)
        )
        SELECT stats.game_id, stats.game_date, stats.season, stats.season_type,
          stats.player_id, stats.player_name, stats.team_abbr, stats.opponent_abbr,
          stats.min, stats.pts, stats.reb, stats.ast, stats.stl, stats.blk, stats.tov,
          stats.fgm, stats.fga, stats.ftm, stats.fta, stats.fg3m, stats.fg3a,
          SAFE_CAST(JSON_VALUE(TO_JSON_STRING(stats), '$.pf') AS FLOAT64) AS pf,
          SAFE_CAST(JSON_VALUE(TO_JSON_STRING(stats), '$.oreb') AS FLOAT64) AS oreb,
          SAFE_CAST(JSON_VALUE(TO_JSON_STRING(stats), '$.dreb') AS FLOAT64) AS dreb,
          stats.ingested_at_utc
        FROM {self._fct_game_stats_table()} stats CROSS JOIN anchor
        WHERE stats.season = @season AND stats.season_type = @season_type
          AND stats.game_date BETWEEN DATE_SUB(anchor.end_date, INTERVAL 60 DAY) AND anchor.end_date
        ORDER BY stats.game_date DESC, stats.player_id, stats.game_id
        LIMIT 20001
        """
        try:
            rows = self._query(sql, params)
        except BQAPIError as exc:
            raise WhatChangedUnavailable("Game statistics are unavailable") from exc
        if len(rows) > 20000:
            raise WhatChangedUnavailable(
                "Source exceeds the complete-ranking safety limit"
            )
        injury_rows = None
        if rows:
            # The optional historical injury table may not be built yet. Missing
            # evidence must not become a healthy status or an inferred DNP-CD.
            dates = [str(row["game_date"])[:10] for row in rows]
            injury_table = f"`{self.settings.project_id}.{self.settings.gold_dataset}.what_changed_injury_reports`"
            try:
                injury_rows = self._query(
                    f"""SELECT player_id, team_abbr, game_date, report_date,
                        report_timestamp_utc, injury_status, reason, source_url
                    FROM {injury_table}
                    WHERE season = @season AND game_date BETWEEN @start_date AND @end_date
                      AND report_date <= @end_date AND report_date <= game_date
                    QUALIFY ROW_NUMBER() OVER (
                      PARTITION BY player_id, team_abbr, game_date
                      ORDER BY report_timestamp_utc DESC
                    ) = 1
                    """,
                    [
                        params[0],
                        bigquery.ScalarQueryParameter("start_date", "DATE", min(dates)),
                        bigquery.ScalarQueryParameter("end_date", "DATE", max(dates)),
                    ],
                )
            except BQAPIError:
                pass
        return build_what_changed(
            rows,
            injury_rows,
            period=period,
            season_type=season_type,
            as_of=as_of,
        )
