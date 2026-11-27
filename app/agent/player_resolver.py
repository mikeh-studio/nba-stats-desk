from __future__ import annotations

from collections import defaultdict
from typing import Any

from pydantic import BaseModel, Field

from app.agent.alias_builder import (
    load_player_aliases as load_player_aliases,
)
from app.agent.alias_builder import (
    normalize_player_text as normalize_player_text,
)
from app.agent.alias_builder import (
    player_aliases_for_cohort,
)
from app.agent.execution import timed
from app.repository import WarehouseRepository


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
        self.alias_map = load_player_aliases() if alias_map is None else alias_map
        self.alias_overrides = alias_map

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
        names = defaultdict(set)
        for row in rows:
            if row.get("player_id") is None or not row.get("player_name"):
                continue
            player_id = row["player_id"]
            players[player_id] = dict(row)
            names[player_id].add(row["player_name"])
        for player_id, player in players.items():
            if len(names[player_id]) > 1:
                player["observed_names"] = tuple(
                    sorted(names[player_id] - {player["player_name"]})
                )
        roster = player_aliases_for_cohort(
            players.values(), overrides=self.alias_overrides
        )
        result = record_resolution(query, resolve_player_name(query, roster))
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
