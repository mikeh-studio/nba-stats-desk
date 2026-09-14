"""Ask-compatible answers from governed evidence, without model-authored numbers."""

from __future__ import annotations

import re
from dataclasses import replace
from time import monotonic
from typing import Any

from app.agent.followup import analysis_context, resolve_followup
from app.agent.history import saved_context_question
from app.agent.performance_overview import (
    build_overview,
    identity_profile,
    overview_scope,
    resolve_overview_player,
    wants_overview,
)
from app.agent.player_comparison import (
    build_comparison,
    comparison_scope,
    comparison_sides,
)
from app.agent.semantic_planner import execute_plan, explicit_scope, plan_question
from app.agent.semantic_serving import requested_seasons, source_players
from app.agent.semantics import Query, SemanticError, run_query


def contextual_leader_plan(question, context, season):
    """A supported contextual leaderboard has defaults, not missing parameters."""
    resolved, hints = resolve_followup(question, context)
    if len(hints.get("excluded_player_ids", [])) != 1 or not hints.get("assumption"):
        return None
    # Keep this route deliberately bounded: modifiers such as team, turnovers,
    # per-minute rates or new time windows still use the general planner.
    body = question.partition(",")[2].strip().rstrip("?. ")
    match = re.fullmatch(
        r"(?:who are\s+)?(?:the\s+|other\s+)*(?:top|leading)\s+(?:(\d+|few)\s+)?(?:playmaking leads|playmakers|playmaking leaders)",
        body,
        re.I,
    )
    scope = explicit_scope(resolved)
    if not match or scope.get("window") != "date_range":
        return None
    limit = int(match[1]) if match[1] and match[1].isdigit() else 5
    if not 1 <= limit <= 100:
        return None
    if "season_type" not in scope:
        scope["season_type"] = (
            "Playoffs" if re.search(r"playoffs?", resolved, re.I) else "Regular Season"
        )
    return {
        "status": "query",
        "model_calls": 0,
        "message": "Top playmakers by assists per game, with the prior player as reference.",
        "queries": [
            {
                "metric": "ast",
                "season": season,
                "aggregation": "average",
                "operation": "rank",
                "player_name": None,
                "limit": limit,
                **scope,
            }
        ],
    }


def normalize_reference_plan(plan, reference_ids, players):
    """Collapse only equivalent leaderboards or a same-scope contextual summary."""
    if plan.get("status") != "compare" or len(reference_ids) != 1:
        return plan
    queries = plan.get("queries", [])
    ranks = [q for q in queries if q.get("operation") == "rank"]
    if len(queries) != 2 or not ranks:
        return plan
    rank = ranks[0]
    names = {
        p["player_name"].casefold() for p in players if p["player_id"] in reference_ids
    } | {f"resolved_player_{reference_ids[0]}"}
    ignored = {"operation", "player_name", "limit"}
    for query in queries:
        if query.get("operation") not in {"rank", "summary"}:
            return plan
        if query.get("player_name") and query["player_name"].casefold() not in names:
            return plan
        if query.get("operation") == "summary" and not query.get("player_name"):
            return plan
        if {k: v for k, v in query.items() if k not in ignored} != {
            k: v for k, v in rank.items() if k not in ignored
        }:
            return plan
    return {**plan, "status": "query", "queries": [dict(rank)]}


