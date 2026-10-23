from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from app.agent.execution import timed
from app.repository import WarehouseRepository

ALIAS_PATH = Path(__file__).with_name("player_aliases.yml")


class PlayerCandidate(BaseModel):
    player_id: int | None = None
    player_name: str
    team_abbr: str | None = None
    latest_game_date: str | None = None
    games_sampled: int | None = None
    overall_rank: int | None = None
    confidence: float = Field(ge=0, le=1)
    match_method: str


class PlayerResolution(BaseModel):
    raw_name: str
    status: str
    confidence: float = Field(ge=0, le=1)
    player: PlayerCandidate | None = None
    candidates: list[PlayerCandidate] = Field(default_factory=list)
    resolution_method: str | None = None


def normalize_player_text(value: str) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-zA-Z0-9]+", " ", text).strip().casefold()
    return re.sub(r"\s+", " ", text)


@lru_cache(maxsize=1)
def load_player_aliases(path: str | Path = ALIAS_PATH) -> dict[str, str]:
    with Path(path).open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}
    aliases: dict[str, str] = {}
    for canonical, values in (loaded.get("aliases") or {}).items():
        canonical_norm = normalize_player_text(str(canonical))
        aliases[canonical_norm] = str(canonical)
        for alias in values or []:
            aliases[normalize_player_text(str(alias))] = str(canonical)
    return aliases


def _to_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None


def _candidate_from_row(
    row: dict[str, Any],
    *,
    confidence: float,
    method: str,
) -> PlayerCandidate:
    return PlayerCandidate(
        player_id=_to_int(row.get("player_id")),
        player_name=str(row.get("player_name") or ""),
        team_abbr=row.get("team_abbr") or row.get("latest_team_abbr"),
        latest_game_date=row.get("latest_game_date"),
        games_sampled=_to_int(row.get("games_sampled")),
        overall_rank=_to_int(row.get("overall_rank")),
        confidence=round(max(0.0, min(1.0, confidence)), 2),
        match_method=method,
    )


class PlayerResolver:
    def __init__(
        self,
        repo: WarehouseRepository,
        *,
        min_confidence: float = 0.78,
        alias_map: dict[str, str] | None = None,
    ) -> None:
        self.repo = repo
        # Retained constructor argument for configuration compatibility. Identity
        # decisions use the shared versioned policy, never a route-specific cutoff.
        self.alias_map = alias_map or load_player_aliases()

    @timed("identity")
    def resolve(self, raw_name: str, *, limit: int = 5) -> PlayerResolution:
        query = str(raw_name or "").strip()
        if not query:
            return PlayerResolution(raw_name=query, status="not_found", confidence=0)
        from app.agent.name_matching import record_resolution
        from app.agent.player_suggestions import resolve_player_name

        getter = getattr(self.repo, "list_agent_player_candidates", None)
        rows = list(getter(limit=1000) or []) if callable(getter) else []
        # Search supplements a bounded roster; a provider error must not create
        # false certainty by silently removing competing identities.
        terms = {query, self.alias_map.get(normalize_player_text(query), query)}
        for term in terms:
            rows.extend(self.repo.search_players(term, limit=1000))
        players = {}
        for row in rows:
            if row.get("player_id") is None or not row.get("player_name"):
                continue
            player = dict(row)
            canonical = normalize_player_text(player["player_name"])
            player["aliases"] = sorted(
                set(player.get("aliases", []))
                | {
                    alias
                    for alias, target in self.alias_map.items()
                    if normalize_player_text(target) == canonical
                }
            )
            players[player["player_id"]] = player
        result = record_resolution(
            query, resolve_player_name(query, list(players.values()))
        )
        method = result.get("resolution", {}).get("method", "exact_or_alias")
        candidates = [
            _candidate_from_row(
                p,
                confidence=1 if method == "exact_or_alias" else 0.9,
                method=method if result["status"] == "ok" else "spelling_suggestion",
            )
            for p in result["matches"][:limit]
        ]
        status = (
            "ambiguous"
            if result["status"] == "clarification_required"
            else result["status"]
        )
        return PlayerResolution(
            raw_name=query,
            status=status,
            confidence=candidates[0].confidence if candidates else 0,
            player=candidates[0] if status == "ok" else None,
            candidates=candidates,
            resolution_method=method if status == "ok" else None,
        )

    def resolve_many(
        self, raw_names: list[str], *, limit: int = 5
    ) -> list[PlayerResolution]:
        seen: set[str] = set()
        resolutions = []
        for raw_name in raw_names:
            key = normalize_player_text(raw_name)
            if not key or key in seen:
                continue
            seen.add(key)
            resolutions.append(self.resolve(raw_name, limit=limit))
        return resolutions
