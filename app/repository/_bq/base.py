from __future__ import annotations

import json
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import Lock
from typing import TYPE_CHECKING, Any

from google.api_core.exceptions import GoogleAPIError as BQAPIError
from google.cloud import bigquery

from app.agent.execution import (
    ACTIVE_STAGES,
    CURRENT,
    STAGE_CHILDREN,
    bounded_timeout,
    record_data_read,
    serialized_warehouse_read,
    timed,
    warehouse_budget,
    warehouse_bytes_billed,
)
from app.agent.source_policy import (
    SourcePolicy,
    denied,
    get_source_policy,
)
from app.config import Settings
from app.repository._helpers import (
    _build_top_improvement_chips,
    _opportunity_state_from_row,
    _sanitize_category_list,
    _to_bool,
    _to_float,
    _to_int,
    _to_iso,
    _trend_direction,
    build_headshot_url,
    build_player_initials,
    build_reason_summary,
    build_season_coverage_payload,
)
from app.seasons import DEFAULT_SEASON

if TYPE_CHECKING:
    from app.agent.publication_ask_source import PublicationAskSource

# Bound concurrent warehouse work across all player requests in this process.
_PLAYER_QUERY_POOL = ThreadPoolExecutor(
    max_workers=8, thread_name_prefix="player-query"
)


def _submit_player_query(fn, *args, **kwargs):
    if CURRENT.get() is not None:
        future = Future()
        try:
            future.set_result(fn(*args, **kwargs))
        except Exception as exc:
            future.set_exception(exc)
        return future
    context = copy_context()

    def run():
        # Preserve policy/trace/deadline, but detach parallel stage nesting.
        active = ACTIVE_STAGES.set(())
        children = STAGE_CHILDREN.set(None)
        try:
            return fn(*args, **kwargs)
        finally:
            STAGE_CHILDREN.reset(children)
            ACTIVE_STAGES.reset(active)

    return _PLAYER_QUERY_POOL.submit(context.run, run)


