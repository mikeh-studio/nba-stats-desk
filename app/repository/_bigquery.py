from __future__ import annotations

from dataclasses import dataclass

from app.agent.source_policy import identity_sql
from app.repository._bq import (
    _PLAYER_QUERY_POOL,
    BigQueryAskMixin,
    BigQueryBaseMixin,
    BigQueryDashboardMixin,
    BigQueryPerformanceMixin,
    BigQueryPlayerMixin,
    BigQuerySimilarityMixin,
    BigQueryTrendingMixin,
    _submit_player_query,
)


@dataclass
class BigQueryWarehouseRepository(
    BigQueryPerformanceMixin,
    BigQueryPlayerMixin,
    BigQueryTrendingMixin,
):
    """BigQuery-backed warehouse repository composed from domain mixins."""


__all__ = [
    "BigQueryWarehouseRepository",
    "BigQueryBaseMixin",
    "BigQueryDashboardMixin",
    "BigQueryPerformanceMixin",
    "BigQueryPlayerMixin",
    "BigQuerySimilarityMixin",
    "BigQueryTrendingMixin",
    "BigQueryAskMixin",
    "_PLAYER_QUERY_POOL",
    "_submit_player_query",
    "identity_sql",
]
