"""Bounded, cached warehouse evidence for governed Ask requests."""

from __future__ import annotations

import re
from collections import OrderedDict
from datetime import date
from threading import Lock
from time import monotonic
from typing import Any, Protocol

from app.agent.clock import today as semantic_today
from app.agent.execution import record_data_read, timed
from app.agent.player_resolver import load_player_aliases, normalize_player_text
from app.agent.semantic_source import (
    BigQuerySemanticSource,
    ScopedSemanticSource,
    snapshot_evidence,
)
from app.agent.semantics import Evidence, SemanticError
from app.agent.source_policy import denied, get_source_policy
from app.seasons import DEFAULT_SEASON, SEASONS, season_bounds, validate_season


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


def seasons_in_dates(start, end):
    """All available archives overlapping a validated explicit metric window."""
    try:
        lo, hi = date.fromisoformat(start), date.fromisoformat(end)
    except (ValueError, TypeError) as exc:
        raise SemanticError(
            "invalid_scope", "Please provide valid start and end dates."
        ) from exc
    if lo > hi or hi > semantic_today():
        raise SemanticError(
            "invalid_scope",
            "Please choose an ordered period ending no later than today.",
        )
    if lo < season_bounds(SEASONS[-1])[0]:
        raise SemanticError(
            "unsupported_coverage", "Requested dates precede available archives."
        )
    seasons = [
        s for s in SEASONS if season_bounds(s)[0] <= hi and season_bounds(s)[1] >= lo
    ]
    if not seasons:
        raise SemanticError(
            "unsupported_coverage", "Requested dates have no available archive."
        )
    return seasons


class SnapshotSource(Protocol):
    def capture(self, seasons: list[str]) -> dict[str, Any]: ...


class SemanticWarehouse:
    def __init__(
        self, source: SnapshotSource, ttl_seconds: int = 300, *, serving_shadow=None
    ):
        self.serving_shadow = serving_shadow
        self.source = source
        self.ttl_seconds = ttl_seconds
        self._lock = Lock()
        self._cache: OrderedDict[tuple[Any, ...], tuple[float, dict[str, Any]]] = (
            OrderedDict()
        )

    def players(self, evidence: Evidence) -> list[dict[str, Any]]:
        return source_players(
            evidence, source_policy=getattr(self.source, "source_policy", None)
        )

    def _cache_key(self, seasons):
        policy = get_source_policy(getattr(self.source, "source_policy", None))
        return (
            id(self.source),
            getattr(self.source, "cache_identity", None),
            policy,
            *sorted(seasons),
        )

    def cached_snapshot(self, seasons):
        """Return only an already loaded, validated scope; never dispatch a read."""
        key = self._cache_key(seasons)
        with self._lock:
            cached = self._cache.get(key)
            if not cached or monotonic() - cached[0] >= self.ttl_seconds:
                return None
            snapshot_evidence(cached[1])
            return cached[1]

    @timed("evidence")
    def load(self, seasons: list[str]) -> tuple[dict[str, Any], Evidence]:
        key = self._cache_key(seasons)
        with self._lock:
            cached = self._cache.get(key)
            cache_hit = bool(cached and monotonic() - cached[0] < self.ttl_seconds)
            if cached and cache_hit:
                snapshot = cached[1]
            else:
                snapshot = self.source.capture(sorted(seasons))
                snapshot_evidence(snapshot)
                self._cache[key] = (monotonic(), snapshot)
                self._cache.move_to_end(key)
                while len(self._cache) > 3:
                    self._cache.popitem(last=False)
        # Validate even cached rows. Errors are never cached as empty evidence.
        try:
            evidence = snapshot_evidence(snapshot)
        except Exception:
            record_data_read(kind="evidence_cache", status="error", cache_hit=cache_hit)
            raise
        record_data_read(
            kind="evidence_cache",
            status="ok",
            rows=len(evidence.rows),
            cache_hit=cache_hit,
            source_version=evidence.snapshot_id,
        )
        return snapshot, evidence


