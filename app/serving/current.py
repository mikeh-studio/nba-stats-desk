"""Explicit current-season offline contract; no source discovery or runtime registration."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import ClassVar
from zoneinfo import ZoneInfo

from app.serving.context import ReadContext
from app.serving.contract import OfflineReadPolicy, content_digest
from app.serving.snapshot import (
    SnapshotHandle,
    _day,
    _digest,
    _instant,
    _keys,
    _manifest,
    _require,
)

SCHEMA = "nba_current_pts_snapshot/1"
ADAPTER_VERSION = "current-pts-shadow/1"
FRESHNESS_POLICY_ID = "current-espn-weekly/1"
DEPENDENCIES = (
    "source_inventory_id",
    "identity_id",
    "selection_id",
    "policy_id",
    "coverage_id",
    "freshness_evidence_id",
    "source_identity_authority_id",
    "conflict_history_id",
)


@dataclass(frozen=True, kw_only=True)
class CurrentReadPolicy(OfflineReadPolicy):
    """Fingerprints are independently supplied by trusted offline composition."""

    freshness_policy_fingerprint: str
    source_identity_authority_id: str

    def __post_init__(self):
        super().__post_init__()
        _require(_digest(self.freshness_policy_fingerprint), "invalid_policy")
        _require(_digest(self.source_identity_authority_id), "invalid_policy")


def current_limitations(clocks):
    return [
        "observed_sample_only",
        "current_season_offline_only",
        "pdf_corroboration_optional",
        "cohort_not_certified",
        "conflict_history_relative_to_pinned_ledger",
    ]


def validate_current_evidence(manifest, policy):
    e = manifest["current_evidence"]
    _keys(
        e,
        "schema policy_fingerprint provider checked_at source_captured_at "
        "discovered_at revalidated_at warning_after fresh_through first_stale_at "
        "reconciliation_id expected_inventory_id observation_ids_sha256 "
        "conflict_history_completeness pdf_corroboration",
    )
    _require(e["schema"] == "current-pts-freshness-evidence/1")
    _require(e["provider"] == "espn", "source_not_supported")
    _require(
        e["policy_fingerprint"] == policy.freshness_policy_fingerprint,
        "freshness_policy_mismatch",
    )
    _require(
        manifest["lineage"]["source_identity_authority_id"]
        == policy.source_identity_authority_id,
        "source_identity_not_authorized",
    )
    _require(
        content_digest(e) == manifest["lineage"]["freshness_evidence_id"],
        "integrity_failure",
    )
    for key in ("reconciliation_id", "expected_inventory_id", "observation_ids_sha256"):
        _require(_digest(e[key]))
    _require(
        {e["reconciliation_id"], e["expected_inventory_id"]}
        <= set(manifest["dependency_ids"])
    )
    _require(e["conflict_history_completeness"] == "caller_pinned_ledger_only")
    _require(e["pdf_corroboration"] == "not_required_not_certified")
    checked, fresh, stale = (
        _instant(e[k]) for k in ("checked_at", "fresh_through", "first_stale_at")
    )
    _require(checked <= fresh and stale == fresh + timedelta(microseconds=1))
    discovery = _instant(e["discovered_at"])
    source = _instant(e["source_captured_at"])
    _require(source <= checked and discovery <= checked)
    _require(_instant(e["warning_after"]) == discovery + timedelta(days=8))
    # Upper bounds only: correction milestones can require an earlier expiry.
    end = datetime.combine(
        _day(manifest["scope"]["end_date"]) + timedelta(days=1),
        datetime.min.time(),
        ZoneInfo("America/New_York"),
    )
    _require(
        fresh
        <= min(
            discovery + timedelta(days=10),
            end.astimezone(timezone.utc) + timedelta(days=10),
        )
    )
    _require(
        manifest["clocks"]
        == {
            "source_captured_at": e["source_captured_at"],
            "discovered_at": e["discovered_at"],
            "revalidated_at": e["revalidated_at"],
            "reviewed_at": e["checked_at"],
            "hard_expiry": e["first_stale_at"],
        }
    )
    _require(e["checked_at"] == manifest["scope"]["as_of"])
    if e["revalidated_at"] is not None:
        _require(_instant(e["revalidated_at"]) <= checked)


class CurrentSnapshotHandle(SnapshotHandle):
    """An explicit reader; the historical reader continues to refuse this schema."""

    def _parse_manifest(self, raw):
        _require(isinstance(self._policy, CurrentReadPolicy), "invalid_policy")
        manifest = _manifest(
            raw,
            self._policy,
            schema=SCHEMA,
            season="2026-27",
            freshness_policy_id=FRESHNESS_POLICY_ID,
            dependencies=DEPENDENCIES,
            extra_keys="current_evidence",
        )
        validate_current_evidence(manifest, self._policy)
        return manifest

    def limitations(self, manifest):
        return current_limitations(manifest["clocks"])

    def diagnostic(self, *args, **kwargs):
        result = super().diagnostic(*args, **kwargs)
        e = self.describe()["current_evidence"]
        result["discovery_age_warning"] = _instant(
            result["freshness_checked_at"]
        ) > _instant(e["warning_after"])
        return result


class CurrentReadContext(ReadContext):
    ADAPTER: ClassVar[str] = ADAPTER_VERSION
    SNAPSHOT_SCHEMA: ClassVar[str] = SCHEMA

    @classmethod
    def bind(cls, handle, **kwargs):
        _require(type(handle) is CurrentSnapshotHandle, "invalid_policy")
        return super().bind(handle, **kwargs)

    def observe(self, request):
        evidence = super().observe(request)
        manifest = self._handle.describe()
        self._match_manifest(manifest)
        evidence["current_evidence"] = copy.deepcopy(manifest["current_evidence"])
        self.authorize()
        return evidence
