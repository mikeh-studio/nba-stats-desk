"""Evidence-only similarity and league-reference answers. No narrative model calls."""

from __future__ import annotations

import hashlib
import json
import math
import re
from time import monotonic
from typing import Any

from app.agent.availability_ask import METRICS, _identities
from app.agent.payload import answer_payload
from app.agent.performance_overview import identity_profile
from app.agent.semantic_serving import requested_seasons, source_players
from app.agent.semantics import (
    Query,
    SemanticError,
    aggregate,
    load_contract,
    run_query,
)

BASELINE = r"\b(?:league\s+(?:average|baseline)|against\s+(?:the\s+)?baseline)\b"
SIMILARITY = r"\b(?:similar(?:ity)?|resembles?|like)\b"


def reference_kind(question: str) -> str | None:
    if re.search(BASELINE, question, re.I):
        return "league_baseline"
    if re.search(
        r"\b(?:similar(?:ity)?|resembles?)\b|\bplayers? like\b", question, re.I
    ):
        return "similarity"
    return None


def reference_followup(question: str, context: dict[str, Any]) -> str | None:
    previous = context.get("reference_question")
    if not previous:
        return None
    match = re.fullmatch(r"(?:what|how) about (.+?)\??", question.strip(), re.I)
    if not match:
        return None
    metric = match[1]
    if reference_kind(previous) == "league_baseline" and any(
        re.fullmatch(pattern, metric, re.I) for pattern in METRICS.values()
    ):
        for pattern in METRICS.values():
            previous = re.sub(
                r"(?<!\w)(?:" + pattern + r")(?!\w)", " ", previous, flags=re.I
            )
        return previous + " " + metric
    return None


def parse_reference(question, players, season, kind):
    if re.search(r"\bplayer\d+\b", question, re.I):
        raise SemanticError("clarification_required", "Use a player's full name.")
    text, named = _identities(question, players)
    if len(named) != 1:
        raise SemanticError(
            "clarification_required",
            "Name one player by their full name for this comparison.",
        )
    player = next(iter(named.values()))
    seasons = requested_seasons(question, season)
    if len(seasons) != 1:
        raise SemanticError("unsupported_scope", "Use one season for this comparison.")
    text = re.sub(r"player\d+|(?:20\d{2})[-–/](?:20)?\d{2}(?![-/]\d)", " ", text)
    text = re.sub(r"[’']s\b", " ", text)
    if kind == "similarity":
        count = re.search(r"\b(?:top|show|find)\s+(\d+)\b", text)
        limit = int(count[1]) if count else 5
        if not 1 <= limit <= 6:
            raise SemanticError(
                "unsupported_scope",
                "Published similarity supports one to six neighbors.",
            )
        if count:
            text = text[: count.start()] + " " + text[count.end() :]
        text = re.sub(SIMILARITY, " ", text)
        text = re.sub(
            r"\b(?:who|which|are|is|the|most|players?|to|show|find|me|for|in|season|this)\b",
            " ",
            text,
        )
        if text.strip(" ?.!,"):
            raise SemanticError(
                "unsupported_scope",
                "Similarity uses the published season profile; custom periods, metrics and filters are not supported.",
            )
        return player, seasons[0], limit

    text = re.sub(BASELINE, " ", text)
    metrics = []
    for key, pattern in METRICS.items():
        text, count = re.subn(
            r"(?<!\w)(?:" + pattern + r")(?!\w)", " ", text, flags=re.I
        )
        if count:
            metrics.append(key)
    if len(metrics) != 1:
        raise SemanticError(
            "clarification_required",
            "Specify one metric, such as points or FG%, for the league baseline comparison.",
        )
    metric = load_contract().metric(metrics[0])
    scope: dict[str, Any] = {}
    dates = re.search(
        r"\bfrom (\d{4}-\d{2}-\d{2}) (?:through|to) (\d{4}-\d{2}-\d{2})\b", text
    )
    if dates:
        scope.update(window="date_range", start_date=dates[1], as_of=dates[2])
        text = text[: dates.start()] + " " + text[dates.end() :]
    games = re.search(r"\blast (\d+) games?\b", text)
    if games:
        if dates:
            raise SemanticError(
                "unsupported_scope",
                "Choose an appearance window or date range, not both.",
            )
        scope.update(window="last_n_games", n=int(games[1]))
        text = text[: games.start()] + " " + text[games.end() :]
    elif re.search(r"\brecent\b", text):
        scope.update(window="last_n_games", n=10)
        text = re.sub(r"\brecent\b", " ", text)
    phase = (
        "Playoffs"
        if re.search(r"\b(?:playoffs?|postseason)\b", text)
        else "Regular Season"
    )
    if phase == "Playoffs" and re.search(r"regular[ -]season", text):
        raise SemanticError(
            "unsupported_scope", "Choose one phase for the league baseline comparison."
        )
    text = re.sub(r"regular[ -]season|\b(?:playoffs?|postseason)\b", " ", text)
    text = re.sub(
        r"\b(?:compare|compared|show|tell|me|how|did|does|perform|performed|against|versus|vs|with|to|the|for|in|season|this|per game|average|averaging|trend|his)\b",
        " ",
        text,
    )
    if text.strip(" ?.!,"):
        raise SemanticError(
            "unsupported_scope",
            "This league baseline supports one metric, season, phase, and an optional last-N appearance window or explicit date range. Other conditions are not supported.",
        )
    aggregation = "ratio" if metric.denominator else "average"
    query = Query(
        metric.key,
        seasons[0],
        aggregation,
        season_type=phase,
        player_id=player["player_id"],
        min_games=1,
        **scope,
    )
    query.validate(metric)
    return player, seasons[0], query


