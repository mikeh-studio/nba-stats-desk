"""Keep optional injury failures visible without blocking valid game statistics."""

from __future__ import annotations

import logging
from functools import wraps
from typing import Any, Callable

from season_config import CURRENT_SEASON

logger = logging.getLogger("nba_pipeline")


def optional_injury_stage(function: Callable) -> Callable:
    @wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> dict:
        previous = args[0] if args else next(iter(kwargs.values()), {})
        previous = previous if isinstance(previous, dict) else {}
        if previous.get("asset_status") == "failed_non_blocking":
            return previous
        try:
            return function(*args, **kwargs)
        except Exception as exc:
            from airflow.operators.python import get_current_context

            # Preserve the configured Airflow retries; soften only the final one.
            ti = get_current_context()["ti"]
            if ti.try_number <= ti.max_tries:
                raise
            logger.exception("Injury stage failed; retaining previous data")
            return {
                **previous,
                "domain": "injury_reports",
                "season": previous.get("season", CURRENT_SEASON),
                "row_count": 0,
                "rows_loaded": 0,
                "rows_inserted": 0,
                "rows_updated": 0,
                "candidate_count": 0,
                "gcs_uri": "",
                "watermark_after": previous.get("watermark_before"),
                "asset_status": "failed_non_blocking",
                "asset_error": f"{function.__name__}: {type(exc).__name__}",
            }

    return wrapped


def publication_details(result: dict) -> str:
    """Machine-readable fields in the existing run log; no schema migration."""
    stats = (
        "success"
        if result.get("dbt_status") == "success"
        and result.get("core_warehouse_changed", False)
        else "no_change"
    )
    injury = result.get("injury_status", "no_change")
    if injury != "failed_non_blocking":
        injury = (
            "success"
            if result.get("injury_report_rows_loaded", 0) > 0
            and result.get("injury_dbt_status") == "success"
            else "no_change"
        )
    statuses = [
        injury,
        result.get("similarity_status"),
    ]
    overall = "partially_updated" if "failed_non_blocking" in statuses else "success"
    return (
        f"publication_status={overall};"
        f"stats_status={stats};"
        f"stats_source_date={result.get('watermark_after') or ''};"
        f"injuries_status={injury};"
        f"injuries_source_date={result.get('injury_watermark_after') or ''};"
        f"similarity_source_date={result.get('watermark_after') or ''};"
    )
