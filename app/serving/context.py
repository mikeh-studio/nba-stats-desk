"""Explicit historical observed-PTS context. No app factory or source selection."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping

from app.serving.contract import (
    ServingError,
    canonical_json,
    content_digest,
    historical_limitations,
)
from app.serving.snapshot import SnapshotHandle

ADAPTER_VERSION = "historical-pts-shadow/1"
AGGREGATE_FIELDS = (
    "value",
    "display_value",
    "numerator",
    "denominator",
    "observed_games",
    "valid_games",
    "missing_component_games",
    "components",
    "game_ids",
)


@dataclass(frozen=True)
class PTSShadowRequest:
    """A deterministic plan: canonical ID and exact scope, never free-form SQL."""

    player_id: int
    aggregation: str
    scope_bytes: bytes
    metric_id: str = "pts"

    @classmethod
    def create(cls, player_id, aggregation, *, scope, metric_id="pts"):
        if not isinstance(scope, Mapping):
            raise ServingError("capability_not_evaluated")
        try:
            scope_bytes = canonical_json(dict(scope))
        except (ValueError, TypeError):
            raise ServingError("capability_not_evaluated") from None
        return cls(player_id, aggregation, scope_bytes, metric_id)


@dataclass(frozen=True)
class ReadContext:
    """Pins content and scope, never an authority lease. Not a runtime boundary."""

    _handle: SnapshotHandle = field(repr=False, compare=False)
    _binding_bytes: bytes = field(repr=False)

    @classmethod
    def bind(cls, handle, *, expected_snapshot_id, scope):
        manifest = handle.describe()
        if manifest["snapshot_id"] != expected_snapshot_id:
            raise ServingError("snapshot_binding_mismatch")
        if not isinstance(scope, Mapping) or dict(scope) != manifest["scope"]:
            raise ServingError("capability_not_evaluated")
        binding = {
            "adapter_version": ADAPTER_VERSION,
            "snapshot_id": manifest["snapshot_id"],
            "projection_version": manifest["projection_version"],
            "metric_semantics_id": manifest["metric_semantics_id"],
            "scope": manifest["scope"],
            "lineage": manifest["lineage"],
            "dependency_ids": manifest["dependency_ids"],
            "capabilities": manifest["capabilities"],
        }
        binding["context_id"] = content_digest(binding)
        return cls(handle, canonical_json(binding))

    @property
    def binding(self) -> dict[str, Any]:
        return json.loads(self._binding_bytes)

    def authorize(self):
        verified = self._handle.authorized_snapshot()
        self._match_manifest(verified["manifest"])
        return verified["authority"]

    def _match_manifest(self, manifest):
        binding = self.binding
        for key in (
            "snapshot_id",
            "projection_version",
            "metric_semantics_id",
            "scope",
            "lineage",
            "dependency_ids",
            "capabilities",
        ):
            if manifest[key] != binding[key]:
                raise ServingError("snapshot_binding_mismatch")

    def observe(self, request: PTSShadowRequest) -> dict[str, Any]:
        if (
            type(request) is not PTSShadowRequest
            or type(request.player_id) is not int
            or not 0 < request.player_id <= 2147483647
            or request.metric_id != "pts"
            or request.aggregation not in ("total", "average")
            or request.scope_bytes != canonical_json(self.binding["scope"])
        ):
            raise ServingError("capability_not_evaluated")
        bundle = self._handle.observed_pts_bundle(
            request.player_id, scope=self.binding["scope"]
        )
        self._match_manifest(bundle["manifest"])
        rows = bundle["rows"]
        if not rows:
            raise ServingError("player_not_observed")
        aggregates = bundle["aggregates"]
        result = {
            "kind": "observed_pts_shadow",
            "binding": self.binding,
            "player_id": request.player_id,
            "aggregation": request.aggregation,
            "metric": {"key": "pts", "unit": "count"},
            "aggregates": {
                mode: {key: report[key] for key in AGGREGATE_FIELDS}
                for mode, report in aggregates.items()
            },
            "games": [
                {k: row[k] for k in ("game_id", "game_date", "team_id", "pts")}
                for row in sorted(rows, key=lambda r: (r["game_date"], r["game_id"]))
            ],
            "evidence_clocks": bundle["manifest"]["clocks"],
            "limitations": historical_limitations(bundle["manifest"]["clocks"]),
            "identity_mode": "canonical_id_only",
            "cohort_complete": False,
            "runtime_eligible": False,
        }
        self.authorize()
        return result

    def check_emission(self, payload):
        """A later still-permitted lease cannot silently replace the recorded status."""
        current = self.authorize()
        recorded = payload["authority"]
        if any(current[k] != recorded[k] for k in ("status_id", "status_checked_at")):
            raise ServingError("authority_changed")
