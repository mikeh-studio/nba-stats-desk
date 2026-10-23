"""Conservative, source-roster name matching shared by Ask routes."""

import re
from difflib import SequenceMatcher

from app.agent.player_resolver import normalize_player_text

# Exclude question/scope words so prose cannot become a player-name candidate.
_CONTEXT_WORDS = frozenset(
    "how did does do has have had is was were he she his her they their it its "
    "play played playing perform performed performing performance doing done "
    "compare compares compared comparing with to and vs versus than who better "
    "last this current previous prior season seasons regular playoffs playoff "
    "postseason year years games game show me tell about points rebounds assists "
    "steals blocks stats statistics average averaged per in on at of the a an "
    "for from through between during since until please overview summary "
    "can could would you give review an analysis of among guards wings centers forwards "
    "scoring rebounding playmaking shooting efficiency fare been assist rebound steal block "
    "three threes leaders leader christmas day".split()
)


_SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}


def name_base(name):
    parts = normalize_player_text(name).split()
    return " ".join(parts[:-1] if len(parts) > 2 and parts[-1] in _SUFFIXES else parts)


def suggestion_prompt(players):
    if len(players) == 1:
        return (
            f"Did you mean {players[0]['player_name']}? "
            "Select the name to confirm, or enter the player's full name."
        )
    return (
        "I found close player-name matches. Which player did you mean? "
        "Select a name to confirm, or enter the player's full name."
    )


def _minor_change(a, b):
    """One insertion/deletion/substitution or adjacent transposition."""
    if min(len(a), len(b)) < 4 or a == b:
        return False
    if len(a) == len(b):
        changed = [i for i, (x, y) in enumerate(zip(a, b)) if x != y]
        return len(changed) == 1 or (
            len(changed) == 2
            and changed[1] == changed[0] + 1
            and a[changed[0]] == b[changed[1]]
            and a[changed[1]] == b[changed[0]]
        )
    if len(a) > len(b):
        a, b = b, a
    return len(b) == len(a) + 1 and any(b[:i] + b[i + 1 :] == a for i in range(len(b)))


def _rank_matches(question, players):
    """Rank all credible IDs before applying any display limit.

    Compare ordered name tokens against bounded question spans. Full-name
    matches take precedence over partial names. Thresholds are spelling
    heuristics, not calibrated probabilities. Full-name minor edits can resolve
    only with a clear margin; partial names never auto-resolve.
    """
    original = re.sub(r"['’]s\b", "", question, flags=re.I).split(
        "\nSelected player:", 1
    )[0]
    original, _, reply = original.partition("\nClarification:")
    tokens = [
        t
        for t in normalize_player_text(reply or original).split()
        if t.isalpha() and t not in _CONTEXT_WORDS
    ][:80]
    full, partial = [], []
    for player in players:
        best_full = best_partial = 0.0
        minor = False
        matched = ""
        names = (player["player_name"], *player.get("aliases", []))
        for name in dict.fromkeys(
            n for value in names for n in (value, name_base(value))
        ):
            parts = normalize_player_text(name).split()
            if not parts or len(parts) > 5:
                continue
            if len(parts) > 1:
                for start in range(len(tokens) - len(parts) + 1):
                    span = tokens[start : start + len(parts)]
                    scores = [
                        SequenceMatcher(None, a, b).ratio() for a, b in zip(span, parts)
                    ]
                    score = sum(scores) / len(scores)
                    if min(scores) >= 0.65 and score >= 0.8:
                        if score > best_full:
                            best_full = score
                            changed = [(a, b) for a, b in zip(span, parts) if a != b]
                            minor = len(changed) == 1 and _minor_change(*changed[0])
                            matched = " ".join(span)
            for part in parts:
                if len(part) < 4 or part in _CONTEXT_WORDS:
                    continue
                for token in tokens:
                    if len(token) < 4:
                        continue
                    score = SequenceMatcher(None, token, part).ratio()
                    if score >= 0.88:
                        best_partial = max(best_partial, score)
        if best_full:
            full.append((best_full, player, minor, matched))
        if best_partial and len(tokens) == 1:
            partial.append((best_partial, player, False, tokens[0]))
    ranked = sorted(
        full or partial,
        key=lambda item: (
            -item[0],
            normalize_player_text(item[1]["player_name"]),
            item[1]["player_id"],
        ),
    )
    seen, result = set(), []
    for score, player, minor, matched in ranked:
        if player["player_id"] not in seen:
            seen.add(player["player_id"])
            result.append((score, player, minor, matched))
    return tokens, result


def suggest_players(question, players, *, limit=5):
    _, ranked = _rank_matches(question, players)
    return [
        {**p, "match_method": "spelling_suggestion"}
        for _, p, _, _ in ranked[: max(0, min(5, limit))]
    ]


def match_player_name(question, players):
    """Fallback after exact matching; never infer an unobserved identity."""
    tokens, ranked = _rank_matches(question, players)
    if ranked:
        score, player, minor, matched = ranked[0]
        margin = score - ranked[1][0] if len(ranked) > 1 else 1.0
        suffix_match = matched == name_base(
            player["player_name"]
        ) and matched != normalize_player_text(player["player_name"])
        if (minor or suffix_match) and score >= 0.9 and margin >= 0.08:
            return {
                "status": "ok",
                "matches": [player],
                "notice": (
                    f"Using {player['player_name']} for ‘{matched}’."
                    if suffix_match
                    else f"Using {player['player_name']} for the minor misspelling ‘{matched}’."
                ),
                "resolution": {
                    "policy": "player_name_match/1",
                    "method": "name_suffix" if suffix_match else "minor_typo",
                    "input_name": matched,
                    "player_id": player["player_id"],
                    "player_name": player["player_name"],
                },
            }
        return {
            "status": "clarification_required",
            "matches": [p for _, p, _, _ in ranked[:5]],
            "message": "Several player names could match. Select the intended player; I won't choose between them."
            if len(ranked) > 1
            else suggestion_prompt([ranked[0][1]]),
        }
    return {
        "status": "not_found" if tokens else "clarification_required",
        "matches": [],
        "message": "No matching player is available in this NBA dataset for the requested scope."
        if tokens
        else "Which player do you mean? Please enter their full name.",
    }


def resolve_player_name(name, players):
    """Exact canonical/curated aliases first; rank every credible ID otherwise."""
    key = normalize_player_text(name)
    matches = {
        p["player_id"]: p
        for p in players
        if key
        and key
        in {normalize_player_text(n) for n in (p["player_name"], *p.get("aliases", []))}
    }
    # A suffix-free exact name must not hide a same-base suffixed identity.
    if key and name_base(key) == key:
        variants = {
            p["player_id"]: p for p in players if name_base(p["player_name"]) == key
        }
        if matches:
            matches.update(variants)
    if matches:
        # An explicit one-word alias must not hide another player with that
        # same first/last name (e.g. a roster containing several Jalens).
        if len(key.split()) == 1:
            matches.update(
                {
                    p["player_id"]: p
                    for p in players
                    if key in normalize_player_text(p["player_name"]).split()
                }
            )
        result = {
            "status": "ok" if len(matches) == 1 else "clarification_required",
            "matches": [matches[k] for k in sorted(matches)],
        }
        if len(matches) > 1:
            result["message"] = (
                "That name identifies more than one player. Select the intended player; I won't choose between them."
            )
        return result
    return match_player_name(name, players)
