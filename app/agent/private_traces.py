"""Opt-in bounded local diagnostics. Never a complete replay or service log."""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import math
import os
import re
import stat
import time
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from app.config import Settings

logger = logging.getLogger("app.agent")
# No arbitrary object serialization. Unknown fields, keys and raw answers vanish.
FIELDS = frozenset(
    "question context query_plan semantic_plan semantic_evidence reference_evidence award_evidence study_claims conversation_context sections queries contract_version operation date_range season_type opponent_abbr home_away last_plan route source reason_code player_id player_name player_ids players resolved_players unresolved_players team_abbr team_id season seasons phase start end date_start date_end time_window metrics metric aggregation filters limit needs_clarification clarification_question clarification_options candidates confidence status scope evidence evidence_id study_id source_id content_sha256 evidence_sha256 sources id version reporting_mode reporting_cutoff previous_start previous_end teammate_id selected_player last_player_id last_player_name last_query_plan last_route turns pending ask_route".split()
)
SECRET = re.compile(
    r"(?i)(?:bearer\s+\S+|AKIA[A-Z0-9]{16}|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+|(?:sk|ghp|github_pat|AIza)[-_a-zA-Z0-9]{8,}|[\w.+-]+@[\w.-]+\.[a-z]{2,}|(?:api[_ -]?key|token|password|secret|user[_ -]?id)\s*[:=]\s*[^\s,;]+)"
)
NAME = re.compile(r"[0-9a-f]{32}\.json\Z")
CONFIG_FIELDS = (
    "season",
    "agent_planner_enabled",
    "agent_planner_model",
    "agent_planner_min_confidence",
    "agent_player_match_min_confidence",
    "agent_max_tool_calls",
    "agent_conversation_max_turns",
    "agent_request_deadline_seconds",
    "agent_max_concurrent_requests",
)


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


@lru_cache(maxsize=1)
def code_digest() -> str:
    root = Path(__file__).resolve().parents[1]
    hashed = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        hashed.update(str(path.relative_to(root)).encode())
        hashed.update(path.read_bytes())
    return hashed.hexdigest()


class Projection:
    def __init__(self) -> None:
        self.reasons: set[str] = {"projection_only_not_complete_replay"}
        self.remaining = 800

    def text(self, value: str) -> str:
        if len(value) > 1000:
            self.reasons.add("truncated")
        original = value[:1000]
        cleaned = SECRET.sub("[REDACTED]", original)
        if cleaned != original:
            self.reasons.add("redacted")
        return cleaned

    def project(self, value: Any, depth: int = 0) -> Any:
        self.remaining -= 1
        if depth > 8 or self.remaining < 0:
            self.reasons.add("truncated")
            return None
        if isinstance(value, str):
            return self.text(value)
        if value is None or type(value) in (bool, int):
            return value
        if type(value) is float:
            return value if math.isfinite(value) else None
        if isinstance(value, dict):
            result = {}
            for index, (key, item) in enumerate(value.items()):
                if index >= 80:
                    self.reasons.add("truncated")
                    break
                if key in FIELDS:
                    result[key] = self.project(item, depth + 1)
                else:
                    self.reasons.add("fields_omitted")
            return result
        if isinstance(value, (list, tuple)):
            if len(value) > 20:
                self.reasons.add("truncated")
            return [self.project(item, depth + 1) for item in value[:20]]
        self.reasons.add("unsupported_value")
        return None


def _directory(path: str) -> int:
    """Walk by descriptors so no path component can redirect through a symlink."""
    target = Path(os.path.abspath(path))
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in target.parts[1:]:
            try:
                os.mkdir(component, mode=0o700, dir_fd=fd)
            except FileExistsError:
                pass
            child = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
            )
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError("untrusted trace directory")
        return fd
    except BaseException:
        os.close(fd)
        raise


