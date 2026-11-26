"""Independently reviewed season-phase game membership for published statistics."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import date
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.serving.publication_contract import digest

PublicationId = Annotated[str, Field(pattern=r"^pub_[0-9a-f]{16}$")]


def game_membership_digest(game_ids: Sequence[str]) -> str:
    return sha256("\n".join(sorted(game_ids)).encode()).hexdigest()


class LeagueCoverageAttestation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal["league-coverage/1"]
    publication_id: PublicationId
    season: str = Field(pattern=r"^20[0-9]{2}-[0-9]{2}$")
    phase: Literal["Regular Season"]
    first: date
    as_of: date
    receipt_count: int = Field(gt=0, le=366)
    game_count: int = Field(gt=0, le=1500)
    games_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    def matches(self, release: Mapping[str, Any], game_ids: Sequence[str]) -> bool:
        scope = release["scope"]
        return (
            self.publication_id == release["publication_id"]
            and self.season == scope["season"]
            and self.phase == scope["phase"]
            and str(self.first) == scope["first"]
            and str(self.as_of) == scope["last"]
            and self.receipt_count == (self.as_of - self.first).days + 1
            and self.game_count == len(game_ids)
            and self.games_sha256 == game_membership_digest(game_ids)
        )


class LeagueCoverageProof(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal["league-coverage/1"]
    publication_id: PublicationId
    season: str = Field(pattern=r"^20[0-9]{2}-[0-9]{2}$")
    phase: Literal["Regular Season"]
    first: date
    as_of: date
    receipt_count: int = Field(gt=0, le=366)
    game_ids: tuple[Annotated[str, Field(pattern=r"^[0-9]{10}$")], ...] = Field(
        min_length=1, max_length=1500
    )

    def attestation(
        self, release: Mapping[str, Any], game_ids: Sequence[str]
    ) -> LeagueCoverageAttestation:
        if len(set(self.game_ids)) != len(self.game_ids):
            raise ValueError("Independent coverage review contains duplicate game IDs")
        proof = LeagueCoverageAttestation.model_validate(
            {
                **self.model_dump(exclude={"game_ids"}),
                "game_count": len(self.game_ids),
                "games_sha256": game_membership_digest(self.game_ids),
            }
        )
        if not proof.matches(release, game_ids):
            raise ValueError(
                "Independent coverage review disagrees with verified release"
            )
        return proof


def coverage_location(entry, *, cloud_only=False):
    if (
        not isinstance(entry, dict)
        or set(entry) != {"path", "digest"}
        or not isinstance(entry["path"], str)
        or not entry["path"]
        or not isinstance(entry["digest"], str)
        or not re.fullmatch(r"[a-f0-9]{64}", entry["digest"])
        or (cloud_only and not entry["path"].startswith("gs://"))
    ):
        raise ValueError("invalid Ask league coverage configuration")
    path = entry["path"]
    return path if path.startswith("gs://") else Path(path)


def read_pinned_coverage(location, expected_digest, read_document):
    try:
        if isinstance(location, Path):
            with location.open("rb") as source:
                data = source.read(131_073)
            if len(data) > 131_072:
                return None
            proof = json.loads(data)
        else:
            proof = read_document(location)
        if digest(proof) == expected_digest:
            return LeagueCoverageProof.model_validate(proof)
    except (KeyError, OSError, TypeError, ValueError):
        pass
    # An optional league assertion may disappear without invalidating player facts.
    return None
