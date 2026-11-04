"""Read the latest successful local weekly PTS build in the normal app.

The configured local build directory is the trust boundary. Status means the
published artifact was checked locally; it is not an independent upstream check.
"""

from __future__ import annotations

import json
import os
import re
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.agent.snapshot_source import CurrentPTSAskShadow
from app.serving.comparison import CurrentPTSComparisonShadow, PTSComparisonRequest
from app.serving.context import PTSShadowRequest
from app.serving.contract import FactReadStatus, ServingError, content_digest
from app.serving.current import (
    CurrentReadContext,
    CurrentReadPolicy,
    CurrentSnapshotHandle,
)
from app.serving.player import CurrentPlayerPTSShadow


def _read(path, limit):
    for component in (path, *path.parents):
        if component.is_symlink():
            raise ValueError("symlink in snapshot path")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o022
            or info.st_size > limit
        ):
            raise ValueError("unsafe snapshot file")
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("snapshot file too large")

    def unique(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    return json.loads(
        raw,
        object_pairs_hook=unique,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
    )


class LocalWeeklyPTS:
    """Trust a successful local builder record; preserve all source expiry checks."""

    def __init__(self, data_root, *, clock=None):
        self.root = Path(data_root)
        if not self.root.is_absolute() or ".." in self.root.parts:
            raise ValueError("absolute snapshot root required")
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _context(self):
        base = self.root / "reports/weekly-snapshots"
        try:
            selection = _read(base / "app-selection.json", 16384)
            if (
                type(selection) is not dict
                or set(selection)
                != {"schema", "snapshot_id", "run_id", "runtime_eligible"}
                or selection["schema"] != "offline-weekly-app-selection/1"
                or selection["runtime_eligible"] is not False
                or not isinstance(selection["snapshot_id"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", selection["snapshot_id"])
                or not isinstance(selection["run_id"], str)
                or not re.fullmatch(r"[0-9a-f]{32}", selection["run_id"])
            ):
                raise ValueError("invalid selection")
            sid = selection["snapshot_id"]
            result = _read(
                base / "runs" / (selection["run_id"] + ".result.json"), 1024 * 1024
            )
            receipt = result["producer_receipt"]
            if (
                result.get("schema") != "weekly-snapshot-result/1"
                or result.get("status") != "succeeded"
                or result.get("run_id") != selection["run_id"]
                or result.get("snapshot_id") != sid
                or result.get("runtime_eligible") is not False
                or receipt.get("snapshot_id") != sid
                or receipt.get("runtime_eligible") is not False
                or receipt.get("source_identity_kind") != "reviewed_source_identity"
                or receipt.get("receipt_id")
                != content_digest(
                    {k: v for k, v in receipt.items() if k != "receipt_id"}
                )
            ):
                raise ValueError("successful reviewed build required")
            manifest = _read(base / "snapshots" / sid / "manifest.json", 1024 * 1024)
            if (
                manifest["snapshot_id"] != sid
                or manifest["capabilities"]["runtime_eligible"] is not False
                or manifest["current_evidence"]["policy_fingerprint"]
                != receipt["policy_fingerprint"]
            ):
                raise ValueError("build and snapshot differ")
            expiry = datetime.fromisoformat(manifest["clocks"]["hard_expiry"])
            if expiry.tzinfo is None:
                raise ValueError("expiry needs timezone")
            record_id = content_digest(result)
            dependencies = manifest["dependency_ids"]
            checked_at = self.clock()

            def local_status():
                if _read(base / "app-selection.json", 16384) != selection:
                    raise ServingError("weekly_selection_changed")
                if (
                    content_digest(
                        _read(
                            base / "runs" / (selection["run_id"] + ".result.json"),
                            1024 * 1024,
                        )
                    )
                    != record_id
                ):
                    raise ServingError("weekly_selection_changed")
                return FactReadStatus(
                    record_id,
                    checked_at,
                    min(checked_at + timedelta(hours=1), expiry),
                    frozenset({sid}),
                    {key: "active" for key in dependencies},
                )

            policy = CurrentReadPolicy(
                local_status,
                clock=self.clock,
                freshness_policy_fingerprint=receipt["policy_fingerprint"],
                source_identity_authority_id=manifest["lineage"][
                    "source_identity_authority_id"
                ],
            )
            handle = CurrentSnapshotHandle.open(base / "snapshots" / sid, policy)
            return CurrentReadContext.bind(
                handle, expected_snapshot_id=sid, scope=manifest["scope"]
            )
        except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
            raise ServingError("weekly_snapshot_unavailable") from exc

    def player(self, player_id, aggregation="average"):
        context = self._context()
        request = PTSShadowRequest.create(
            player_id, aggregation, scope=context.binding["scope"]
        )
        return CurrentPlayerPTSShadow(context).get_pts(request)

    def ask(self, player_id, aggregation="average"):
        context = self._context()
        request = PTSShadowRequest.create(
            player_id, aggregation, scope=context.binding["scope"]
        )
        return CurrentPTSAskShadow(context).answer(request)

    def compare(self, player_ids, aggregation="average"):
        context = self._context()
        request = PTSComparisonRequest.create(
            player_ids, aggregation, scope=context.binding["scope"]
        )
        return CurrentPTSComparisonShadow(context).answer(request)

    def check_payload(self, payload):
        """Recheck the exact selection and expiry before sending a response."""
        context = self._context()
        if payload.get("binding") != context.binding:
            raise ServingError("weekly_selection_changed")
        context.authorize()