@dataclass
class BigQueryBaseMixin:
    settings: Settings
    client: bigquery.Client | None = None
    source_policy: SourcePolicy | None = None
    active_three_point_leaders: Callable[[], dict[str, Any] | None] | None = field(
        default=None, repr=False, compare=False
    )
    ask_publication_source: (
        Callable[[str | None], PublicationAskSource | None] | None
    ) = field(default=None, repr=False, compare=False, init=False)
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

    @timed("repository_retrieval")
    @serialized_warehouse_read
    def _query(
        self, sql: str, params: list[bigquery.ScalarQueryParameter] | None = None
    ) -> list[dict[str, Any]]:
        if get_source_policy(self.source_policy) is not None:
            denied("Ordinary repository SQL is disabled by the source policy")
        job_config = (
            bigquery.QueryJobConfig(query_parameters=params or []) if params else None
        )
        assert self.client is not None
        policy = CURRENT.get()
        if policy:
            job_config = job_config or bigquery.QueryJobConfig()
            job_config.maximum_bytes_billed = warehouse_budget(100_000_000)
        job = None
        failure = None
        rows: list[dict[str, Any]] | None = None
        status = "error"
        timeout = bounded_timeout(30) if policy else None
        try:
            if policy:
                job = self.client.query(sql, job_config=job_config, timeout=timeout)
                result = job.result(timeout=bounded_timeout(120))
            else:
                job = self.client.query(sql, job_config=job_config)
                result = job.result()
            rows = []
            for row in result:
                if policy and len(rows) >= 100000:
                    raise ValueError("Repository query row budget exceeded")
                rows.append({key: _to_iso(value) for key, value in dict(row).items()})
            status = "ok"
            return rows
        except Exception as exc:
            failure = exc
            raise
        finally:
            record_data_read(
                kind="warehouse",
                status=status,
                rows=len(rows) if rows is not None else None,
                budget_ceiling=job_config.maximum_bytes_billed
                if job_config is not None and policy
                else None,
                bytes_processed=getattr(job, "total_bytes_processed", None),
                bytes_billed=warehouse_bytes_billed(job, failure),
                cache_hit=getattr(job, "cache_hit", None),
            )

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

    def get_latest_successful_run(self) -> dict[str, Any] | None:
        table = f"`{self.settings.project_id}.{self.settings.metadata_dataset}.pipeline_run_log`"
        sql = f"""
        SELECT
          season,
          finished_at_utc
        FROM {table}
        WHERE season = @season
          AND status = 'success'
          AND source_system = 'nba_api'
        ORDER BY finished_at_utc DESC
        LIMIT 1
        """
        rows = self._query(
            sql,
            [bigquery.ScalarQueryParameter("season", "STRING", self.settings.season)],
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
                [
                    bigquery.ScalarQueryParameter(
                        "season", "STRING", self.settings.season
                    )
                ],
            )
        except BQAPIError:
            return None
        return build_season_coverage_payload(
            rows[0] if rows else None, season=self.settings.season
        )

    def get_asset_publication_history(self) -> list[dict[str, Any]] | None:
        table = f"`{self.settings.project_id}.{self.settings.metadata_dataset}.pipeline_run_log`"
        # Skipped/no-change runs do not reset the last publication or erase a
        # previous failure. Historical logs remain readable without migration.
        sql = f"""
        WITH runs AS (
          SELECT finished_at_utc, watermark_after, rows_loaded, details
          FROM {table}
          WHERE season = @season AND source_system = 'nba_api' AND status = 'success'
        ), attempts AS (
          SELECT finished_at_utc, asset.* FROM runs,
          UNNEST([
            STRUCT('stats' AS asset,
              COALESCE(REGEXP_EXTRACT(details, r'(?:^|;)stats_status=([^;]*)'),
                IF(rows_loaded > 0 AND REGEXP_CONTAINS(details, r'(?:^|;)dbt_status=success;'),
                   'success', 'no_change')) AS attempt_status,
              watermark_after AS source_date),
            STRUCT('injuries' AS asset,
              COALESCE(REGEXP_EXTRACT(details, r'(?:^|;)injuries_status=([^;]*)'),
                IF(SAFE_CAST(REGEXP_EXTRACT(details, r'(?:^|;)injury_report_rows_loaded=([^;]*)') AS INT64) > 0,
                   'success', 'no_change')) AS attempt_status,
              SAFE_CAST(REGEXP_EXTRACT(details, r'(?:^|;)injuries_source_date=([^;]*)') AS DATE) AS source_date),
            STRUCT('similarity' AS asset,
              REGEXP_EXTRACT(details, r'(?:^|;)similarity_status=([^;]*)') AS attempt_status,
              watermark_after AS source_date)
          ]) AS asset
        )
        SELECT asset,
          ARRAY_AGG(STRUCT(attempt_status, finished_at_utc)
            ORDER BY finished_at_utc DESC LIMIT 1)[OFFSET(0)].attempt_status AS last_attempt_status,
          MAX(finished_at_utc) AS last_attempt_at_utc,
          MAX(IF(attempt_status = 'success', finished_at_utc, NULL)) AS last_successful_finished_at_utc,
          ARRAY_AGG(IF(attempt_status = 'success', STRUCT(source_date), NULL)
            IGNORE NULLS ORDER BY finished_at_utc DESC LIMIT 1)[SAFE_OFFSET(0)].source_date AS latest_source_date
        FROM attempts
        WHERE attempt_status IN ('success', 'failed_non_blocking')
        GROUP BY asset
        """
        try:
            return self._query(
                sql,
                [
                    bigquery.ScalarQueryParameter(
                        "season", "STRING", self.settings.season
                    )
                ],
            )
        except BQAPIError:
            return None

    def get_health(self) -> dict[str, Any]:
        from app.freshness import build_publication_health

        if self.settings.season != DEFAULT_SEASON:
            table = f"`{self.settings.project_id}.{self.settings.metadata_dataset}.historical_backfill_manifest`"
            try:
                rows = self._query(
                    f"SELECT published_at_utc, as_of_date, report_json FROM {table} WHERE season = @season LIMIT 1",
                    [
                        bigquery.ScalarQueryParameter(
                            "season", "STRING", self.settings.season
                        )
                    ],
                )
            except BQAPIError:
                rows = []
            publication = rows[0] if rows else {}
            archive_report = json.loads(publication.get("report_json") or "{}")
            names = archive_report.get("validated_assets", ["stats", "similarity"])
            if "validated_assets" not in archive_report and archive_report.get(
                "row_counts", {}
            ).get("injury_reports", 0):
                names.append("injuries")
            assets = (
                [
                    {
                        "asset": name,
                        "last_successful_finished_at_utc": publication.get(
                            "published_at_utc"
                        ),
                        "latest_source_date": archive_report.get(
                            "injury_coverage", {}
                        ).get("latest_report_date")
                        if name == "injuries"
                        else publication.get("as_of_date"),
                        "last_attempt_status": "success",
                    }
                    for name in names
                ]
                if publication
                else []
            )
            health = build_publication_health(
                None,
                assets,
                self.get_season_coverage(),
                settings=self.settings,
                now=datetime.now(tz=UTC),
            )
            health["data_limitations"] = archive_report.get("limitations", [])
            health["injury_coverage"] = archive_report.get("injury_coverage")
            return health
        return build_publication_health(
            self.get_latest_successful_run(),
            self.get_asset_publication_history(),
            self.get_season_coverage(),
            settings=self.settings,
            now=datetime.now(tz=UTC),
        )
