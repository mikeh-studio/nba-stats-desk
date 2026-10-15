"""Two-player scorecards from the same governed player-game evidence."""

from __future__ import annotations

import re
from datetime import date

from app.agent.performance_overview import (
    METRICS,
    build_overview,
    overview_scope,
    resolve_overview_player,
)
from app.agent.semantics import SemanticError, aggregate, load_contract
from app.seasons import season_bounds

COMPARISON_SECTIONS = (
    (
        "shooting_efficiency",
        "Shooting efficiency",
        ("fg_pct", "fg3_pct", "ft_pct", "efg_pct", "ts_pct"),
        "Percentages use total makes and attempts. Differences are percentage points. TS% uses the 0.44 free-throw approximation.",
    ),
    (
        "ball_security",
        "Ball security",
        ("tov", "ast", "min"),
        "Read turnovers alongside assists and minutes. A lower turnover average alone does not establish better playmaking.",
    ),
    (
        "per36_production",
        "Per-36 production",
        ("pts_per36", "ast_per36"),
        "Rates use 36 times total production divided by total minutes. They describe production rate, not projected output or isolated impact.",
    ),
)


def additional_comparisons(summaries, names):
    contract = load_contract()
    by_key = [
        {m["key"]: m for m in s["semantic_evidence"]["metrics"]} for s in summaries
    ]
    sections = []
    for key, title, keys, description in COMPARISON_SECTIONS:
        rows = []
        for metric_key in keys:
            left, right = (side[metric_key] for side in by_key)
            metric = contract.metric(metric_key)
            unit = left.get("unit", "per game")
            difference_unit = "percentage points" if metric.unit == "ratio" else unit
            complete = all(
                m["value"] is not None and not m["missing_component_games"]
                for m in (left, right)
            )
            delta = left["value"] - right["value"] if complete else None
            label = (
                "Turnovers"
                if metric_key == "tov"
                else "Assists"
                if metric_key == "ast"
                else "Minutes"
                if metric_key == "min"
                else metric.label
            )
            comparison = (
                "Comparison withheld: incomplete inputs or no valid denominator"
            )
            if delta is not None:
                if abs(delta) < 0.005:
                    comparison = (
                        "Tied" if delta == 0 else "Similar at displayed precision"
                    )
                elif metric_key == "min":
                    comparison = f"{names[0 if delta > 0 else 1]} plays {abs(delta):.2f} more minutes per game"
                else:
                    higher = metric.direction != "lower"
                    side = 0 if (delta > 0) == higher else 1
                    comparison = f"{names[side]} is {'higher' if higher else 'lower'} by {abs(delta):.2f} {difference_unit}"
            rows.append(
                {
                    "key": metric_key,
                    "label": label,
                    "left": left,
                    "right": right,
                    "delta": delta,
                    "unit": unit,
                    "difference_unit": difference_unit,
                    "direction": metric.direction,
                    "comparison": comparison,
                }
            )
        sections.append(
            {"key": key, "title": title, "description": description, "metrics": rows}
        )
    return sections


def comparison_sides(question):
    from app.agent.overview_positions import strip_peer_clause

    original = strip_peer_clause(question.split("\nClarification:", 1)[0])
    original = original.split("\nSelected player:", 1)[0]
    if "\nCurrent question:" in original:
        return None  # Do not reinterpret an arbitrary follow-up as the old question.
    parts = re.split(r"\s+v(?:s\.?|ersus)\s+", original, flags=re.I)
    if len(parts) != 2:
        match = re.match(r"\s*compare\s+(.+?)\s+(?:and|with)\s+(.+)", original, re.I)
        if not match:
            return None
        parts = list(match.groups())
    # A dated month is a period comparison, not a second player identity.
    from app.agent.overview_calendar import MONTH_PATTERN

    if re.match(r"\s*(?:in\s+)?" + MONTH_PATTERN + r"\s+20\d{2}\b", parts[1], re.I):
        return None
    # A baseline is not a second player, regardless of comparison wording.
    if re.match(
        r"\s*(?:(?:the|his|her|their|its)\s+)?(?:prior|previous|last|past|this|current|regular season|playoffs?|postseason|home|away|road|before|after|20\d{2})\b",
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
        selected_games = set(summary["semantic_evidence"]["game_ids"])
        rows = [
            r
            for r in evidence.rows
            if r["player_id"] == player["player_id"]
            and r["game_id"] in selected_games
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
    comparison_keys = {key for key, _ in METRICS}
    for left, right in zip(*(s["semantic_evidence"]["metrics"] for s in summaries)):
        # The comparison scorecard is per-game production, not overview context.
        if left["key"] not in comparison_keys:
            continue
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
    sections = additional_comparisons(summaries, names)
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
        "These box-score differences do not establish an overall winner. Playing time, roles, teammates and opponents affect the comparison.",
    ]
    summary_keys = {
        "shooting_efficiency": ("ts_pct",),
        "ball_security": ("tov",),
        "per36_production": ("pts_per36", "ast_per36"),
    }
    for section in sections:
        takeaways = [
            f"{m['label']}: {m['comparison']}"
            for m in section["metrics"]
            if m["key"] in summary_keys[section["key"]]
        ]
        paragraphs.insert(
            -1,
            section["title"]
            + ". "
            + "; ".join(takeaways)
            + ". "
            + section["description"],
        )
    if any(s["status"] == "no_observations" for s in summaries):
        paragraphs.insert(
            0,
            "At least one player has no recorded appearances in this scope; missing results are not zero.",
        )
    # Optional impact measures stay absent until a verified matching-scope source is connected.
    impact = []
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
            "Comparisons describe recorded box-score production, not isolated player impact.",
            "Additional sections retain each metric's valid/observed appearances. Partial inputs suppress differences. Shooting rates use aggregate attempts, not averaged game percentages; per-36 rates use total minutes.",
        ],
        "semantic_evidence": {
            "kind": "player_comparison",
            "scope": {
                k: str(v) if isinstance(v, date) else v
                for k, v in scope.items()
                if not k.startswith("previous")
            },
            "metrics": metrics,
            "comparison_sections": sections,
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
