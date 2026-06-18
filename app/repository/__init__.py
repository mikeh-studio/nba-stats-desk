"""Warehouse data-access layer.

Splits the former single-file ``app/repository`` module into a package while
preserving its public import surface. External code should keep importing from
``app.repository`` directly.
"""

from __future__ import annotations

from app.repository._bigquery import BigQueryWarehouseRepository
from app.repository._constants import (
    SIMILARITY_FEATURE_COLUMNS,
    STATE_FRESH,
    STATE_INSUFFICIENT_SAMPLE,
    STATE_MISSING,
    STATE_STALE,
    STATE_UNAVAILABLE,
    CompareFocus,
    CompareWindow,
)
from app.repository._helpers import (
    _weighted_similarity_vector,
    build_analysis_payload,
    build_freshness_payload,
    build_headshot_url,
    build_player_initials,
    build_reason_summary,
    build_season_coverage_payload,
    get_compare_focus_options,
    get_compare_window_options,
)
from app.repository._protocol import WarehouseRepository

__all__ = [
    "STATE_FRESH",
    "STATE_STALE",
    "STATE_MISSING",
    "STATE_INSUFFICIENT_SAMPLE",
    "STATE_UNAVAILABLE",
    "CompareWindow",
    "CompareFocus",
    "SIMILARITY_FEATURE_COLUMNS",
    "WarehouseRepository",
    "BigQueryWarehouseRepository",
    "get_compare_window_options",
    "get_compare_focus_options",
    "build_reason_summary",
    "build_headshot_url",
    "build_player_initials",
    "build_freshness_payload",
    "build_season_coverage_payload",
    "build_analysis_payload",
    "_weighted_similarity_vector",
]