def build_baseline(evidence, player, query):
    """Appearance-weighted league baseline across the player's calendar window.

    No top-N truncation: each identity is queried separately under one scope.
    A missing component anywhere withholds the league comparison, not the row.
    """
    current = run_query(evidence, query)
    row = next(iter(current["rows"]), None)
    if not row:
        raise SemanticError(
            "no_observations", "No player appearances match the requested scope."
        )
    player_games = [
        r
        for r in evidence.rows
        if r["player_id"] == player["player_id"]
        and r["season"] == query.season
        and r["game_id"] in row["game_ids"]
    ]
    # Appearance windows become a shared calendar window for league peers.
    start = (
        min(r["game_date"] for r in player_games)
        if query.window == "last_n_games"
        else current["scope"]["window_start"]
    )
    end = current["scope"]["as_of"]
    peers = [
        r
        for r in evidence.rows
        if r["season"] == query.season
        and r["season_type"] == query.season_type
        and (start is None or r["game_date"] >= start)
        and r["game_date"] <= end
    ]
    metric = load_contract().metric(query.metric)
    baseline = aggregate(peers, metric, query.aggregation)
    complete = all(
        v["value"] is not None and v["missing_component_games"] == 0
        for v in (row, baseline)
    )
    scale = 100 if metric.unit == "ratio" else 1
    difference = (row["value"] - baseline["value"]) * scale if complete else None
    return {
        "kind": "league_baseline",
        "contract_version": "league_baseline/1",
        "player_id": player["player_id"],
        "metric": metric.public(),
        "scope": current["scope"],
        "player": row,
        "baseline": baseline,
        "difference": difference,
        "difference_unit": "percentage points"
        if scale == 100
        else f"{metric.label} per game",
        "baseline_scope": {
            "season": query.season,
            "season_type": query.season_type,
            "start_date": start,
            "end_date": end,
            "weighting": "pooled_components"
            if metric.denominator
            else "player_appearance",
            "population": "all recorded player appearances, including the focal player",
            "minimum_appearances": 1,
            "player_count": len({r["player_id"] for r in peers}),
        },
        "membership": [
            {
                "season": r["season"],
                "game_id": r["game_id"],
                "player_id": r["player_id"],
            }
            for r in peers
        ],
        "provenance": current["provenance"],
        "warnings": current["warnings"]
        + ([row["sample_warning"]] if row["sample_warning"] else []),
    }