def add_leaderboard_reference(payload, result, evidence, players, player_id):
    """Compare leaderboard values with one player under precisely the same scope."""
    query = Query(**result["resolved_queries"][0])
    reference = run_query(
        evidence,
        replace(
            query, operation="summary", player_id=player_id, excluded_player_ids=None
        ),
    )
    payload["reference_evidence"] = reference
    row = next(iter(reference["rows"]), None)
    name = next(p["player_name"] for p in players if p["player_id"] == player_id)
    payload["reference_player"] = {
        "player": {"player_id": player_id, "player_name": name}
    }
    if not row or not row["eligible"]:
        payload["answer"] += (
            f"\n\n{name} has insufficient eligible data in this scope; gaps are unavailable."
        )
        return
    metric = reference["metric"]
    unit = (
        "assists per game"
        if query.metric == "ast" and query.aggregation == "average"
        else metric["label"]
    )
    value = row["display_value"]
    scope = reference["scope"]
    lines = [
        f"{name}: {value:.1f} {unit}, league rank {row['rank']} among {reference['cohort']['size']} eligible players ({row['valid_games']} games).",
        f"Same scope: {scope.get('start_date') or scope.get('window_start') or query.season} through {scope['as_of']}, {scope['season_type']}.",
    ]
    names = {p["player_id"]: p["player_name"] for p in players}
    table = payload["tables"][0]
    table["title"] = f"Other leaders — {unit}; gaps relative to {name}"
    table["columns"][1]["label"] = unit.capitalize()
    table["columns"].append({"key": "gap", "label": f"Gap vs {name}"})
    for index, leader in enumerate(result["evidence"]["rows"]):
        gap = (leader["value"] - row["value"]) * (
            100 if metric["unit"] == "ratio" else 1
        )
        table["rows"][index].append(f"{gap:+.1f}")
        if index < 5:
            relation = "ahead of" if gap > 0 else "behind" if gap < 0 else "level with"
            lines.append(
                f"{names[leader['player_id']]}: {leader['display_value']:.1f} {unit} — "
                + (
                    f"{abs(gap):.1f} {relation} {name}."
                    if gap
                    else f"{relation} {name}."
                )
            )
    table["reference_row_index"] = len(table["rows"])
    table["rows"].append(
        [
            name,
            f"{value:.1f}",
            str(row["observed_games"]),
            str(row["valid_games"]),
            str(row["rank"]),
            "Unavailable" if row["percentile"] is None else f"{row['percentile']:.1f}",
            "0.0",
        ]
    )
    lines.append(
        "Assists measure recorded creation, not complete playmaking impact; minutes, turnovers and shooting outcomes can affect the comparison."
    )
    payload["assumptions"].append(
        "Gaps use unrounded averages; displayed rounded values may differ by 0.1."
    )
    payload["answer"] = "\n\n".join(lines)


def _payload(message: str, status: str) -> dict[str, Any]:
    return {
        "answer": message,
        "status": status,
        "assumptions": [],
        "tables": [],
        "charts": [],
        "metric_definitions": [],
        "followups": [],
        "player_profile": None,
        "clarification_options": [],
        "tool_calls": [],
        "semantic_evidence": None,
    }