def source_players(evidence: Evidence, *, source_policy=None) -> list[dict[str, Any]]:
    policy = get_source_policy(source_policy)
    if policy is not None:
        if not evidence.complete or not set(evidence.source.split(", ")) <= {
            r for _, r in policy.relations
        }:
            denied("Identity evidence is outside the approved source relations")
        if not evidence.covered_scopes <= {
            (s, p) for s, _ in policy.relations for p in policy.phases
        }:
            denied("Identity evidence is outside the approved source scope")
    return players_from_rows(evidence.rows, source_policy=policy)


@timed("identity")
def players_from_rows(rows, *, source_policy=None) -> list[dict[str, Any]]:
    """Resolve identities from observed rows or bound appearance metadata."""
    policy = get_source_policy(source_policy)
    if policy is not None:
        rows = list(rows)
        allowed = {(s, p) for s, _ in policy.relations for p in policy.phases}
        if any((r.get("season"), r.get("season_type")) not in allowed for r in rows):
            denied("Identity rows are outside the approved source scope")
        rows = [r for r in rows if r["player_id"] in policy.allowed_player_ids]
        names: dict[int, str] = {}
        for row in rows:
            if (
                row["player_id"] in names
                and names[row["player_id"]] != row["player_name"]
            ):
                raise SemanticError(
                    "ambiguous_identity", "Conflicting names for an approved identity"
                )
            names[row["player_id"]] = row["player_name"]
    players: dict[int, dict[str, Any]] = {}
    latest_teams: dict[int, tuple[str, str]] = {}
    for row in rows:
        player = players.setdefault(
            row["player_id"],
            {
                "player_id": row["player_id"],
                "player_name": row["player_name"],
                "aliases": set(),
            },
        )
        player["aliases"].add(row["player_name"])
        team = str(row.get("team_abbr") or "")
        if team:
            latest_teams[row["player_id"]] = max(
                latest_teams.get(row["player_id"], ("", "")),
                (str(row.get("game_date") or ""), team),
            )
    aliases = load_player_aliases()
    for player in players.values():
        player["team_abbr"] = latest_teams.get(player["player_id"], ("", ""))[1] or None
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


def warehouse_for_repository(repo: Any) -> Any:
    with _factory_lock:
        policy = get_source_policy(getattr(repo, "source_policy", None))
        warehouse = getattr(repo, "_governed_warehouse", None)
        if (
            warehouse is not None
            and getattr(repo, "_governed_source_policy", None) != policy
        ):
            warehouse = None
        if warehouse is None:
            settings = repo.settings
            dataset = settings.gold_dataset
            if settings.season != DEFAULT_SEASON:
                dataset = dataset.removesuffix("_" + settings.season.replace("-", "_"))
            source_class = BigQuerySemanticSource
            if policy and policy.source_mode == "retained_local_review":
                from app.agent.espn_dev_source import RetainedReviewSource

                source_class = RetainedReviewSource
            source = source_class(
                repo.client,
                project=settings.project_id,
                gold_dataset=dataset,
                position_context_path=settings.agent_position_context_path,
                source_policy=policy,
                max_rows=policy.max_rows if policy else 100_000,
                maximum_bytes_billed=policy.maximum_bytes_billed
                if policy
                else 100_000_000,
            )
            warehouse = SemanticWarehouse(
                ScopedSemanticSource(source, policy.phases) if policy else source,
                settings.agent_cache_ttl_seconds,
            )
            if settings.ask_local_espn_dev and not policy:
                from app.agent.espn_dev_source import ESPNDevSource

                warehouse.source = ESPNDevSource(warehouse.source)
            if settings.ask_serving_mode != "off" and not policy:
                from datetime import datetime

                from app.agent.semantic_source import capture_timestamp
                from app.agent.serving_warehouse import RefreshingServingWarehouse

                warehouse = RefreshingServingWarehouse(
                    warehouse,
                    repo.client,
                    settings.ask_serving_reference_path,
                    lambda: datetime.fromisoformat(capture_timestamp()),
                    project=settings.project_id,
                    mode=settings.ask_serving_mode,
                    reuse_cohort=settings.ask_cohort_cache_enabled,
                    families=settings.ask_serving_families,
                )
            repo._governed_warehouse = warehouse
            repo._governed_source_policy = policy
        return warehouse
