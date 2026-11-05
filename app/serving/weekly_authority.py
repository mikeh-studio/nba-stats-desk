"""Read explicit, locally administered authority for the weekly PTS candidate.

This file is independent of snapshot contents. Whoever controls it grants read
authority; the producer must not create approvals merely by producing a snapshot.
"""

from __future__ import annotations

import json
import os
import stat
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.serving.candidate_service import CandidateBinding
from app.serving.contract import (
    FactReadStatus,
    OfflineReadPolicy,
    ServingError,
    content_digest,
)
from app.serving.current import CurrentReadPolicy

MAX_AUTHORITY_BYTES = 256 * 1024


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate authority key")
        result[key] = value
    return result


def _read_owner_json(path, *, with_digest=False):
    import hashlib

    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("authority path must be absolute")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o022
            or info.st_size > MAX_AUTHORITY_BYTES
        ):
            raise ValueError("unsafe authority file")
        raw = stream.read(MAX_AUTHORITY_BYTES + 1)
        if len(raw) > MAX_AUTHORITY_BYTES:
            raise ValueError("authority too large")
    value = json.loads(
        raw,
        object_pairs_hook=_pairs,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
    )
    return (value, hashlib.sha256(raw).hexdigest()) if with_digest else value


def _check_registry(value):
    reference = value.get("registry")
    if reference is None:
        return
    if type(reference) is not dict or set(reference) != {
        "path",
        "policy_path",
        "policy_sha256",
    }:
        raise ValueError("invalid registry reference")
    _, policy_hash = _read_owner_json(reference["policy_path"], with_digest=True)
    if policy_hash != reference["policy_sha256"]:
        raise ValueError("local policy changed")
    registry = _read_owner_json(reference["path"])
    fields = {
        "schema",
        "policy_sha256",
        "enabled",
        "revision",
        "revoked_snapshot_ids",
        "revoked_dependency_ids",
        "revoked_code_ids",
    }
    if (
        type(registry) is not dict
        or set(registry) != fields
        or registry["schema"] != "weekly-local-revocations/1"
        or registry["policy_sha256"] != reference["policy_sha256"]
        or registry["enabled"] is not True
        or type(registry["revision"]) is not int
        or type(value["binding"]["status_revision"]) is not int
        or registry["revision"] < value["binding"]["status_revision"]
    ):
        raise ValueError("local registry unavailable")
    for key in ("revoked_snapshot_ids", "revoked_dependency_ids", "revoked_code_ids"):
        if type(registry[key]) is not list or any(
            type(item) is not str for item in registry[key]
        ):
            raise ValueError("invalid local revocations")
    historical = value["historical"]
    historical_dependencies = (
        set(historical["status"]["dependency_states"])
        if historical is not None
        else set()
    )
    if (
        set(value["binding"]["snapshot_ids"]) & set(registry["revoked_snapshot_ids"])
        or value["binding"]["code_id"] in registry["revoked_code_ids"]
        or (set(value["current_status"]["dependency_states"]) | historical_dependencies)
        & set(registry["revoked_dependency_ids"])
    ):
        raise ValueError("local authority revoked")


def _status(value):
    keys = {
        "status_id",
        "checked_at",
        "valid_until",
        "allowed_snapshot_ids",
        "dependency_states",
    }
    if type(value) is not dict or set(value) != keys:
        raise ValueError("invalid status")
    if (
        type(value["allowed_snapshot_ids"]) is not list
        or type(value["dependency_states"]) is not dict
    ):
        raise ValueError("invalid status")
    return FactReadStatus(
        status_id=value["status_id"],
        checked_at=datetime.fromisoformat(value["checked_at"]),
        valid_until=datetime.fromisoformat(value["valid_until"]),
        allowed_snapshot_ids=frozenset(value["allowed_snapshot_ids"]),
        dependency_states=value["dependency_states"],
    )


def _context_digest(value):
    historical = value["historical"]
    if historical is not None:
        if type(historical) is not dict:
            raise ValueError("invalid historical authority")
        historical = {k: v for k, v in historical.items() if k != "status"}
    return content_digest(
        {
            "current_policy": value["current_policy"],
            "players": value["players"],
            "historical": historical,
            "registry": value.get("registry"),
        }
    )


