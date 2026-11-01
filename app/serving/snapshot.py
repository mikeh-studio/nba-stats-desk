"""Bounded immutable PTS reads with renewed external permission on every use."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import stat
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from app.agent.semantics import aggregate, load_contract
from app.serving.contract import (
    DEPENDENCIES,
    FRESHNESS_POLICY_ID,
    METRIC_SEMANTICS_ID,
    PROJECTION_VERSION,
    SCHEMA,
    FactReadStatus,
    OfflineReadPolicy,
    ServingError,
    canonical_json,
    content_digest,
    historical_limitations,
    pts_semantics,
)

_HEX = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,64}\Z")
_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})\Z"
)
_SHARD_PATH = re.compile(r"facts/[0-9]{6}\.json\Z")


def _require(condition: bool, code: str = "invalid_artifact") -> None:
    if not condition:
        raise ServingError(code)


def _keys(obj: Any, names: str) -> None:
    _require(type(obj) is dict and set(obj) == set(names.split()))


def _digest(value: Any) -> bool:
    return type(value) is str and _HEX.fullmatch(value) is not None


def _integer(value: Any, maximum: int) -> bool:
    return type(value) is int and 0 <= value <= maximum


def _day(value: Any) -> date:
    _require(type(value) is str)
    try:
        result = date.fromisoformat(value)
        _require(result.isoformat() == value)
        return result
    except ValueError as exc:
        raise ServingError("invalid_artifact") from exc


def _aware(value: Any, code: str = "invalid_artifact") -> datetime:
    _require(isinstance(value, datetime), code)
    _require(value.tzinfo is not None and value.utcoffset() is not None, code)
    return value.astimezone(timezone.utc)


def _instant(value: Any) -> datetime:
    _require(type(value) is str and _TIMESTAMP.fullmatch(value) is not None)
    try:
        return _aware(datetime.fromisoformat(value))
    except ValueError as exc:
        raise ServingError("invalid_artifact") from exc


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ServingError("invalid_artifact")


def _decode(data: bytes) -> Any:
    try:
        value = json.loads(
            data, object_pairs_hook=_pairs, parse_constant=_reject_constant
        )
        _require(canonical_json(value) == data, "noncanonical_artifact")
        return value
    except (ValueError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, ServingError):
            raise
        raise ServingError("invalid_artifact") from exc


def _root_fd(root: Path) -> int:
    """Walk all components with dirfds: no ancestor or final symlink is followed."""
    _require(root.is_absolute(), "unsafe_path")
    _require(".." not in root.parts, "unsafe_path")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in root.parts[1:]:
            next_fd = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
            )
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read_at(root_fd: int, relative: str, limit: int) -> bytes:
    parent_fd = os.dup(root_fd)
    fd: int | None = None
    try:
        parts = relative.split("/")
        for part in parts[:-1]:
            next_fd = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
            os.close(parent_fd)
            parent_fd = next_fd
        # NONBLOCK ensures a FIFO cannot hang before its file type is rejected.
        fd = os.open(
            parts[-1],
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=parent_fd,
        )
        info = os.fstat(fd)
        _require(stat.S_ISREG(info.st_mode), "unsafe_path")
        _require(0 < info.st_size <= limit, "budget_exceeded")
        chunks = bytearray()
        while len(chunks) <= limit:
            chunk = os.read(fd, min(65536, limit + 1 - len(chunks)))
            if not chunk:
                break
            chunks.extend(chunk)
        _require(len(chunks) <= limit, "budget_exceeded")
        _require(len(chunks) == info.st_size, "integrity_failure")
        return bytes(chunks)
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent_fd)


def _manifest(raw: Any, policy: OfflineReadPolicy) -> dict[str, Any]:
    _keys(
        raw,
        "schema snapshot_id projection_version metric_semantics "
        "metric_semantics_id scope lineage clocks freshness_policy_id "
        "capabilities shards row_count dependency_ids",
    )
    _require(raw["schema"] == SCHEMA, "unsupported_version")
    _require(
        raw["projection_version"] == PROJECTION_VERSION
        and raw["projection_version"] in policy.supported_projection_versions,
        "unsupported_version",
    )
    _require(
        raw["metric_semantics"] == pts_semantics()
        and raw["metric_semantics_id"] == METRIC_SEMANTICS_ID
        and METRIC_SEMANTICS_ID in policy.supported_metric_semantics_ids,
        "semantic_mismatch",
    )
    _require(raw["freshness_policy_id"] == FRESHNESS_POLICY_ID, "unsupported_version")
    _require(_digest(raw["snapshot_id"]))
    content = {key: val for key, val in raw.items() if key != "snapshot_id"}
    _require(content_digest(content) == raw["snapshot_id"], "integrity_failure")
    scope = raw["scope"]
    _keys(scope, "season phase start_date end_date as_of cutoff")
    _require(
        scope["season"] == "2025-26" and scope["phase"] == "Regular Season",
        "capability_not_evaluated",
    )
    start, end = _day(scope["start_date"]), _day(scope["end_date"])
    _require(date(2025, 10, 1) <= start <= end <= date(2026, 6, 30))
    as_of, cutoff = _instant(scope["as_of"]), _instant(scope["cutoff"])
    _require(end <= as_of.date() and as_of <= cutoff)
    _keys(raw["lineage"], " ".join(DEPENDENCIES))
    _require(all(_digest(raw["lineage"][key]) for key in DEPENDENCIES))
    dependencies = raw["dependency_ids"]
    _require(type(dependencies) is list and 1 <= len(dependencies) <= 512)
    _require(all(_digest(value) for value in dependencies))
    _require(dependencies == sorted(set(dependencies)))
    _require(set(raw["lineage"].values()) <= set(dependencies))
    clocks = raw["clocks"]
    _keys(
        clocks,
        "source_captured_at discovered_at revalidated_at reviewed_at hard_expiry",
    )
    for name in ("source_captured_at", "discovered_at", "revalidated_at"):
        if clocks[name] is not None:
            _instant(clocks[name])
    _instant(clocks["reviewed_at"])
    if clocks["hard_expiry"] is not None:
        _require(_instant(clocks["hard_expiry"]) > _instant(clocks["reviewed_at"]))
    expected_caps = {
        "metric_id": "pts",
        "aggregations": ["total", "average"],
        "observed_sample": True,
        "cohort_complete": False,
        "runtime_eligible": False,
    }
    _require(
        canonical_json(raw["capabilities"]) == canonical_json(expected_caps),
        "capability_not_evaluated",
    )
    _require(_integer(raw["row_count"], policy.max_rows) and raw["row_count"] > 0)
    shards = raw["shards"]
    _require(type(shards) is list and 1 <= len(shards) <= policy.max_shards)
    previous = ""
    count, total_bytes = 0, 0
    for shard in shards:
        _keys(shard, "path sha256 byte_count row_count min_date max_date")
        path = shard["path"]
        _require(
            type(path) is str and _SHARD_PATH.fullmatch(path) is not None, "unsafe_path"
        )
        _require(previous < path, "duplicate_or_unordered_shard")
        previous = path
        _require(_digest(shard["sha256"]))
        _require(
            _integer(shard["byte_count"], policy.max_shard_bytes)
            and shard["byte_count"] > 0,
            "budget_exceeded",
        )
        _require(
            _integer(shard["row_count"], policy.max_rows) and shard["row_count"] > 0
        )
        _require(start <= _day(shard["min_date"]) <= _day(shard["max_date"]) <= end)
        count += shard["row_count"]
        total_bytes += shard["byte_count"]
    _require(count == raw["row_count"], "count_mismatch")
    _require(total_bytes <= policy.max_total_bytes, "budget_exceeded")
    return raw


def _rows(raw: Any, manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    _require(type(raw) is list)
    for row in raw:
        _keys(
            row, "season phase game_id player_id team_id game_date played pts minutes"
        )
        _require(
            row["season"] == manifest["scope"]["season"]
            and row["phase"] == manifest["scope"]["phase"],
            "scope_mismatch",
        )
        _require(row["played"] is True, "participation_not_affirmed")
        for name in ("game_id", "team_id"):
            _require(
                type(row[name]) is str and _IDENTIFIER.fullmatch(row[name]) is not None
            )
        _require(_integer(row["player_id"], 2147483647) and row["player_id"] > 0)
        _day(row["game_date"])
        _require(
            manifest["scope"]["start_date"]
            <= row["game_date"]
            <= manifest["scope"]["end_date"],
            "scope_mismatch",
        )
        _require(row["pts"] is None or _integer(row["pts"], 1000000), "invalid_pts")
        minutes = row["minutes"]
        _require(
            minutes is None
            or (
                type(minutes) in (int, float)
                and math.isfinite(minutes)
                and 0 <= minutes <= 1000
            ),
            "invalid_minutes",
        )
    return raw


class SnapshotHandle:
    """Pins identity, never perpetual authority; all returned data is copied."""

    def __init__(self, root: Path, policy: OfflineReadPolicy):
        self._root = root
        self._policy = policy
        self._pinned: bytes | None = None

    @classmethod
    def open(cls, root: Path, policy: OfflineReadPolicy) -> SnapshotHandle:
        handle = cls(root, policy)
        handle._verified()
        return handle

    def _authorize(
        self, manifest: Mapping[str, Any]
    ) -> tuple[FactReadStatus, datetime]:
        try:
            status = self._policy.authority_provider()
            now = _aware(self._policy.clock(), "invalid_clock")
            _require(isinstance(status, FactReadStatus), "authority_unavailable")
            _require(
                status.schema == "shared-serving-fact-status/1"
                and _digest(status.status_id),
                "invalid_status",
            )
            _require(
                type(status.allowed_snapshot_ids) is frozenset
                and all(_digest(value) for value in status.allowed_snapshot_ids),
                "invalid_status",
            )
            _require(
                isinstance(status.dependency_states, Mapping)
                and all(
                    _digest(key) and type(value) is str
                    for key, value in status.dependency_states.items()
                ),
                "invalid_status",
            )
            checked = _aware(status.checked_at, "invalid_status")
            until = _aware(status.valid_until, "invalid_status")
            _require(
                checked <= now < until and now - checked <= self._policy.max_status_age,
                "expired_status",
            )
            _require(
                manifest["snapshot_id"] in status.allowed_snapshot_ids,
                "snapshot_not_allowed",
            )
            for dependency in manifest["dependency_ids"]:
                _require(
                    status.dependency_states.get(dependency)
                    in ("active", "superseded"),
                    "dependency_denied",
                )
            clocks = manifest["clocks"]
            for key in (
                "source_captured_at",
                "discovered_at",
                "revalidated_at",
                "reviewed_at",
            ):
                if clocks[key] is not None:
                    _require(_instant(clocks[key]) <= now, "future_evidence_clock")
            for key in ("as_of", "cutoff"):
                _require(
                    _instant(manifest["scope"][key]) <= now, "future_evidence_clock"
                )
            expiry = clocks["hard_expiry"]
            _require(expiry is None or now < _instant(expiry), "expired_artifact")
            return status, now
        except ServingError:
            raise
        except Exception as exc:
            raise ServingError("authority_unavailable") from exc

    def _verified(
        self,
    ) -> tuple[dict[str, Any], tuple[dict[str, Any], ...], FactReadStatus, datetime]:
        fd: int | None = None
        try:
            fd = _root_fd(self._root)
            data = _read_at(fd, "manifest.json", self._policy.max_manifest_bytes)
            _require(self._pinned is None or data == self._pinned, "integrity_failure")
            manifest = _manifest(_decode(data), self._policy)
            self._authorize(manifest)
            rows: list[dict[str, Any]] = []
            previous: tuple[Any, ...] | None = None
            for shard in manifest["shards"]:
                blob = _read_at(fd, shard["path"], shard["byte_count"])
                _require(
                    len(blob) == shard["byte_count"]
                    and hashlib.sha256(blob).hexdigest() == shard["sha256"],
                    "integrity_failure",
                )
                decoded = _decode(blob)
                _require(
                    type(decoded) is list and len(decoded) == shard["row_count"],
                    "count_mismatch",
                )
                part = _rows(decoded, manifest)
                dates = [row["game_date"] for row in part]
                _require(
                    min(dates) == shard["min_date"] and max(dates) == shard["max_date"],
                    "bound_mismatch",
                )
                for row in part:
                    key = tuple(
                        row[name]
                        for name in ("season", "phase", "game_id", "player_id")
                    )
                    _require(
                        previous is None or previous < key, "duplicate_or_unordered_row"
                    )
                    previous = key
                rows.extend(part)
            _require(len(rows) == manifest["row_count"], "count_mismatch")
            status, authorized_at = self._authorize(manifest)
            self._pinned = data
            return manifest, tuple(rows), status, authorized_at
        except ServingError:
            raise
        except (OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
            raise ServingError("integrity_or_path_failure") from exc
        finally:
            if fd is not None:
                os.close(fd)

    def authorized_snapshot(self) -> dict[str, Any]:
        """One full verification, with the exact permission decision from that pass."""
        manifest, _, status, now = self._verified()
        return {
            "manifest": copy.deepcopy(manifest),
            "authority": {
                "status_id": status.status_id,
                "status_checked_at": status.checked_at.isoformat(),
                "freshness_checked_at": now.isoformat(),
            },
        }

    def authority_receipt(self) -> dict[str, Any]:
        return self.authorized_snapshot()["authority"]

    def observed_pts_bundle(self, player_id: int, *, scope: Mapping[str, Any]):
        """Two PTS aggregates and selected rows from one verified fact image."""
        manifest, rows, _, _ = self._verified()
        _require(_integer(player_id, 2147483647) and player_id > 0)
        _require(dict(scope) == manifest["scope"], "capability_not_evaluated")
        selected = [r for r in rows if r["player_id"] == player_id]
        result = {
            "manifest": copy.deepcopy(manifest),
            "rows": copy.deepcopy(selected),
            "aggregates": {
                mode: _pts_aggregate(selected, mode) for mode in ("total", "average")
            },
        }
        self._authorize(manifest)
        return result

    def describe(self) -> dict[str, Any]:
        manifest, _, _, _ = self._verified()
        return copy.deepcopy(manifest)

    def capabilities(self) -> dict[str, Any]:
        manifest, _, _, _ = self._verified()
        return copy.deepcopy({**manifest["capabilities"], "scope": manifest["scope"]})

    def read(self) -> tuple[dict[str, Any], ...]:
        _, rows, _, _ = self._verified()
        return copy.deepcopy(rows)

    def diagnostic(
        self,
        player_id: int,
        aggregation: str,
        *,
        scope: Mapping[str, Any] | None = None,
        metric_id: str = "pts",
    ) -> dict[str, Any]:
        manifest, rows, _, _ = self._verified()
        _require(_integer(player_id, 2147483647) and player_id > 0)
        try:
            _require(
                metric_id == "pts"
                and aggregation in ("total", "average")
                and (
                    scope is None
                    or isinstance(scope, Mapping)
                    and dict(scope) == manifest["scope"]
                ),
                "capability_not_evaluated",
            )
        except Exception:
            raise ServingError("capability_not_evaluated") from None
        selected = [row for row in rows if row["player_id"] == player_id]
        result = _pts_aggregate(selected, aggregation)
        status, authorized_at = self._authorize(manifest)
        return {
            **result,
            "snapshot_id": manifest["snapshot_id"],
            "metric_semantics_id": manifest["metric_semantics_id"],
            "scope": copy.deepcopy(manifest["scope"]),
            "player_id": player_id,
            "aggregation": aggregation,
            "status_id": status.status_id,
            "status_checked_at": status.checked_at.isoformat(),
            "runtime_eligible": False,
            "cohort_complete": False,
            "freshness_policy_id": FRESHNESS_POLICY_ID,
            "freshness_checked_at": authorized_at.isoformat(),
            "limitations": historical_limitations(manifest["clocks"]),
        }


def _pts_aggregate(rows, aggregation):
    try:
        metric = load_contract().metric("pts")
        _require(
            metric.components == {"pts"}
            and metric.numerator == "pts"
            and metric.denominator is None
            and metric.unit == "count"
            and metric.aggregations == ("total", "average"),
            "semantic_mismatch",
        )
        return aggregate(rows, metric, aggregation)
    except Exception:
        raise ServingError("semantic_mismatch") from None
