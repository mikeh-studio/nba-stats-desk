"""Two-player scorecards from the same governed player-game evidence."""

from __future__ import annotations

import re
from datetime import date

from app.agent.performance_overview import (
    build_overview,
    overview_scope,
    resolve_overview_player,
)
from app.agent.semantics import SemanticError, aggregate, load_contract
from app.seasons import season_bounds


def comparison_sides(question):
    original = question.split("\nClarification:", 1)[0]
    original = original.split("\nSelected player:", 1)[0]
    if "\nCurrent question:" in original:
        return None  # Do not reinterpret an arbitrary follow-up as the old question.
    parts = re.split(r"\s+v(?:s\.?|ersus)\s+", original, flags=re.I)
    if len(parts) != 2:
        match = re.match(r"\s*compare\s+(.+?)\s+(?:and|with)\s+(.+)", original, re.I)
        if not match:
            return None
        parts = list(match.groups())
    # A baseline is not a second player, regardless of comparison wording.
    if re.match(
        r"\s*(?:(?:the|his|her|their|its)\s+)?(?:prior|previous|last|past|this|current|regular season|playoffs|20\d{2})\b",
        parts[1],
        re.I,
    ):
        return None
    return parts


def comparison_scope(question, season, today=None):
    # Never silently turn round, clutch, per-minute or last-N-game requests into
    # a whole-season average. Those need their own governed scope.
    if re.search(
        r"\b(round|finals?|clutch|home|away|wins?|losses|against|opponent|before|after|per\s+(?:36|48|100)|last\s+\d+\s+games?)\b",
        question,
        re.I,
    ):
        raise SemanticError(
            "clarification_required",
            "This scorecard supports a shared date range and regular season, playoffs, or both. Please restate the comparison with that scope.",
        )
    playoff_year = re.search(
        r"\b(20\d{2})\s+(?:playoffs?|postseason)\b", question, re.I
    )
    if playoff_year:
        year = int(playoff_year[1])
        question += f" {year - 1}-{str(year)[-2:]}"
    scope = overview_scope(question, season, today)
    scope["seasons"] = [
        s
        for s in scope["seasons"]
        if season_bounds(s)[0] <= scope["end"] and season_bounds(s)[1] >= scope["start"]
    ]
    return scope


