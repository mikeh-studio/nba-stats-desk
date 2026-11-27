"""Deterministic aliases scoped to observed player identities."""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

ALIAS_PATH = Path(__file__).with_name("player_aliases.yml")
_SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}


def normalize_player_text(value: str) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"['’‘ʼ]", "", text)
    return re.sub(r"[^a-zA-Z0-9]+", " ", text).strip().casefold()


def name_base(name: str) -> str:
    parts = normalize_player_text(name).split()
    return " ".join(parts[:-1] if len(parts) > 2 and parts[-1] in _SUFFIXES else parts)


@lru_cache(maxsize=1)
def _curated_alias_pairs(path: str | Path = ALIAS_PATH) -> tuple[tuple[str, str], ...]:
    with Path(path).open(encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}
    return tuple(
        (normalize_player_text(alias), canonical)
        for canonical, values in (loaded.get("aliases") or {}).items()
        for alias in (canonical, *(values or ()))
    )


@lru_cache(maxsize=1)
def load_player_aliases(path: str | Path = ALIAS_PATH) -> dict[str, str]:
    """Expose curated spellings to callers that do not yet have a roster."""
    return dict(_curated_alias_pairs(path))


def build_programmatic_aliases(
    players: Iterable[Mapping[str, Any]],
    *,
    overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Map normalized aliases to canonical names when exactly one ID claims them."""
    claims: dict[str, set[Any]] = defaultdict(set)
    name_owners: dict[str, set[Any]] = defaultdict(set)
    token_owners: dict[str, set[Any]] = defaultdict(set)
    canonical_names: dict[Any, set[str]] = defaultdict(set)
    for player in players:
        name = player["player_name"]
        key = normalize_player_text(name)
        if not key:
            continue
        identity = player.get("player_id")
        if identity is None:
            identity = name
        canonical_names[identity].add(name)
        name_owners[key].add(identity)
        base = name_base(name)
        claims[key].add(identity)
        claims[base].add(identity)
        # Retain the older spaced spelling for apostrophized names.
        claims[normalize_player_text(re.sub(r"['’‘ʼ]", " ", name))].add(identity)
        parts = base.split()
        if len(parts) > 1:
            claims[parts[-1]].add(identity)
        for token in parts:
            token_owners[token].add(identity)
        for alias in player.get("aliases", ()):
            normalized = normalize_player_text(alias)
            if normalized:
                claims[normalized].add(identity)
                if " " in normalized:
                    name_owners[normalized].add(identity)

    pairs = _curated_alias_pairs() if overrides is None else overrides.items()
    for alias, target in pairs:
        key = normalize_player_text(alias)
        targets = name_owners.get(normalize_player_text(target))
        if key and targets:
            claims[key].update(targets)

    result = {}
    for alias, owners in sorted(claims.items()):
        # A one-word alias must also respect other canonical name tokens.
        owners = owners | token_owners.get(alias, set())
        if len(owners) == 1:
            result[alias] = min(canonical_names[next(iter(owners))])
    return result


def player_aliases_for_cohort(
    players: Iterable[Mapping[str, Any]],
    *,
    overrides: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Attach safe aliases while retaining existing identity spellings."""
    roster = list(players)
    by_name: dict[str, set[str]] = defaultdict(set)
    for alias, target in build_programmatic_aliases(
        roster, overrides=overrides
    ).items():
        by_name[target].add(alias)
    return [
        {
            **player,
            "aliases": sorted(
                set(player.get("aliases", ())) | by_name[player["player_name"]]
            ),
        }
        for player in roster
    ]