def write_record(settings: Settings, record: dict[str, Any]) -> None:
    name = UUID(record["request_id"]).hex + ".json"
    data = canonical(record)
    if len(data) > settings.agent_trace_max_bytes:
        record = {
            key: record[key]
            for key in (
                "schema",
                "request_id",
                "time",
                "transport",
                "outcome",
                "error_type",
                "code",
                "config_sha256",
            )
        }
        record["incomplete_reasons"] = [
            "record_size_limit",
            "projection_only_not_complete_replay",
        ]
        data = canonical(record)
    if len(data) > settings.agent_trace_max_bytes:
        raise ValueError("trace too large")
    directory = _directory(settings.agent_trace_directory)
    lock = -1
    temporary = uuid4().hex + ".tmp"
    try:
        lock = os.open(
            ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=directory
        )
        info = os.fstat(lock)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise ValueError("untrusted trace lock")
        fcntl.flock(lock, fcntl.LOCK_EX)
        # Fail duplicate writes before retention changes; never replace a record.
        try:
            os.stat(name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise FileExistsError("duplicate trace")
        records = []
        for candidate in os.listdir(directory):
            if not NAME.fullmatch(candidate):
                continue
            info = os.stat(candidate, dir_fd=directory, follow_symlinks=False)
            if (
                stat.S_ISREG(info.st_mode)
                and info.st_uid == os.getuid()
                and info.st_nlink == 1
                and stat.S_IMODE(info.st_mode) == 0o600
            ):
                records.append((info.st_mtime, candidate))
        records.sort()
        cutoff = time.time() - settings.agent_trace_retention_days * 86400
        for index, (mtime, candidate) in enumerate(records):
            if (
                mtime < cutoff
                or index < len(records) - settings.agent_trace_max_records + 1
            ):
                os.unlink(candidate, dir_fd=directory)
        file_fd = os.open(
            temporary,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory,
        )
        with os.fdopen(file_fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(
            temporary,
            name,
            src_dir_fd=directory,
            dst_dir_fd=directory,
            follow_symlinks=False,
        )
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
        if lock >= 0:
            os.close(lock)
        os.close(directory)


class PrivateCapture:
    def __init__(
        self,
        settings: Settings,
        trace: Any,
        *,
        provider: str | None,
        transport: str,
        static_version: str,
        selected_player: dict[str, Any] | None = None,
    ) -> None:
        self.settings = settings
        self.projection = Projection()
        self.done = False
        self.record: dict[str, Any] = {
            "schema": "ask_private_trace/1",
            "request_id": trace.request_id,
            "time": time.time(),
            "transport": transport,
            "question": self.projection.text(trace.question),
            "selected_player": self.projection.project(selected_player),
            "season": settings.season,
            "conversation_sha256": digest(trace.conversation_id),
            "provider": self.projection.text(provider or "openai"),
            "code": {"static_version": static_version, "source_sha256": code_digest()},
            "config_sha256": digest(
                {key: getattr(settings, key) for key in CONFIG_FIELDS}
            ),
        }

    def context(self, store: Any, conversation_id: str) -> None:
        try:
            turns = store.get_turns(
                conversation_id,
                max_turns=min(self.settings.agent_conversation_max_turns, 20),
            )
            pending = store.get_pending_clarification(conversation_id)
            self.record["context_before"] = self.projection.project(
                {
                    "turns": [
                        {"question": turn.question, "context": turn.context}
                        for turn in turns
                    ],
                    "pending": None
                    if pending is None
                    else {
                        "question": pending.question,
                        "query_plan": pending.query_plan,
                        "route": pending.route,
                        "candidates": pending.candidates,
                    },
                }
            )
        except Exception:
            self.projection.reasons.add("context_unavailable")

    def answer(self, answer: dict[str, Any]) -> None:
        try:
            self.record["answer_projection"] = self.projection.project(
                {
                    key: answer[key]
                    for key in (
                        "query_plan",
                        "semantic_plan",
                        "semantic_evidence",
                        "reference_evidence",
                        "award_evidence",
                        "study_claims",
                        "conversation_context",
                        "scope",
                        "evidence",
                        "ask_route",
                        "status",
                        "sources",
                    )
                    if key in answer
                }
            )
            self.record["projection_sha256"] = digest(self.record["answer_projection"])
        except Exception:
            self.projection.reasons.add("evidence_unavailable")

    def finish(self, trace: Any) -> None:
        if self.done:
            return
        self.done = True
        try:
            self.record.update(
                model=self.projection.text(trace.model),
                outcome=self.projection.text(trace.outcome),
                error_type=self.projection.text(trace.error_type or ""),
                ask_route=self.projection.project(trace.ask_route),
            )
            if "answer_projection" not in self.record:
                self.projection.reasons.add("evidence_unavailable")
            self.record["route"] = self.projection.project(trace.route)
            self.record["tool_scope"] = [
                self.projection.project(tool.get("args")) for tool in trace.tools[:20]
            ]
            self.record["incomplete_reasons"] = sorted(self.projection.reasons)
            write_record(self.settings, self.record)
        except Exception:
            try:
                logger.warning("private_trace_write_failed")
            except Exception:
                pass