def render_answer(
    result: dict[str, Any], players: list[dict[str, Any]]
) -> dict[str, Any]:
    status = result["status"]
    if not result.get("evidence"):
        message = result.get("message") or "Please choose the intended player."
        payload = _payload(message, status)
        identity = result.get("identity", {})
        if identity:
            payload["identity"] = identity
        payload["clarification_options"] = [
            {
                "player_id": p["player_id"],
                "player_name": p["player_name"],
                "label": p["player_name"],
            }
            for p in identity.get("matches", [])
        ]
        return payload
    evidence = result["evidence"]
    payload = _payload("", status)
    payload["semantic_evidence"] = evidence
    payload["query_plan"] = {
        "route": "governed_metrics",
        "queries": result["resolved_queries"],
    }
    names = {p["player_id"]: p["player_name"] for p in players}
    sections = (
        [("Current", evidence["current"]), ("Baseline", evidence["baseline"])]
        if "current" in evidence
        else [("Result", evidence)]
    )
    statements = []
    definitions = {}
    for title, section in sections:
        scope = section["scope"]
        metric = section["metric"]
        definitions[metric["key"]] = {
            "key": metric["key"],
            "label": metric["label"],
            "definition": metric["numerator"]
            + (f" / ({metric['denominator']})" if metric["denominator"] else ""),
            "unit": metric["unit"],
        }
        context = f"{', '.join(scope.get('seasons') or [scope['season']])} {scope['season_type']}; {scope['window']}; as of {scope['as_of']}"
        if scope["window"] == "last_n_months":
            statements.append(
                f"Past {scope['n']} calendar months: {scope['window_start']} through "
                f"{scope['window_end']}, anchored to the source date. "
                f"Includes only {scope['season']} {scope['season_type']} games; "
                "other seasons and phases are excluded."
            )
        payload["assumptions"].append(context)
        payload["assumptions"].append(f"Metric contract: {section['contract_version']}")
        if scope.get("team_abbr"):
            payload["assumptions"].append(f"Appearance team: {scope['team_abbr']}")
        if scope.get("opponent_abbr"):
            payload["assumptions"].append(f"Opponent: {scope['opponent_abbr']}")
        if scope["operation"] == "game_log":
            source = section["provenance"]
            payload["assumptions"].append(
                f"Source: {source['source']}; snapshot {source['snapshot_id']}; data through {source['data_through']}. Retrospective source evidence."
            )
            payload["assumptions"].extend(section["warnings"])
            name = names.get(scope["player_id"], str(scope["player_id"]))
            statements.append(
                f"{name} — {metric['label']} by game: showing {section['displayed_games']} of {section['observed_games']} observed appearances, oldest to newest."
            )
            game_rows, points = [], []
            for row in section["rows"]:
                display = row["display_value"]
                value = (
                    "Unavailable"
                    if display is None
                    else f"{display:.1f}" + ("%" if metric["unit"] == "ratio" else "")
                )
                game_rows.append(
                    [
                        str(row["game_date"]),
                        str(row["game_id"]),
                        row["season_type"],
                        row["team_abbr"] or "Unknown",
                        row["opponent_abbr"] or "Unknown",
                        value,
                    ]
                )
                if display is not None:
                    points.append(
                        {
                            "x": str(row["game_date"]),
                            "y": display,
                            "meta": f"{row['game_id']} · {row['season_type']} · {row['team_abbr']} vs {row['opponent_abbr']}",
                        }
                    )
            payload["tables"].append(
                {
                    "title": f"{name}: {metric['label']} game log — {context}",
                    "columns": [
                        {"key": k, "label": v}
                        for k, v in (
                            ("date", "Date"),
                            ("game_id", "Game ID"),
                            ("phase", "Phase"),
                            ("team", "Team"),
                            ("opponent", "Opponent"),
                            ("value", metric["label"]),
                        )
                    ],
                    "rows": game_rows,
                }
            )
            if points and len(points) == len(game_rows):
                payload["charts"].append(
                    {
                        "type": "line",
                        "title": f"{name}: {metric['label']} by appearance",
                        "x_label": "Appearance date",
                        "y_label": metric["label"]
                        + (" (%)" if metric["unit"] == "ratio" else ""),
                        "series": [
                            {"key": metric["key"], "label": name, "points": points}
                        ],
                    }
                )
            elif game_rows:
                payload["assumptions"].append(
                    "Chart withheld because some game values are unavailable; see the game log. Missing values are not zero."
                )
            continue
        cohort = section["cohort"]
        payload["assumptions"].append(
            f"Ranking cohort: {cohort['size']} eligible players; minimum {cohort['min_games']} games; {cohort['excluded_players']} excluded."
        )
        if cohort["min_attempts"] is not None:
            payload["assumptions"].append(
                f"Attempt minimum: {cohort['min_attempts']} {cohort['attempt_field']}."
            )
        source = section["provenance"]
        payload["assumptions"].append(
            f"Source: {source['source']}; snapshot {source['snapshot_id']}; data through {source['data_through']}. Retrospective source evidence."
        )
        payload["assumptions"].extend(section["warnings"])
        table_rows = []
        for row in section["rows"]:
            name = names.get(row["player_id"], str(row["player_id"]))
            value = (
                "Unavailable"
                if row["display_value"] is None
                else f"{row['display_value']:.1f}"
                + ("%" if metric["unit"] == "ratio" else "")
            )
            table_rows.append(
                [
                    name,
                    value,
                    str(row["observed_games"]),
                    str(row["valid_games"]),
                    str(row["rank"] or "—"),
                    "Unavailable"
                    if row["percentile"] is None
                    else f"{row['percentile']:.1f}",
                ]
            )
            if len(section["rows"]) == 1:
                statements.append(
                    f"{title}: {name} — {metric['label']} {value} ({scope['aggregation']}, {row['valid_games']} valid / {row['observed_games']} observed games)."
                )
            if row["sample_warning"]:
                payload["assumptions"].append(f"{name}: {row['sample_warning']}")
            if row["status"] == "partial":
                payload["assumptions"].append(
                    f"{name}: missing components in {row['missing_component_games']} games; partial result."
                )
            if scope["operation"] != "summary" and row["exclusion_reasons"]:
                payload["assumptions"].append(
                    f"{name}: {', '.join(row['exclusion_reasons'])}"
                )
            if metric["denominator"]:
                payload["assumptions"].append(
                    f"{name}: numerator {row['numerator']}; denominator {row['denominator']}."
                )
        payload["tables"].append(
            {
                "title": f"{title}: {metric['label']} — {context}",
                "columns": [
                    {"key": k, "label": label}
                    for k, label in (
                        ("player", "Player"),
                        ("value", "Value"),
                        ("observed", "Observed games"),
                        ("valid", "Valid games"),
                        ("rank", "Rank"),
                        ("percentile", "Percentile"),
                    )
                ],
                "rows": table_rows,
            }
        )
        if not table_rows:
            statements.append(
                f"{title}: no eligible observations for {context}. Thresholds were not lowered."
            )
    if "difference" in evidence:
        value = evidence["difference"]
        statements.append(
            "Comparison unavailable."
            if value is None
            else f"Difference: {value:.1f} {evidence['difference_unit']}."
        )
        payload["assumptions"].append(
            f"Overlapping appearances: {len(evidence['overlapping_game_ids'])}."
        )
    payload["answer"] = (
        "\n".join(statements)
        or "Results are shown below for the stated scope and qualification."
    )
    payload["metric_definitions"] = list(definitions.values())
    ids = {q.get("player_id") for q in result["resolved_queries"] if q.get("player_id")}
    if len(ids) == 1:
        player = next((p for p in players if p["player_id"] in ids), None)
        if player:
            payload["player_profile"] = identity_profile(player, [])
    return payload


