from __future__ import annotations

from typing import Any

from google.api_core.exceptions import GoogleAPIError as BQAPIError
from google.cloud import bigquery

from app.agent.execution import (
    bounded_timeout,
    record_data_read,
    serialized_warehouse_read,
    warehouse_budget,
    warehouse_bytes_billed,
)
from app.agent.source_policy import (
    denied,
    get_source_policy,
    identity_sql,
)
from app.repository._bq.base import BigQueryBaseMixin
from app.repository._helpers import (
    _get_agent_metric_leader_config,
    _to_float,
    _to_int,
)


class BigQueryAskMixin(BigQueryBaseMixin):
    @serialized_warehouse_read
    def _scoped_identity_rows(
        self, player_ids: list[int], *, season: str | None = None
    ) -> list[dict[str, Any]]:
        """A fixed, ID-bounded query; never enters legacy lookup fallbacks."""
        from app.agent.semantics import SemanticError

        policy = get_source_policy(self.source_policy)
        if policy is None:
            denied("Scoped identity requires a trusted source policy")
        # Identity follows the explicit trusted scope, independently of the UI shell.
        if season is None:
            season = (
                policy.relations[0][0]
                if len(policy.relations) == 1
                else self.settings.season
            )
        policy.check_scope([season], policy.phases)
        if (
            not player_ids
            or any(type(pid) is not int for pid in player_ids)
            or not set(player_ids) <= policy.allowed_player_ids
        ):
            denied("Identity is outside the approved source scope")
        import app.repository._bigquery as _bq_export

        sql = getattr(_bq_export, "identity_sql", identity_sql)(policy.relation(season))
        params = [
            bigquery.ScalarQueryParameter(
                "snapshot_at", "TIMESTAMP", policy.snapshot_at
            ),
            bigquery.ScalarQueryParameter("season", "STRING", season),
            bigquery.ArrayQueryParameter("player_ids", "INT64", sorted(player_ids)),
            bigquery.ScalarQueryParameter(
                "row_limit", "INT64", policy.identity_max_rows + 1
            ),
            bigquery.ArrayQueryParameter("phases", "STRING", list(policy.phases)),
        ]
        config = bigquery.QueryJobConfig(
            query_parameters=params,
            maximum_bytes_billed=warehouse_budget(policy.maximum_bytes_billed),
            use_legacy_sql=False,
            use_query_cache=False,
        )
        policy.authorize(
            sql=sql,
            config=config,
            seasons=[season],
            phases=policy.phases,
            identity_ids=player_ids,
        )
        from app.agent.semantic_serving import SemanticWarehouse

        cached = None
        warehouse = getattr(self, "_governed_warehouse", None)
        if (
            isinstance(warehouse, SemanticWarehouse)
            and getattr(self, "_governed_source_policy", None) == policy
        ):
            cached = warehouse.cached_snapshot([season])
            if cached is not None and (
                cached.get("sources") != [policy.relation(season)]
                or cached.get("source_mode") != policy.source_mode
                or cached.get("source_timestamp") != policy.snapshot_at
            ):
                denied("Cached identity source differs from approved scope")
        assert self.client is not None
        job = None
        rows: list[dict[str, Any]] | None = None
        failure = None
        try:
            if cached is None:
                job = self.client.query(
                    sql,
                    job_config=config,
                    timeout=bounded_timeout(policy.submission_timeout),
                )
                rows = []
                for row in job.result(timeout=bounded_timeout(policy.result_timeout)):
                    if len(rows) >= policy.identity_max_rows:
                        raise SemanticError(
                            "incomplete_evidence", "Scoped identity query was truncated"
                        )
                    rows.append(dict(row))
            else:
                rows = [r for r in cached["rows"] if r["player_id"] in player_ids]
            players: dict[int, dict[str, Any]] = {}
            for row in rows:
                pid = row.get("player_id")
                name = row.get("player_name")
                if (
                    type(pid) is not int
                    or pid not in player_ids
                    or row.get("season") != season
                    or row.get("season_type") not in policy.phases
                    or not isinstance(name, str)
                    or not name.strip()
                ):
                    raise SemanticError(
                        "invalid_evidence", "Identity row differs from approved scope"
                    )
                previous = players.get(pid)
                if previous and previous["player_name"] != name:
                    raise SemanticError(
                        "ambiguous_identity",
                        "Conflicting names for an approved identity",
                    )
                if previous is None:
                    players[pid] = {
                        "player_id": pid,
                        "player_name": name,
                        "latest_season": season,
                        "team_abbr": row.get("team_abbr"),
                        "latest_team_abbr": row.get("team_abbr"),
                    }
                elif previous["team_abbr"] != row.get("team_abbr"):
                    # Multiple observed teams do not establish a latest team.
                    previous["team_abbr"] = previous["latest_team_abbr"] = None
            return list(players.values())
        except Exception as exc:
            failure = exc
            raise
        finally:
            record_data_read(
                kind="evidence_cache" if cached is not None else "warehouse",
                status="error" if failure else "ok",
                rows=len(rows) if rows is not None else None,
                budget_ceiling=config.maximum_bytes_billed,
                bytes_processed=getattr(job, "total_bytes_processed", None),
                bytes_billed=warehouse_bytes_billed(job, failure),
                cache_hit=True
                if cached is not None
                else getattr(job, "cache_hit", None),
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
            bigquery.ScalarQueryParameter("season", "STRING", self.settings.season),
            bigquery.ScalarQueryParameter("query", "STRING", query.strip()),
            bigquery.ScalarQueryParameter("limit", "INT64", limit),
        ]
        return self._query(sql, params)

    def search_players(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        policy = get_source_policy(self.source_policy)
        if policy is not None:
            if (
                not query.strip()
                or type(limit) is not int
                or not 1 <= limit <= policy.identity_max_rows
            ):
                denied("Broad or unbounded identity search is disabled")
            rows = self._scoped_identity_rows(sorted(policy.allowed_player_ids))
            return [
                row
                for row in rows
                if query.strip().casefold() in row["player_name"].casefold()
            ][:limit]
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
        policy = get_source_policy(self.source_policy)
        if policy is not None:
            if type(limit) is not int or limit < len(policy.allowed_player_ids):
                denied("Identity candidate limit would truncate the approved roster")
            return self._scoped_identity_rows(sorted(policy.allowed_player_ids))
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
            bigquery.ScalarQueryParameter("season", "STRING", self.settings.season),
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
                    bigquery.ScalarQueryParameter(
                        "season", "STRING", self.settings.season
                    ),
                    bigquery.ScalarQueryParameter("query", "STRING", query.strip()),
                    bigquery.ScalarQueryParameter("limit", "INT64", limit),
                ],
            )
        except BQAPIError:
            return []

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
                    bigquery.ScalarQueryParameter(
                        "season", "STRING", self.settings.season
                    ),
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
                            "season", "STRING", self.settings.season
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
                    bigquery.ScalarQueryParameter(
                        "season", "STRING", self.settings.season
                    ),
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
