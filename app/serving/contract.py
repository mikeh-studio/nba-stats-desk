"""Data-only offline PTS contract; artifact content never supplies authority."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

SCHEMA = "nba_shared_serving_snapshot/1"
PROJECTION_VERSION = "affirmative-played-pts/2"
FRESHNESS_POLICY_ID = "historical-integrity/1"
DEPENDENCIES = (
    "source_inventory_id",
    "identity_id",
    "selection_id",
    "policy_id",
    "coverage_id",
    "final_capability_id",
)


def pts_semantics() -> dict[str, Any]:
    """Return a fresh copy of the reviewed, non-executable PTS definition."""
    return {
        "metric_id": "pts",
        "components": ["pts"],
        "unit": "count",
        "aggregation_rule_id": "selected_pts_sum_or_valid_appearance_average/1",
        "appearance_rule_id": "affirmative_played_canonical_player_game/1",
        "null_policy_id": "missing_component_is_unknown/1",
        "precision_rule_id": "finite_nonnegative_integer_count/1",
        "supported_aggregations": ["total", "average"],
    }


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def content_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


METRIC_SEMANTICS_ID = content_digest(pts_semantics())


class ServingError(ValueError):
    """Bounded reason codes suitable for an offline diagnostic receipt."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class FactReadStatus:
    """Trusted caller input, never loaded from a snapshot or approved by its hash."""

    status_id: str
    checked_at: datetime
    valid_until: datetime
    allowed_snapshot_ids: frozenset[str]
    dependency_states: Mapping[str, str]
    schema: str = "shared-serving-fact-status/1"


@dataclass(frozen=True)
class OfflineReadPolicy:
    authority_provider: Callable[[], FactReadStatus]
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    supported_projection_versions: frozenset[str] = field(
        default_factory=lambda: frozenset({PROJECTION_VERSION})
    )
    supported_metric_semantics_ids: frozenset[str] = field(
        default_factory=lambda: frozenset({METRIC_SEMANTICS_ID})
    )
    max_manifest_bytes: int = 1024 * 1024
    max_shard_bytes: int = 50 * 1024 * 1024
    max_total_bytes: int = 100 * 1024 * 1024
    max_shards: int = 16
    max_rows: int = 100000
    max_status_age: timedelta = timedelta(days=1)

    def __post_init__(self) -> None:
        # Trusted callers may tighten the reviewed wire budgets, not expand them.
        for name, maximum in (
            ("max_manifest_bytes", 1024 * 1024),
            ("max_shard_bytes", 50 * 1024 * 1024),
            ("max_total_bytes", 100 * 1024 * 1024),
            ("max_shards", 16),
            ("max_rows", 100000),
        ):
            value = getattr(self, name)
            if type(value) is not int or not 0 < value <= maximum:
                raise ServingError("invalid_policy")
        if self.max_status_age <= timedelta(0):
            raise ServingError("invalid_policy")