def build_similarity(repo, player, season, limit):
    detail = repo.get_player_detail(player["player_id"])
    identity = (detail or {}).get("player", {})
    if (
        not detail
        or identity.get("player_id") != player["player_id"]
        or identity.get("player_name") != player["player_name"]
        or identity.get("season") != season
    ):
        raise SemanticError(
            "invalid_evidence",
            "The similarity profile does not match the requested identity and season.",
        )
    rows = detail.get("similar_players") or []
    if not rows or detail.get("panel_states", {}).get("similarity") == "unavailable":
        raise SemanticError(
            "unsupported_coverage",
            "Published similarity evidence is unavailable for this player.",
        )
    seen = {player["player_id"]}
    for row in rows:
        score = row.get("similarity_score")
        pid = row.get("player_id")
        if (
            type(pid) is not int
            or pid <= 0
            or pid in seen
            or not isinstance(row.get("player_name"), str)
            or not row["player_name"].strip()
            or type(score) not in (int, float)
            or not math.isfinite(score)
            or not 0 <= score <= 1
        ):
            raise SemanticError(
                "invalid_evidence",
                "Published similarity contains invalid identities or scores.",
            )
        seen.add(pid)
    ordered = sorted(rows, key=lambda r: (-r["similarity_score"], r["player_name"]))[
        :limit
    ]
    return {
        "kind": "similarity",
        "contract_version": "similarity_reference/1",
        "player_id": player["player_id"],
        "scope": {
            "season": season,
            "limit": limit,
            "basis": "published season feature profile",
        },
        "rows": [
            {k: r[k] for k in ("player_id", "player_name", "similarity_score")}
            for r in ordered
        ],
        "provenance": {
            "source": "repository/player_similarity_features",
            "snapshot_id": hashlib.sha256(
                json.dumps(rows, sort_keys=True).encode()
            ).hexdigest(),
            "score_definition": "1 / (1 + Euclidean distance between normalized feature vectors)",
            "model_version": None,
            "version_note": "The serving response does not publish a model build version; the hash identifies these returned neighbors.",
        },
    }


def render_reference(evidence, player):
    payload = answer_payload(
        "",
        status="ok",
        semantic_evidence=evidence,
        player_profile=identity_profile(player, []),
    )
    if evidence["kind"] == "similarity":
        payload["answer"] = (
            f"{evidence['rows'][0]['player_name']} is the closest published match to {player['player_name']} in {evidence['scope']['season']} (similarity score {evidence['rows'][0]['similarity_score']:.4f}). This describes the season feature profile, not a probability or a prediction."
        )
        payload["tables"] = [
            {
                "title": "Published similarity neighbors",
                "columns": [
                    {"key": "player", "label": "Player"},
                    {"key": "score", "label": "Similarity score (0–1)"},
                ],
                "rows": [
                    [r["player_name"], f"{r['similarity_score']:.4f}"]
                    for r in evidence["rows"]
                ],
            }
        ]
        payload["assumptions"] = [
            evidence["provenance"]["score_definition"],
            evidence["provenance"]["version_note"],
        ]
    else:
        metric = evidence["metric"]
        unit = "%" if metric["unit"] == "ratio" else " per game"

        def fmt(v):
            return "Unavailable" if v is None else f"{v:.1f}{unit}"

        a, b, delta = evidence["player"], evidence["baseline"], evidence["difference"]
        payload["answer"] = (
            f"{player['player_name']}: {fmt(a['display_value'])} {metric['label']}; the league baseline is {fmt(b['display_value'])}. "
            + (
                "The difference is unavailable because a comparison sample is incomplete."
                if delta is None
                else f"Player minus league: {delta:+.1f} {evidence['difference_unit']}."
            )
        )
        payload["answerability"] = "partial" if delta is None else "full"
        payload["metric_definitions"] = [metric]
        payload["tables"] = [
            {
                "title": f"{metric['label']} — player versus league",
                "columns": [
                    {"key": k, "label": label}
                    for k, label in (
                        ("group", "Group"),
                        ("value", f"Value ({unit.strip()})"),
                        ("observed", "Player appearances in scope"),
                        ("valid", "Appearances with complete inputs"),
                    )
                ],
                "rows": [
                    [
                        label,
                        fmt(v["display_value"]),
                        str(v["observed_games"]),
                        str(v["valid_games"]),
                    ]
                    for label, v in ((player["player_name"], a), ("League baseline", b))
                ],
            }
        ]
        scope = evidence["baseline_scope"]
        payload["assumptions"] = [
            f"{scope['season']} · {scope['season_type']} · {scope['start_date'] or 'season start'} through {scope['end_date']}.",
            "League baseline: all recorded player appearances including this player; minimum one appearance. Count averages are appearance-weighted; ratios pool their components. This is not the average of player averages.",
            "Incomplete inputs suppress the difference; individual partial averages use only complete observations.",
            *evidence["warnings"],
        ]
    return payload


