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

from app.agent.name_vocabulary import SCOPE_WORDS

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


def observed_player_names(player: Mapping[str, Any]) -> tuple[str, ...]:
    return (player["player_name"], *player.get("observed_names", ()))


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


def _folded_raw_surname(spelling: str) -> str:
    text = unicodedata.normalize("NFKD", str(spelling or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"['’‘ʼ]", "", text)
    tokens = [
        re.sub(r"^[^a-zA-Z0-9]+|[^a-zA-Z0-9]+$", "", token) for token in text.split()
    ]
    tokens = [token for token in tokens if token]
    if len(tokens) > 2 and tokens[-1].casefold() in _SUFFIXES:
        tokens = tokens[:-1]
    if not tokens:
        return ""
    return re.sub(r"[^a-zA-Z0-9-]+", "", tokens[-1]).strip("-").casefold()


def _programmatic_alias_owners(
    players: Iterable[Mapping[str, Any]],
    *,
    overrides: Mapping[str, str] | None = None,
) -> dict[str, tuple[Any, str]]:
    """Map normalized aliases to (owner_id, canonical_name) when unique."""
    claims: dict[str, set[Any]] = defaultdict(set)
    name_owners: dict[str, set[Any]] = defaultdict(set)
    token_owners: dict[str, set[Any]] = defaultdict(set)
    canonical_names: dict[Any, set[str]] = defaultdict(set)
    for player in players:
        name = player["player_name"]
        if not normalize_player_text(name):
            continue
        identity = player.get("player_id")
        if identity is None:
            identity = name
        canonical_names[identity].add(name)
        for spelling in observed_player_names(player):
            key = normalize_player_text(spelling)
            if not key:
                continue
            name_owners[key].add(identity)
            base = name_base(spelling)
            # Retain the older spaced spelling for apostrophized names.
            spaced = normalize_player_text(re.sub(r"['’‘ʼ]", " ", spelling))
            parts = base.split()
            raw_surname = _folded_raw_surname(spelling)
            surname_aliases = (
                (raw_surname, normalize_player_text(raw_surname)) if raw_surname else ()
            )
            for alias in (key, base, spaced, *surname_aliases):
                if not alias:
                    continue
                norm_alias = normalize_player_text(alias)
                if " " in alias or (
                    alias not in SCOPE_WORDS
                    and norm_alias not in SCOPE_WORDS
                    and not all(part in SCOPE_WORDS for part in norm_alias.split())
                    and sum(ch.isalpha() for ch in alias) >= 2
                ):
                    claims[alias].add(identity)
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

    result: dict[str, tuple[Any, str]] = {}
    for alias, owners in sorted(claims.items()):
        # A one-word alias must also respect other canonical name tokens.
        owners = owners | token_owners.get(alias, set())
        if len(owners) == 1:
            owner = next(iter(owners))
            result[alias] = (owner, min(canonical_names[owner]))
    return result


def build_programmatic_aliases(
    players: Iterable[Mapping[str, Any]],
    *,
    overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Map normalized aliases to canonical names when exactly one ID claims them."""
    return {
        alias: target
        for alias, (_, target) in _programmatic_alias_owners(
            players, overrides=overrides
        ).items()
    }


def player_aliases_for_cohort(
    players: Iterable[Mapping[str, Any]],
    *,
    overrides: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Attach safe aliases while retaining existing identity spellings."""
    roster = list(players)
    by_identity: dict[Any, set[str]] = defaultdict(set)
    for alias, (owner, _) in _programmatic_alias_owners(
        roster, overrides=overrides
    ).items():
        by_identity[owner].add(alias)
    result = []
    for player in roster:
        identity = player.get("player_id")
        if identity is None:
            identity = player["player_name"]
        result.append(
            {
                **player,
                "aliases": sorted(
                    set(player.get("aliases", ())) | by_identity[identity]
                ),
            }
        )
    return result
