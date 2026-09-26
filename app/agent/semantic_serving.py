"""Bounded, cached warehouse evidence for governed Ask requests."""

from __future__ import annotations

import re
from collections import OrderedDict
from threading import Lock
from time import monotonic
from typing import Any

from app.agent.player_resolver import load_player_aliases, normalize_player_text
from app.agent.semantic_source import BigQuerySemanticSource, snapshot_evidence
from app.agent.semantics import Evidence, SemanticError
from app.seasons import DEFAULT_SEASON, SEASONS, validate_season


def has_time_scope(question: str) -> bool:
    """A stated period must never be replaced by an older season."""
    return bool(
        re.search(
            r"\b(?:20\d{2}|19\d{2})\b|\b(?:20\d{2})[-–/]\d{2,4}\b|"
            r"\b(?:last|past|prior|previous|current|latest|this|today|yesterday|"
            r"tonight|recent|since|before|after|between|as.of|all.time|career|"
            r"january|february|march|april|may|june|july|august|september|"
            r"october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec)\b",
            question,
            re.I,
        )
    )


def season_candidates(question: str, selected: str) -> list[str]:
    requested = requested_seasons(question, selected)
    if has_time_scope(question) or len(requested) != 1:
        return requested
    return sorted((s for s in SEASONS if s <= selected), reverse=True)


def fallback_notice(requested: str, used: str) -> str:
    return (
        f"No matching data is available for {requested}. "
        f"Using {used}, the most recent available season for this request."
    )


def load_available_season(loader, candidates):
    """Try bounded archives newest first; source failures are never missing data."""
    last_error = None
    for season in candidates:
        try:
            snapshot, evidence = loader([season])
            evidence.validate()
            if not any(r["season"] == season for r in evidence.rows):
                raise SemanticError("unsupported_coverage", f"No evidence for {season}")
            return snapshot, evidence, season
        except SemanticError as exc:
            if exc.code != "unsupported_coverage":
                raise
            last_error = exc
    raise last_error or SemanticError("unsupported_coverage", "No season is available")


def requested_seasons(question: str, selected: str) -> list[str]:
    seasons = []
    for match in re.finditer(r"\b(20\d{2})[-–/](20\d{2}|\d{2})\b(?![-/–]\d)", question):
        start, end = match.groups()
        season = f"{start}-{end[-2:]}"
        try:
            validate_season(season)
        except ValueError as exc:
            raise SemanticError(
                "unsupported_coverage", f"Season {season} is unavailable"
            ) from exc
        if season not in seasons:
            seasons.append(season)
    if not seasons and re.search(r"\b(?:last|previous|prior) season\b", question, re.I):
        year = int(selected[:4]) - 1
        previous = f"{year}-{str(year + 1)[-2:]}"
        try:
            seasons = [validate_season(previous)]
        except ValueError as exc:
            raise SemanticError("unsupported_coverage", str(exc)) from exc
    return seasons or [validate_season(selected)]


class SemanticWarehouse:
    def __init__(self, source: BigQuerySemanticSource, ttl_seconds: int = 300):
        self.source = source
        self.ttl_seconds = ttl_seconds
        self._lock = Lock()
        self._cache: OrderedDict[tuple[str, ...], tuple[float, dict[str, Any]]] = (
            OrderedDict()
        )

    def players(self, evidence: Evidence) -> list[dict[str, Any]]:
        return source_players(evidence)

    def load(self, seasons: list[str]) -> tuple[dict[str, Any], Evidence]:
        key = tuple(sorted(seasons))
        with self._lock:
            cached = self._cache.get(key)
            if cached and monotonic() - cached[0] < self.ttl_seconds:
                snapshot = cached[1]
            else:
                snapshot = self.source.capture(list(key))
                snapshot_evidence(snapshot)
                self._cache[key] = (monotonic(), snapshot)
                self._cache.move_to_end(key)
                while len(self._cache) > 3:
                    self._cache.popitem(last=False)
        # Validate even cached rows. Errors are never cached as empty evidence.
        return snapshot, snapshot_evidence(snapshot)


def source_players(evidence: Evidence) -> list[dict[str, Any]]:
    players: dict[int, dict[str, Any]] = {}
    for row in evidence.rows:
        player = players.setdefault(
            row["player_id"],
            {
                "player_id": row["player_id"],
                "player_name": row["player_name"],
                "aliases": set(),
            },
        )
        player["aliases"].add(row["player_name"])
    aliases = load_player_aliases()
    for player in players.values():
        canonical = {normalize_player_text(n) for n in player["aliases"]}
        player["aliases"].update(
            alias
            for alias, name in aliases.items()
            if normalize_player_text(name) in canonical
        )
        player["aliases"].add(normalize_player_text(player["player_name"]))
        player["aliases"] = sorted(player["aliases"])
    return list(players.values())


_factory_lock = Lock()


def warehouse_for_repository(repo: Any) -> SemanticWarehouse:
    with _factory_lock:
        warehouse = getattr(repo, "_governed_warehouse", None)
        if warehouse is None:
            settings = repo.settings
            dataset = settings.gold_dataset
            if settings.season != DEFAULT_SEASON:
                dataset = dataset.removesuffix("_" + settings.season.replace("-", "_"))
            warehouse = SemanticWarehouse(
                BigQuerySemanticSource(
                    repo.client, project=settings.project_id, gold_dataset=dataset
                ),
                settings.agent_cache_ttl_seconds,
            )
            repo._governed_warehouse = warehouse
        return warehouse