class WeeklyAuthority:
    """Read a trusted local authority document afresh for every authorization."""

    def __init__(self, path, *, clock=None, expected_code_id=None):
        self.path = Path(path)
        if not self.path.is_absolute() or ".." in self.path.parts:
            raise ValueError("authority path must be absolute")
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.expected_code_id = expected_code_id
        self._lock = threading.Lock()
        self._last_binding = None
        self._last_context_digest = None

    def read(self):
        try:
            value = _read_owner_json(self.path)
            expected = {
                "schema",
                "binding",
                "current_status",
                "current_policy",
                "players",
                "historical",
            }
            if (
                type(value) is not dict
                or not expected <= set(value) <= expected | {"registry"}
                or value["schema"] != "weekly-app-authority/1"
            ):
                raise ValueError("invalid authority")
            _check_registry(value)
            return value
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            UnicodeError,
            RecursionError,
        ) as exc:
            raise ServingError("weekly_authority_unavailable") from exc

    def binding(self, expected_context=None):
        try:
            value = self.read()
            binding = CandidateBinding.from_mapping(value["binding"])
            if (
                self.expected_code_id is not None
                and binding.code_id != self.expected_code_id
            ):
                raise ValueError("application source differs from approved code")
            context_digest = _context_digest(value)
            if expected_context is not None and context_digest != expected_context:
                raise ValueError("authority context changed")
            with self._lock:
                if (
                    binding == self._last_binding
                    and context_digest != self._last_context_digest
                ):
                    raise ValueError("policy change requires new binding")
                self._last_binding = binding
                self._last_context_digest = context_digest
            return binding
        except (ValueError, TypeError, KeyError) as exc:
            raise ServingError("weekly_authority_unavailable") from exc

    def _status(self, field, expected_context):
        try:
            value = self.read()
            if _context_digest(value) != expected_context:
                raise ValueError("authority context changed")
            return _status(value[field])
        except (ValueError, TypeError, KeyError) as exc:
            raise ServingError("weekly_authority_unavailable") from exc

    def context(self, value=None):
        try:
            if value is None:
                value = self.read()
            expected_context = _context_digest(value)
            policy = value["current_policy"]
            if type(policy) is not dict or set(policy) != {
                "freshness_policy_fingerprint",
                "source_identity_authority_id",
                "max_status_age_seconds",
            }:
                raise ValueError("invalid policy")
            age = policy["max_status_age_seconds"]
            if type(age) is not int or not 0 < age <= 24 * 60 * 60:
                raise ValueError("invalid status age")
            players = value["players"]
            if type(players) is not list or any(type(p) is not list for p in players):
                raise ValueError("invalid players")
            result = {
                "policy": CurrentReadPolicy(
                    lambda: self._status("current_status", expected_context),
                    clock=self.clock,
                    freshness_policy_fingerprint=policy["freshness_policy_fingerprint"],
                    source_identity_authority_id=policy["source_identity_authority_id"],
                    max_status_age=timedelta(seconds=age),
                ),
                "check_binding": lambda: self.binding(expected_context),
                "players": tuple(tuple(p) for p in players),
            }
            historical = value["historical"]
            if historical is not None:
                from app.serving.composite import CompositionAuthority

                if type(historical) is not dict or set(historical) != {
                    "root",
                    "snapshot_id",
                    "status",
                    "composition",
                }:
                    raise ValueError("invalid historical authority")
                composition = historical["composition"]
                if type(composition) is not dict or set(composition) != {
                    "policy_id",
                    "identity_record_id",
                    "historical_identity_id",
                    "current_identity_id",
                    "approved_player_ids",
                }:
                    raise ValueError("invalid composition authority")
                result["historical_policy"] = OfflineReadPolicy(
                    lambda: self._historical_status(expected_context),
                    clock=self.clock,
                    max_status_age=timedelta(seconds=age),
                )
                result["composition_authority"] = CompositionAuthority(
                    **{
                        **composition,
                        "approved_player_ids": frozenset(
                            composition["approved_player_ids"]
                        ),
                    }
                )
            return result
        except (ValueError, TypeError, KeyError) as exc:
            raise ServingError("weekly_authority_unavailable") from exc

    def _historical_status(self, expected_context):
        try:
            value = self.read()
            if _context_digest(value) != expected_context:
                raise ValueError("authority context changed")
            return _status(value["historical"]["status"])
        except (ValueError, TypeError, KeyError) as exc:
            raise ServingError("weekly_authority_unavailable") from exc

    def application(self, data_root, *, settings, jobs_app=None):
        from app.serving.weekly_selection import create_weekly_candidate_application

        value = self.read()
        context = self.context(value)
        historical = value["historical"]
        options = {}
        if historical is not None:
            options = {
                "historical_root": Path(historical["root"]),
                "historical_snapshot_id": historical["snapshot_id"],
                "historical_policy": context["historical_policy"],
                "composition_authority": context["composition_authority"],
            }
        return create_weekly_candidate_application(
            data_root,
            policy=context["policy"],
            binding=self.binding(),
            check_binding=context["check_binding"],
            players=context["players"],
            settings=settings,
            jobs_app=jobs_app,
            binding_loader=self.binding,
            refresh_context_loader=self.context,
            **options,
        )
