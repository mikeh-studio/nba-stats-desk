"""Deterministic recent statistical neighbors from complete player appearances."""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict

from app.agent.execution import timed
from app.agent.semantics import SemanticError

LABELS = {"pts": "PTS", "reb": "REB", "ast": "AST"}


@timed("calculation")
def build_recent_similarity(source, player, season, spec):
    source.validate()
    phase, n, metrics = spec["season_type"], spec["n"], spec["metrics"]
    scope_key = (season, phase)
    if scope_key not in source.covered_scopes:
        raise SemanticError(
            "unsupported_coverage", "This season and phase are not covered."
        )
    cutoff = source.data_through[scope_key]
    groups = defaultdict(list)
    for row in source.rows:
        if (
            row["season"] == season
            and row["season_type"] == phase
            and str(row["game_date"]) <= cutoff
        ):
            groups[row["player_id"]].append(row)
    profiles = []
    for pid, rows in groups.items():
        games = sorted(
            rows, key=lambda r: (str(r["game_date"]), r["game_id"]), reverse=True
        )[:n]
        complete = len(games) == n and all(
            type(r.get(m)) in (int, float) and math.isfinite(r[m])
            for r in games
            for m in metrics
        )
        if not complete:
            if pid == player["player_id"]:
                raise SemanticError(
                    "insufficient_sample",
                    f"This comparison needs {n} recorded appearances with complete values for every selected metric. Missing values are not zero.",
                )
            continue
        profiles.append(
            {
                "player_id": pid,
                "player_name": games[0]["player_name"],
                "values": {m: sum(r[m] for r in games) / n for m in metrics},
                "games": n,
                "start_date": str(games[-1]["game_date"]),
                "end_date": str(games[0]["game_date"]),
                "game_ids": sorted(r["game_id"] for r in games),
            }
        )
    anchor = next((p for p in profiles if p["player_id"] == player["player_id"]), None)
    if anchor is None:
        raise SemanticError(
            "no_observations", "No player appearances match this scope."
        )
    if len(profiles) < 2:
        raise SemanticError(
            "insufficient_sample", "No other players have a complete comparison window."
        )
    means = {m: sum(p["values"][m] for p in profiles) / len(profiles) for m in metrics}
    scales = {
        m: math.sqrt(
            sum((p["values"][m] - means[m]) ** 2 for p in profiles) / len(profiles)
        )
        for m in metrics
    }
    neighbors = []
    for p in profiles:
        if p["player_id"] == player["player_id"]:
            continue
        distance = math.sqrt(
            sum(
                ((p["values"][m] - anchor["values"][m]) / scales[m]) ** 2
                if scales[m]
                else 0
                for m in metrics
            )
            / len(metrics)
        )
        neighbors.append(
            dict(p, distance=distance, similarity_score=1 / (1 + distance))
        )
    neighbors.sort(key=lambda p: (p["distance"], p["player_id"]))
    membership = sorted(
        (p["player_id"], gid) for p in profiles for gid in p["game_ids"]
    )
    return {
        "kind": "recent_statistical_similarity",
        "contract_version": "recent_statistical_similarity/1",
        "player_id": player["player_id"],
        "player": anchor,
        "scope": {
            "season": season,
            "season_type": phase,
            "window": "last_n_games",
            "n": n,
            "as_of": cutoff,
        },
        "metrics": metrics,
        "metrics_defaulted": spec["metrics_defaulted"],
        "cohort": {
            "size": len(profiles),
            "minimum_games": n,
            "means": means,
            "standard_deviations": scales,
            "membership_sha256": hashlib.sha256(
                json.dumps(membership, separators=(",", ":")).encode()
            ).hexdigest(),
        },
        "rows": neighbors[: spec["limit"]],
        "provenance": {
            "source": source.source,
            "snapshot_id": source.snapshot_id,
            "score_definition": "1 / (1 + root-mean-square distance in cohort-standardized per-game metrics); equal metric weights; zero-variance metrics contribute zero.",
        },
    }


@timed("rendering")
def render_recent_similarity(evidence, player):
    from app.agent.payload import answer_payload

    metrics = evidence["metrics"]
    names = ", ".join(LABELS[m] for m in metrics)
    scope = evidence["scope"]
    first = evidence["rows"][0]
    assumed = (
        "Since you did not specify metrics, I used"
        if evidence["metrics_defaulted"]
        else "I used the requested metrics:"
    )
    payload = answer_payload(
        f"{first['player_name']} is the closest recent statistical match to {player['player_name']} among {evidence['cohort']['size'] - 1} eligible peers. "
        f"{assumed} {names} per game over each player's last {scope['n']} recorded {scope['season_type'].lower()} appearances in {scope['season']}. "
        "Metrics are standardized across eligible players and weighted equally. This is a recent statistical comparison, separate from the published season-profile model; it does not establish the same playing style. "
        "Would you like different metrics or a different window?",
        status="ok",
        semantic_evidence=evidence,
    )
    payload["tables"] = [
        {
            "title": "Statistical Profile Similarity — recent appearances",
            "columns": [
                {"key": "player", "label": "Player"},
                *[{"key": m, "label": f"{LABELS[m]} / game"} for m in metrics],
                {"key": "sample", "label": "Recorded appearances"},
                {"key": "dates", "label": "Sample dates"},
                {"key": "score", "label": "Profile similarity score"},
            ],
            "rows": [
                [
                    p["player_name"],
                    *[f"{p['values'][m]:.1f}" for m in metrics],
                    str(p["games"]),
                    f"{p['start_date']} – {p['end_date']}",
                    "Reference"
                    if p is evidence["player"]
                    else f"{p['similarity_score']:.4f}",
                ]
                for p in [evidence["player"], *evidence["rows"]]
            ],
        }
    ]
    payload["assumptions"] = [
        f"Source through {scope['as_of']}. Each player's own last {scope['n']} appearances may span different dates; no shared calendar window is implied.",
        f"Only players with {scope['n']} complete appearances qualify; the reference player is included when calculating cohort scales. Missing values and short windows are excluded, not filled with zeros.",
        evidence["provenance"]["score_definition"],
        "The score is not a probability, player-quality grade, or the published season-profile score. Methodology: /similarity-methodology",
    ]
    payload["followups"] = [
        f"Who is similar to {player['player_name']} in points over the last {scope['n']} games in {scope['season']} {scope['season_type'].lower()}?"
    ]
    from app.agent.claim_renderers import reference_tables

    reference_tables(payload, player)
    return payload