class SemanticAsk:
    def __init__(self, settings: Any, warehouse: Any, conversation_store: Any = None):
        self.settings = settings
        self.warehouse = warehouse
        self.store = conversation_store

    def answer(
        self,
        question: str,
        *,
        client: Any,
        model: str,
        conversation_id: str | None = None,
        selected_player: dict[str, Any] | None = None,
        trace: Any = None,
        progress_callback: Any = None,
    ) -> dict[str, Any]:
        started = monotonic()
        original = question
        store = self.store if conversation_id else None
        pending = store.get_pending_clarification(conversation_id) if store else None
        turns = (
            store.get_turns(
                conversation_id, max_turns=self.settings.agent_conversation_max_turns
            )
            if store
            else []
        )
        context: dict[str, Any] = next(
            (turn.context for turn in reversed(turns) if turn.context), {}
        )
        leader_plan = contextual_leader_plan(original, context, self.settings.season)
        if leader_plan:
            # A fully restated supported request supersedes an earlier mistaken
            # request for a count; do not feed that clarification back in.
            pending = None
        if pending and not selected_player and comparison_sides(question):
            # A fully restated comparison replaces a scope clarification rather
            # than remaining trapped behind the unsupported original scope.
            pending = None
        if pending:
            if selected_player:
                question = pending.question
            else:
                question = f"{pending.question}\nClarification: {question}"
        elif store and not context:
            if turns and re.search(
                r"\b(his|him|their|them|instead|same)\b", question, re.I
            ):
                question = f"Previous question: {turns[-1].question}\nCurrent question: {question}"
        question, inherited = resolve_followup(question, context)
        if selected_player:
            question += f"\nSelected player: {selected_player.get('player_name', '')}"
        if progress_callback:
            progress_callback(
                {
                    "type": "plan",
                    "route": "governed_metrics",
                    "confidence": 1.0,
                    "required_tools": ["governed_metrics"],
                }
            )
        try:
            comparison = comparison_sides(question)
            overview = (
                comparison_scope(question, self.settings.season)
                if comparison
                else overview_scope(question, self.settings.season)
                if wants_overview(question)
                else None
            )
            seasons = (
                overview["seasons"]
                if overview
                else requested_seasons(question, self.settings.season)
            )
            if inherited.get("seasons") and not overview:
                seasons = [s for s in inherited["seasons"] if s]
            snapshot, evidence = self.warehouse.load(seasons)
            players = (
                self.warehouse.players(evidence)
                if hasattr(self.warehouse, "players")
                else source_players(evidence)
            )
            if context.get("browser_recovered") and any(
                not any(
                    p["player_id"] == hint["player_id"]
                    and p["player_name"].casefold() == hint["player_name"].casefold()
                    for p in players
                )
                for hint in context.get("players", [])
            ):
                raise SemanticError(
                    "unsupported_coverage",
                    "Saved player context does not match this source. Please restate the full player name and dates.",
                )
            if pending and not selected_player and not overview:
                resolved = resolve_overview_player(question, players, evidence.rows)
                if len(resolved) == 1:
                    selected_player = resolved[0]
            if selected_player and not comparison:
                matches = [
                    p
                    for p in players
                    if p["player_id"] == selected_player.get("player_id")
                ]
                if not matches:
                    raise SemanticError(
                        "unsupported_coverage",
                        "Selected player is absent from this source scope",
                    )
                # A selection resolves ambiguous aliases only to this source ID.
                chosen = matches[0]
                players = [
                    dict(
                        p,
                        aliases=[]
                        if p["player_id"] != chosen["player_id"]
                        else p["aliases"],
                    )
                    for p in players
                    if p["player_id"] == chosen["player_id"]
                    or p["player_name"].casefold() != chosen["player_name"].casefold()
                ]
            if overview:
                if comparison:
                    payload = build_comparison(
                        question,
                        evidence,
                        players,
                        overview,
                        selected_player,
                        (pending.query_plan or {}).get("comparison_choices")
                        if pending
                        else None,
                    )
                else:
                    payload = build_overview(
                        question, evidence, players, overview, selected_player
                    )
                plan = {
                    "status": payload["status"],
                    "queries": [],
                    "message": "Player comparison"
                    if comparison
                    else "Performance overview",
                    "model_calls": 0,
                    "comparison_choices": payload.get("comparison_choices", {}),
                }
                result = {"status": payload["status"]}
            else:
                plan = leader_plan or plan_question(
                    client,
                    model=model,
                    question=question,
                    selected_season=seasons[0]
                    if len(seasons) == 1
                    else self.settings.season,
                    usage_callback=trace.add_usage if trace else None,
                    players=players,
                    conversation_context=context,
                    teams=sorted(
                        {
                            r[k]
                            for r in evidence.rows
                            for k in ("team_abbr", "opponent_abbr")
                            if r.get(k)
                        }
                    ),
                )
                plan = normalize_reference_plan(
                    plan, inherited.get("excluded_player_ids", []), players
                )
                for query in plan.get("queries", []):
                    if inherited.get("seasons") and query.get("window") == "date_range":
                        query["seasons"] = seasons
                    if inherited.get("excluded_player_ids"):
                        if query.get("operation") != "rank":
                            raise SemanticError(
                                "clarification_required",
                                "Should I rank other players, excluding the player discussed above?",
                            )
                        query["player_name"] = None
                        query["excluded_player_ids"] = inherited["excluded_player_ids"]
                result = execute_plan(plan, evidence, players)
                payload = render_answer(result, players)
                if (
                    payload["status"] == "ok"
                    and len(inherited.get("excluded_player_ids", [])) == 1
                ):
                    add_leaderboard_reference(
                        payload,
                        result,
                        evidence,
                        players,
                        inherited["excluded_player_ids"][0],
                    )
                if inherited.get("assumption") and payload["status"] == "ok":
                    payload["assumptions"].append(inherited["assumption"])
                    payload["answer"] = (
                        inherited["assumption"] + "\n\n" + payload["answer"]
                    )
                if payload.get("player_profile"):
                    player = payload["player_profile"]["player"]
                    payload["player_profile"] = identity_profile(player, evidence.rows)
                for option in payload.get("clarification_options", []):
                    option["team_abbr"] = identity_profile(option, evidence.rows)[
                        "player"
                    ].get("team_abbr")
                    option["label"] = (
                        f"{option['player_name']} (ID {option['player_id']})"
                    )
            payload["semantic_plan"] = plan
            payload["source_query"] = {
                k: v for k, v in snapshot.get("capture", {}).items() if k != "sql"
            }
            payload["tool_calls"] = [
                {
                    "name": "governed_metrics",
                    "args": {"seasons": seasons},
                    "status": result["status"],
                    "result": {
                        "contract": "nba_semantics/0.1",
                        "source_query": payload["source_query"],
                    },
                }
            ]
        except SemanticError as exc:
            payload = _payload(str(exc), exc.code)
        if trace:
            trace.add_tool(
                name="governed_metrics",
                args={"selected_season": self.settings.season},
                status=payload["status"],
                duration_ms=round((monotonic() - started) * 1000),
                result={
                    "status": payload["status"],
                    "source_query": payload.get("source_query"),
                },
            )
        if progress_callback:
            progress_callback(
                {
                    "type": "tool_end",
                    "name": "governed_metrics",
                    "status": payload["status"],
                    "duration_ms": round((monotonic() - started) * 1000),
                }
            )
        payload["conversation_id"] = conversation_id
        if payload["status"] == "clarification_required":
            payload["clarification_question"] = question
        payload["agent_plan"] = {
            "route": "governed_metrics",
            "confidence": 1.0,
            "needs_clarification": payload["status"] == "clarification_required",
        }
        if trace:
            trace.set_plan(route="governed_metrics", confidence=1.0)
            trace.outcome = (
                "clarified"
                if payload["status"] == "clarification_required"
                else "answered"
                if payload["status"] == "ok"
                else payload["status"]
            )
        if store:
            if payload["status"] == "clarification_required":
                store.set_pending_clarification(
                    conversation_id,
                    question=question,
                    query_plan=payload.get("semantic_plan"),
                )
            else:
                store.clear_pending_clarification(conversation_id)
            if payload["status"] == "ok":
                next_context = analysis_context(original, payload)
                if not next_context.get("players"):
                    next_context["players"] = context.get("players", [])
                store.append_turn(
                    conversation_id,
                    question=saved_context_question(original, payload),
                    answer=payload["answer"],
                    max_turns=self.settings.agent_conversation_max_turns,
                    context=next_context,
                )
        return payload
