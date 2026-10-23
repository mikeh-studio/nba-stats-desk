"""Private local development publications with atomic compare-and-swap promotion.

This is a POSIX development store, not a BigQuery publisher. Captures stay private.
Readers independently select the trusted snapshot ID; a pointer is not source trust.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime
from pathlib import Path

from app.agent.execution import checkpoint
from app.agent.semantics import SemanticError
from app.agent.serving_publication import digest
from app.agent.serving_runtime import PilotPublication


class DevelopmentPublications:
    def __init__(self, directory: Path, *, max_bytes: int = 50_000_000):
        if type(max_bytes) is not int or not 1 <= max_bytes <= 100_000_000:
            raise ValueError("Invalid development publication byte bound")
        self.directory = directory
        self.max_bytes = max_bytes
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _read(self, path: Path):
        checkpoint()
        with path.open("rb") as stream:
            content = stream.read(self.max_bytes + 1)
        if len(content) > self.max_bytes:
            raise SemanticError("invalid_evidence", "Publication exceeds byte bound")
        return json.loads(content)

    def publish(
        self,
        snapshot: dict,
        *,
        expected_snapshot_id: str,
        expected_generation: str | None,
        now: datetime,
    ) -> str:
        import fcntl

        generation = uuid.uuid4().hex
        # Build and validate before the atomic pointer section.
        PilotPublication.build(
            snapshot,
            expected_snapshot_id=expected_snapshot_id,
            publication_id=generation,
            relation_version=generation,
            now=now,
        )
        envelope = dict(version=1, generation=generation, snapshot=snapshot)
        content = json.dumps(envelope, sort_keys=True, allow_nan=False).encode()
        if len(content) > self.max_bytes:
            raise SemanticError("invalid_evidence", "Publication exceeds byte bound")
        with (self.directory / ".promotion.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            pointer = self.directory / "current.json"
            previous = self._read(pointer)["generation"] if pointer.exists() else None
            if previous != expected_generation:
                raise SemanticError(
                    "publication_conflict", "Publication generation changed"
                )
            checkpoint()
            artifact = self.directory / f"{generation}.json"
            with artifact.open("xb") as stream:
                os.chmod(artifact, 0o600)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            # Verify stored representation before publishing its pointer.
            if self._read(artifact) != envelope:
                raise SemanticError("invalid_evidence", "Stored publication changed")
            temporary = self.directory / f".{generation}.pointer"
            with temporary.open("x") as stream:
                os.chmod(temporary, 0o600)
                json.dump(dict(generation=generation, digest=digest(envelope)), stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, pointer)
        return generation

    def load(self, *, expected_snapshot_id: str, now: datetime) -> PilotPublication:
        try:
            pointer = self._read(self.directory / "current.json")
            generation = pointer["generation"]
            if not isinstance(generation, str) or not re.fullmatch(
                "[0-9a-f]{32}", generation
            ):
                raise ValueError("Invalid generation")
            envelope = self._read(self.directory / f"{generation}.json")
            if (
                envelope.get("version") != 1
                or envelope.get("generation") != generation
                or digest(envelope) != pointer["digest"]
            ):
                raise ValueError("Changed artifact")
            return PilotPublication.build(
                envelope["snapshot"],
                expected_snapshot_id=expected_snapshot_id,
                publication_id=generation,
                relation_version=generation,
                now=now,
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise SemanticError(
                "invalid_evidence", "Development publication unavailable or invalid"
            ) from exc