def answer_reference(
    agent, question, kind, conversation_id=None, trace=None, selected_player=None
):
    started = monotonic()
    original = question
    store = agent.conversation_store if conversation_id else None
    context = {}
    inherited_reference = False
    pending = store.get_pending_clarification(conversation_id) if store else None
    if (
        pending
        and reference_kind(pending.question) == kind
        and not reference_kind(question)
    ):
        question = (
            pending.question
            + " "
            + (
                str(selected_player.get("player_name", ""))
                if selected_player
                else question
            )
        )
    if store:
        turns = store.get_turns(
            conversation_id, max_turns=agent.settings.agent_conversation_max_turns
        )
        context = next((t.context for t in reversed(turns) if t.context), {})
        followup = reference_followup(question, context)
        inherited_reference = followup is not None
        question = followup or question
    try:
        seasons = requested_seasons(question, agent.settings.season)
        if agent.semantic_agent is None:
            raise SemanticError(
                "unsupported_coverage",
                "A governed statistics source is required for this comparison.",
            )
        if kind == "similarity" and seasons != [agent.settings.season]:
            raise SemanticError(
                "unsupported_coverage",
                "The similarity publication must match the selected season.",
            )
        _, source = agent.semantic_agent.warehouse.load(seasons)
        source.validate()
        players = source_players(source)
        if selected_player:
            chosen = next(
                (
                    p
                    for p in players
                    if p["player_id"] == selected_player.get("player_id")
                    and p["player_name"] == selected_player.get("player_name")
                ),
                None,
            )
            if chosen is None:
                raise SemanticError(
                    "invalid_context", "Selected identity does not match this source."
                )
            players = [
                p
                for p in players
                if p["player_id"] == chosen["player_id"]
                or p["player_name"] != chosen["player_name"]
            ]
        player, season, spec = parse_reference(
            question, players, agent.settings.season, kind
        )
        if (
            inherited_reference
            and context.get("browser_recovered")
            and any(
                hint.get("player_id") != player["player_id"]
                or hint.get("player_name") != player["player_name"]
                for hint in context.get("players", [])
            )
        ):
            raise SemanticError(
                "invalid_context",
                "Saved reference identity does not match this source. Restate the full name and scope.",
            )
        evidence = (
            build_similarity(agent.repo, player, season, spec)
            if kind == "similarity"
            else build_baseline(source, player, spec)
        )
        payload = render_reference(evidence, player)
        if kind == "league_baseline" and evidence["difference"] is not None:
            metric = evidence["metric"]
            unit = "%" if metric["unit"] == "ratio" else "per game"
            payload["charts"] = [
                {
                    "type": "bar",
                    "title": f"{metric['label']} — player versus league",
                    "x_label": "Group",
                    "y_label": f"{metric['label']} ({unit})",
                    "series": [
                        {
                            "key": metric["key"],
                            "label": metric["label"],
                            "points": [
                                {
                                    "x": label,
                                    "y": group["display_value"],
                                    "meta": f"{group['valid_games']} complete player appearances",
                                }
                                for label, group in (
                                    (player["player_name"], evidence["player"]),
                                    ("League baseline", evidence["baseline"]),
                                )
                            ],
                        }
                    ],
                }
            ]
        payload["conversation_context"] = {
            "question": original,
            "reference_question": question,
            "players": [{k: player[k] for k in ("player_id", "player_name")}],
            "scope": evidence["scope"],
            "metrics": [evidence["metric"]["key"]] if kind == "league_baseline" else [],
        }
    except SemanticError as exc:
        payload = answer_payload(str(exc), status=exc.code)
        if exc.code == "clarification_required" and "players" in locals():
            from app.agent.performance_overview import resolve_overview_player

            options = resolve_overview_player(question, players, source.rows)
            payload["clarification_options"] = [
                dict(p, label=f"{p['player_name']} (ID {p['player_id']})")
                for p in options
            ]
    payload.update(
        conversation_id=conversation_id,
        agent_plan={
            "route": kind,
            "confidence": 1.0,
            "needs_clarification": payload["status"] == "clarification_required",
        },
        semantic_plan={"kind": kind, "model_calls": 0},
    )
    record = {
        "name": "governed_reference",
        "args": {"kind": kind},
        "status": payload["status"],
        "duration_ms": round((monotonic() - started) * 1000),
        "result": {"status": payload["status"]},
    }
    payload["tool_calls"] = [record]
    if trace:
        trace.add_tool(**record)
        trace.set_plan(route=kind, confidence=1.0)
        trace.outcome = "answered" if payload["status"] == "ok" else payload["status"]
    if store:
        if payload["status"] == "clarification_required":
            store.set_pending_clarification(
                conversation_id, question=question, query_plan={"kind": kind}
            )
        else:
            store.clear_pending_clarification(conversation_id)
        if payload["status"] == "ok":
            store.append_turn(
                conversation_id,
                question=original,
                answer=payload["answer"],
                max_turns=agent.settings.agent_conversation_max_turns,
                context=payload["conversation_context"],
            )
    return payload
