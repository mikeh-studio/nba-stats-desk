"""Literal synthetic conformance; no retained source or cohort certification."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from app.serving import FactReadStatus, OfflineReadPolicy
from app.serving.contract import (
    DEPENDENCIES,
    METRIC_SEMANTICS_ID,
    canonical_json,
    content_digest,
    pts_semantics,
)

NOW = datetime(2026, 10, 31, tzinfo=timezone.utc)


def row(game, pts, **extra):
    return {
        "season": "2025-26",
        "phase": "Regular Season",
        "game_id": game,
        "player_id": 1,
        "team_id": "team-a",
        "game_date": "2025-11-01",
        "played": True,
        "pts": pts,
        "minutes": 0,
        **extra,
    }


def write_artifact(root, rows=None, transform=None):
    rows = rows if rows is not None else [row("g1", 0), row("g2", 10), row("g3", None)]
    blob = canonical_json(rows)
    root.mkdir(parents=True, exist_ok=True)
    (root / "facts").mkdir(exist_ok=True)
    (root / "facts/000000.json").write_bytes(blob)
    manifest = {
        "schema": "nba_shared_serving_snapshot/1",
        "projection_version": "affirmative-played-pts/2",
        "metric_semantics": pts_semantics(),
        "metric_semantics_id": METRIC_SEMANTICS_ID,
        "scope": {
            "season": "2025-26",
            "phase": "Regular Season",
            "start_date": "2025-11-01",
            "end_date": "2025-11-01",
            "as_of": "2025-11-01T23:59:59+00:00",
            "cutoff": "2026-10-30T00:00:00+00:00",
        },
        "lineage": {key: content_digest(key) for key in DEPENDENCIES},
        "clocks": {
            "source_captured_at": "2026-10-29T00:00:00+00:00",
            "discovered_at": "2026-10-29T00:00:00+00:00",
            "revalidated_at": "2026-10-30T00:00:00+00:00",
            "reviewed_at": "2026-10-30T00:00:00+00:00",
            "hard_expiry": None,
        },
        "freshness_policy_id": "historical-integrity/1",
        "capabilities": {
            "metric_id": "pts",
            "aggregations": ["total", "average"],
            "observed_sample": True,
            "cohort_complete": False,
            "runtime_eligible": False,
        },
        "shards": [
            {
                "path": "facts/000000.json",
                "sha256": hashlib.sha256(blob).hexdigest(),
                "byte_count": len(blob),
                "row_count": len(rows),
                "min_date": "2025-11-01",
                "max_date": "2025-11-01",
            }
        ],
        "row_count": len(rows),
    }
    manifest["dependency_ids"] = sorted(manifest["lineage"].values())
    if transform:
        transform(manifest)
    manifest["snapshot_id"] = content_digest(manifest)
    (root / "manifest.json").write_bytes(canonical_json(manifest))
    return manifest


@pytest.fixture
def artifact(tmp_path):
    # /var is a macOS symlink. Use the physical temporary root deliberately.
    root = tmp_path.resolve() / "snapshot"
    manifest = write_artifact(root)
    status = FactReadStatus(
        content_digest("status"),
        NOW,
        NOW + timedelta(hours=1),
        frozenset({manifest["snapshot_id"]}),
        {value: "active" for value in manifest["dependency_ids"]},
    )
    live = {"status": status, "now": NOW}
    policy = OfflineReadPolicy(lambda: live["status"], lambda: live["now"])
    return root, manifest, live, policy
