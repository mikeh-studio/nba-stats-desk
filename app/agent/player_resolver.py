from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from app.repository import WarehouseRepository

try:  # pragma: no cover - exercised when dependency is installed in prod.
    from rapidfuzz import fuzz

    def _token_sort_ratio(a: str, b: str) -> float:
        return float(fuzz.token_sort_ratio(a, b))

except ImportError:  # pragma: no cover - local fallback for constrained envs.
    from difflib import SequenceMatcher

    def _token_sort_ratio(a: str, b: str) -> float:
        left = " ".join(sorted(a.split()))
        right = " ".join(sorted(b.split()))
        return SequenceMatcher(None, left, right).ratio() * 100


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


def _dedupe_candidates(candidates: list[PlayerCandidate]) -> list[PlayerCandidate]:
    by_key: dict[int | str, PlayerCandidate] = {}
    for candidate in candidates:
        key: int | str = candidate.player_id or normalize_player_text(
            candidate.player_name
        )
        existing = by_key.get(key)
        if existing is None or candidate.confidence > existing.confidence:
            by_key[key] = candidate
    return sorted(
        by_key.values(),
        key=lambda item: (
            -item.confidence,
            item.overall_rank is None,
            item.overall_rank or 999999,
            item.player_name,
        ),
    )


def _candidate_score(query: str, player_name: str) -> float:
    query_norm = normalize_player_text(query)
    name_norm = normalize_player_text(player_name)
    if not query_norm or not name_norm:
        return 0.0
    if query_norm == name_norm:
        return 1.0
    if name_norm.startswith(query_norm) or query_norm in name_norm:
        return 0.9
    return _token_sort_ratio(query_norm, name_norm) / 100


class PlayerResolver:
    def __init__(
        self,
        repo: WarehouseRepository,
        *,
        min_confidence: float = 0.78,
        alias_map: dict[str, str] | None = None,
    ) -> None:
        self.repo = repo
        self.min_confidence = min_confidence
        self.alias_map = alias_map or load_player_aliases()

    def resolve(self, raw_name: str, *, limit: int = 5) -> PlayerResolution:
        query = str(raw_name or "").strip()
        if not query:
            return PlayerResolution(raw_name=query, status="not_found", confidence=0)
        candidates: list[PlayerCandidate] = []
        query_norm = normalize_player_text(query)
        alias_target = self.alias_map.get(query_norm)
        search_terms = [query]
        if alias_target and normalize_player_text(alias_target) != query_norm:
            search_terms.insert(0, alias_target)

        for index, term in enumerate(search_terms):
            method = "alias" if index == 0 and alias_target else "search"
            try:
                rows = self.repo.search_players(term, limit=limit)
            except Exception:
                rows = []
            for row in rows:
                score = _candidate_score(
                    alias_target or query, str(row.get("player_name") or "")
                )
                if method == "alias":
                    score = max(score, 0.96)
                candidates.append(
                    _candidate_from_row(row, confidence=score, method=method)
                )

        candidates.extend(self._fuzzy_candidates(query, limit=limit))
        candidates = _dedupe_candidates(candidates)[:limit]
        if not candidates:
            return PlayerResolution(raw_name=query, status="not_found", confidence=0)

        top = candidates[0]
        second = candidates[1] if len(candidates) > 1 else None
        if top.confidence < self.min_confidence:
            return PlayerResolution(
                raw_name=query,
                status="not_found",
                confidence=top.confidence,
                candidates=candidates,
            )
        if (
            second is not None
            and second.confidence >= self.min_confidence
            and (top.confidence - second.confidence < 0.05)
        ):
            return PlayerResolution(
                raw_name=query,
                status="ambiguous",
                confidence=top.confidence,
                candidates=candidates,
            )
        return PlayerResolution(
            raw_name=query,
            status="ok",
            confidence=top.confidence,
            player=top,
            candidates=candidates,
            resolution_method=top.match_method,
        )

    def _fuzzy_candidates(self, query: str, *, limit: int) -> list[PlayerCandidate]:
        rows: list[dict[str, Any]] = []
        getter = getattr(self.repo, "list_agent_player_candidates", None)
        if callable(getter):
            try:
                rows = list(getter(limit=1000))
            except Exception:
                rows = []
        candidates = []
        for row in rows:
            score = _candidate_score(query, str(row.get("player_name") or ""))
            if score >= max(0.7, self.min_confidence - 0.08):
                candidates.append(
                    _candidate_from_row(row, confidence=score, method="fuzzy")
                )
        return _dedupe_candidates(candidates)[:limit]

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