def build_comparison(question, evidence, players, scope, selected=None, choices=None):
    sides = comparison_sides(question)
    if not sides:
        raise SemanticError(
            "clarification_required", "Name two players separated by vs."
        )
    resolved = []
    choices = dict(choices or {})
    reply = question.partition("\nClarification:")[2]
    for index, side in enumerate(sides):
        candidates = resolve_overview_player(side, players, evidence.rows)
        if str(index) in choices:
            candidates = [
                p for p in candidates if p["player_id"] == choices[str(index)]
            ]
        if len(candidates) != 1:
            narrowed = resolve_overview_player(
                side + ("\nClarification: " + reply if reply else ""),
                players,
                evidence.rows,
                selected,
            )
            if len(narrowed) == 1:
                candidates = narrowed
                selected = None
                reply = ""
        if len(candidates) != 1:
            return {
                "status": "clarification_required",
                "answer": f"Which player do you mean by ‘{side.strip()}’? Select a player or reply with their full name or ID.",
                "assumptions": [],
                "tables": [],
                "charts": [],
                "followups": [],
                "player_profile": None,
                "semantic_evidence": None,
                "metric_definitions": [],
                "comparison_choices": choices,
                "clarification_options": [
                    {**p, "label": f"{p['player_name']} (ID {p['player_id']})"}
                    for p in candidates
                ],
            }
        resolved.append(candidates[0])
        choices[str(index)] = candidates[0]["player_id"]
    if resolved[0]["player_id"] == resolved[1]["player_id"]:
        raise SemanticError(
            "clarification_required", "Please choose two different players."
        )

    summaries = [
        build_overview(p["player_name"], evidence, players, scope, p) for p in resolved
    ]
    contract = load_contract()
    profiles = []
    for player, summary in zip(resolved, summaries):
        rows = [
            r
            for r in evidence.rows
            if r["player_id"] == player["player_id"]
            and r["season_type"] in scope["phases"]
            and scope["start"]
            <= date.fromisoformat(str(r["game_date"]))
            <= scope["end"]
        ]
        minutes = aggregate(rows, contract.metric("min"), "total")
        complete = bool(rows) and not minutes["missing_component_games"]
        profiles.append(
            {
                **summary["player_profile"],
                "minutes": minutes["value"] if complete else None,
                "minutes_per_game": minutes["value"] / len(rows) if complete else None,
            }
        )
    metrics = []
    for left, right in zip(*(s["semantic_evidence"]["metrics"] for s in summaries)):
        complete = all(
            m["value"] is not None and not m["missing_component_games"]
            for m in (left, right)
        )
        delta = left["value"] - right["value"] if complete else None
        metrics.append(
            {
                "key": left["key"],
                "label": left["label"],
                "left": left,
                "right": right,
                "delta": delta,
            }
        )
    names = [p["player_name"] for p in resolved]
    higher = [
        [
            m["label"].lower()
            for m in metrics
            if m["delta"] is not None and (m["delta"] > 0 if i == 0 else m["delta"] < 0)
        ]
        for i in (0, 1)
    ]
    paragraphs = [
        "; ".join(
            f"{name} has higher per-game {', '.join(stats)}"
            for name, stats in zip(names, higher)
            if stats
        )
        + "."
        if any(higher)
        else "The available core averages do not separate these players.",
        "These category differences do not establish an overall winner. Win Shares and possession-based on/off ratings are unavailable in the governed source, so total versus per-minute winning contribution cannot yet be assessed.",
    ]
    if any(s["status"] == "no_observations" for s in summaries):
        paragraphs.insert(
            0,
            "At least one player has no recorded appearances in this scope; missing results are not zero.",
        )
    impact = [
        {
            "key": key,
            "label": label,
            "left": None,
            "right": None,
            "reason": reason,
            "definition": definition,
        }
        for key, label, reason, definition in (
            (
                "ws",
                "Win Shares",
                "Win Shares source not connected",
                "Estimated total contribution; longer runs add opportunity.",
            ),
            (
                "ws48",
                "Win Shares / 48",
                "Win Shares source not connected",
                "Estimated contribution per 48 minutes.",
            ),
            (
                "on_net",
                "On-court net rating",
                "On-court possession totals not available",
                "Team scoring margin per 100 possessions while playing.",
            ),
            (
                "off_net",
                "Off-court net rating",
                "Off-court possession totals not available",
                "Team scoring margin per 100 possessions while resting.",
            ),
            (
                "on_off",
                "On/off difference",
                "Requires both on- and off-court ratings",
                "On-court minus off-court; not isolated player impact.",
            ),
        )
    ]
    canonical = f"{names[0]} vs {names[1]} from {scope['start']} through {scope['end']} ({' + '.join(scope['phases'])})"
    return {
        "status": "ok"
        if all(s["status"] == "ok" for s in summaries)
        else "no_observations",
        "answer": "\n\n".join(paragraphs),
        "player_profile": None,
        "player_profiles": profiles,
        "clarification_options": [],
        "tables": [{"title": "Player comparison", "columns": [], "rows": []}],
        "charts": [],
        "followups": [
            canonical + ". Compare points.",
            canonical + ". Compare assists.",
        ],
        "metric_definitions": summaries[0]["metric_definitions"],
        "assumptions": [
            f"Shared dates: {scope['start']} through {scope['end']}; {' + '.join(scope['phases'])}.",
            f"Percentiles use all eligible players in these dates and phases, minimum {contract.min_games} games and complete data per metric. They are not head-to-head win probabilities.",
            "Missing components withhold comparison edges and percentiles. Partial averages show their valid-game sample.",
            f"Source: {evidence.source}; snapshot {evidence.snapshot_id}. Warehouse coverage is not independent proof of complete NBA coverage.",
            "Win Shares and on/off possession data are not connected. Raw plus/minus is not substituted for net rating. No illustrative values are used.",
        ],
        "semantic_evidence": {
            "kind": "player_comparison",
            "scope": {
                k: str(v) if isinstance(v, date) else v
                for k, v in scope.items()
                if not k.startswith("previous")
            },
            "metrics": metrics,
            "impact": impact,
            "paragraphs": paragraphs,
            "min_games": contract.min_games,
            "source": evidence.source,
            "snapshot_id": evidence.snapshot_id,
            "data_through": max(
                evidence.data_through[(s, p)]
                for s in scope["seasons"]
                for p in scope["phases"]
            ),
            "context_question": canonical,
            "preferred_metric": next(
                (
                    key
                    for key, label in (
                        ("ast", "assists"),
                        ("reb", "rebounds"),
                        ("stl", "steals"),
                        ("blk", "blocks"),
                    )
                    if re.search(r"\b" + label + r"\b", question, re.I)
                ),
                "pts",
            ),
        },
    }
