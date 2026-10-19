"""Ask-compatible answers from governed evidence, without model-authored numbers."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import replace
from datetime import date, timedelta
from time import monotonic
from typing import Any

from app.agent.award_lookup import award_intro, resolve_award_question, wants_award
from app.agent.clock import today as semantic_today
from app.agent.execution import timed
from app.agent.followup import analysis_context, resolve_followup
from app.agent.history import saved_context_question
from app.agent.metric_presentation import (
    GAME_COVERAGE_NOTE,
    GAMES_IN_SCOPE,
    GAMES_WITH_DATA,
    game_coverage,
)
from app.agent.performance_overview import (
    build_overview,
    identity_profile,
    overview_scope,
    resolve_overview_player,
)
from app.agent.player_comparison import (
    build_comparison,
    comparison_scope,
    comparison_sides,
)
from app.agent.player_splits import answer_split
from app.agent.question_intent import question_intent, split_kind
from app.agent.route_labels import route_hint, status_reason
from app.agent.routes import supersedes_pending
from app.agent.semantic_planner import execute_plan, explicit_scope, plan_question
from app.agent.semantic_serving import (
    fallback_notice,
    has_time_scope,
    load_available_season,
    requested_seasons,
    season_candidates,
    seasons_in_dates,
    source_players,
)
from app.agent.semantics import Query, SemanticError, run_query
from app.seasons import season_bounds

# Semantic shapes (app.agent.routes) and the payload labels that tell them apart.
SEMANTIC_LABELS = {
    "award": "award",
    "player_split": "player_split",
    "player_comparison": "player_comparison",
    "overview": "player_overview",
    "metrics": "governed_metrics",
}


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
    if not match:
        return None
    try:
        scope = explicit_scope(resolved)
    except SemanticError:
        return None
    if scope.get("window") != "date_range":
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
    collapsed = {**plan, "status": "query", "queries": [dict(rank)]}
    resolutions = plan.get("aggregation_resolution")
    if isinstance(resolutions, list) and len(resolutions) == len(queries):
        collapsed["aggregation_resolution"] = [resolutions[queries.index(rank)]]
    return collapsed


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
    from app.agent.payload import answer_payload

    return answer_payload(message, status=status)


@timed("rendering")
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
        "aggregation_resolution": result.get("aggregation_resolution", []),
    }
    if policy := result.get("aggregation_policy"):
        payload["query_plan"]["aggregation_policy"] = policy
        payload["assumptions"].append(
            "Aggregation was not specified; using points per game (average over games with valid points)."
        )
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
        if scope.get("home_away"):
            payload["assumptions"].append(f"Venue: {scope['home_away']}")
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
                    f"{title}: {name} — {metric['label']} {value} ({scope['aggregation']}, {game_coverage(row['valid_games'], row['observed_games'])} games with data)."
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
                        ("observed", GAMES_IN_SCOPE),
                        ("valid", GAMES_WITH_DATA),
                        ("rank", "Rank"),
                        ("percentile", "Percentile"),
                    )
                ],
                "rows": table_rows,
                "description": GAME_COVERAGE_NOTE,
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
        from app.agent.availability_ask import wants_availability
        from app.agent.research_ask import refusal

        if wants_availability(question):
            return route_hint(
                refusal(
                    "This question requires verified teammate availability. Overall statistics cannot answer that condition.",
                    code="availability_scope_required",
                    trace=trace,
                ),
                route="unsupported",
                source="deterministic",
                reason_code="availability_scope_required",
            )
        original = question
        award = None
        award_request = wants_award(original)
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
        if award_request:
            context = {}
            pending = None
            selected_player = None
        if (
            pending
            and not selected_player
            and not (pending.query_plan or {}).get("overview_clarification")
            and supersedes_pending(original, pending)
        ):
            # A self-contained request for a different task is answered on its
            # own wording; it must not be folded into the pending question.
            # Typed Overview clarifications resume through resume_overview.
            pending = None
        from app.agent.overview_clarification import (
            OverviewClarification,
            resume_overview,
        )

        resume_error = None
        resumed_overview = False
        if pending and (pending.query_plan or {}).get("overview_clarification"):
            question, resume_error, restated = resume_overview(question, pending)
            resumed_overview = True
            context = {}
            if restated:
                from app.agent.overview_clarification_context import (
                    pending_overview_context,
                )

                try:
                    context = pending_overview_context(
                        question, pending, self.settings.season, self.warehouse
                    )
                except OverviewClarification as exc:
                    resume_error = exc
                    question = pending.question
            pending = None
        leader_plan = contextual_leader_plan(original, context, self.settings.season)
        if leader_plan:
            # A fully restated supported request supersedes an earlier mistaken
            # request for a count; do not feed that clarification back in.
            pending = None
        if (
            pending
            and not selected_player
            and (comparison_sides(question) or split_kind(question))
        ):
            # A fully restated comparison replaces a scope clarification rather
            # than remaining trapped behind the unsupported original scope.
            pending = None
        dated_points_eligible = not (
            pending or resumed_overview or selected_player or award_request
        )
        if pending:
            if selected_player:
                question = pending.question
            else:
                question = f"{pending.question}\nClarification: {question}"
        elif store and not context and not resumed_overview:
            if turns and re.search(
                r"\b(his|him|their|them|instead|same)\b", question, re.I
            ):
                dated_points_eligible = False
                question = f"Previous question: {turns[-1].question}\nCurrent question: {question}"
        if context.get("split_question") and not pending:
            from app.agent.player_splits import split_followup

            question = split_followup(question, context["split_question"])
        question, inherited = resolve_followup(question, context)
        # Route labels for payload/trace; the semantic shape is filled in below.
        route_shape = "award" if award_request else None
        route_source = (
            "pending_clarification"
            if pending or resumed_overview
            else "prior_route"
            if inherited or leader_plan or context.get("split_question")
            else "deterministic"
        )
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
        request_question = question
        overview_pending = None
        try:
            if resume_error:
                raise resume_error
            notice = None
            prior_award = context.get("award_evidence") or {}
            if (
                prior_award.get("performance_phase") == "Finals"
                and re.search(r"\b(?:his|him|he|their|them)\b", original, re.I)
                and not re.search(
                    r"\b(?:regular[ -]season|playoffs?|postseason)\b", original, re.I
                )
            ):
                award = prior_award
                raise SemanticError(
                    "unsupported_scope",
                    "Finals-only performance is not connected. Specify regular-season or full-playoff performance to use game-level statistics.",
                )
            if award_request:
                award = resolve_award_question(original, self.settings.season)
                if not award["performance_requested"]:
                    payload = _payload(award_intro(award), "ok")
                    payload["award_evidence"] = award
                    payload["semantic_plan"] = {
                        "kind": "award_lookup",
                        "model_calls": 0,
                    }
                    payload["agent_plan"] = {
                        "route": "governed_metrics",
                        "needs_clarification": False,
                        "confidence": 1.0,
                    }
                    payload["conversation_id"] = conversation_id
                    payload["conversation_context"] = {
                        "route": "award",
                        "award_evidence": award,
                        "players": [
                            {k: award[k] for k in ("player_id", "player_name")}
                        ],
                        "scope": {
                            "season": award["season"],
                            "start": season_bounds(award["season"])[0].isoformat(),
                            "end": season_bounds(award["season"])[1].isoformat(),
                            "phases": [award["performance_phase"]],
                        },
                    }
                    if store:
                        store.clear_pending_clarification(conversation_id)
                        store.append_turn(
                            conversation_id,
                            question=original,
                            answer=payload["answer"],
                            max_turns=self.settings.agent_conversation_max_turns,
                            context=payload["conversation_context"],
                        )
                    route_hint(payload, route="award", source=route_source)
                    if trace:
                        trace.set_plan(route="award", confidence=1.0)
                        trace.outcome = "answered"
                    return payload
                if award["performance_phase"] == "Finals":
                    raise SemanticError(
                        "unsupported_scope",
                        "Finals-only game scope is not connected. Ask for this winner's regular-season or full-playoff performance explicitly; I will not substitute either for Finals performance.",
                    )
                question = award["performance_question"]
                inherited = {}
                selected_player = {k: award[k] for k in ("player_id", "player_name")}
            from app.agent.overview_positions import requested_groups

            groups = requested_groups(question)
            if len(groups) > 1:
                raise OverviewClarification(
                    "groups",
                    "Choose one comparison group: guards, wings/forwards, or bigs/centers. Multiple groups need separate comparisons.",
                )
            intent = question_intent(question)
            if award:
                intent["kind"] = "award_lookup_then_performance"
            if intent["status"]:
                raise SemanticError(
                    intent["status"], intent["message"] or "Unsupported question"
                )
            from app.agent.overview_positions import (
                comparison_intent,
                peer_question,
                requested_group,
            )
            from app.agent.time_defaults import resolve_time_defaults

            requested_comparison = comparison_intent(question)
            position_group = requested_group(question)
            from app.agent.routes import semantic_shape

            initial_shape = semantic_shape(question)
            route_shape = route_shape or initial_shape
            if position_group and initial_shape != "overview":
                # A cohort constraint must not disappear in a scalar/league plan
                # or a two-player scorecard that cannot represent it.
                message = (
                    "Choose a two-player scorecard without a group ranking, or name one player for a comparison with "
                    + {
                        "Guard": "guards",
                        "Wing": "wings/forwards",
                        "Big": "bigs/centers",
                    }[position_group]
                    + "."
                    if initial_shape == "player_comparison"
                    else "Position comparisons currently need one player and a broad scoring, playmaking, rebounding, or defensive-activity question. Please specify that comparison."
                )
                raise OverviewClarification("composition", message)
            question, time_assumptions, period_options = resolve_time_defaults(
                peer_question(question) if initial_shape == "overview" else question,
                context=context,
            )
            if position_group:
                question += (
                    " among "
                    + {"Guard": "guards", "Wing": "wings", "Big": "big men"}[
                        position_group
                    ]
                )
            from app.agent.routes import semantic_shape

            shape = semantic_shape(question)
            route_shape = "award" if award_request else shape
            split = split_kind(question) if shape == "player_split" else None
            comparison = (
                comparison_sides(question) if shape == "player_comparison" else None
            )
            overview = (
                comparison_scope(question, self.settings.season)
                if comparison
                else overview_scope(
                    question, self.settings.season, comparison=requested_comparison
                )
                if shape == "overview"
                else None
            )
            if overview and not comparison:
                overview["comparison_intent"] = requested_comparison
            seasons = (
                overview["seasons"]
                if overview
                else requested_seasons(question, self.settings.season)
            )
            if split:
                from app.agent.player_splits import split_seasons

                seasons = split_seasons(question, self.settings.season)
            if inherited.get("seasons") and not overview:
                seasons = [s for s in inherited["seasons"] if s]
            metric_window = {}
            named_seasons = bool(
                re.search(r"\b20\d{2}[-–/](?:20\d{2}|\d{2})\b(?![-/–]\d)", question)
            )

            def window_seasons(start, end):
                covered = seasons_in_dates(start, end)
                inherited_order = [
                    s for s in inherited.get("seasons", []) if s in covered
                ]
                covered = inherited_order + [
                    s for s in covered if s not in inherited_order
                ]
                if named_seasons and not set(covered).issubset(
                    requested_seasons(question, self.settings.season)
                ):
                    raise SemanticError(
                        "clarification_required",
                        "The dates conflict with the named season. Please choose one scope.",
                    )
                return covered

            if not overview and not split:
                metric_window = explicit_scope(question)
                if metric_window.get("window") == "last_n_months" and any(
                    "rolling" in note for note in time_assumptions
                ):
                    from app.agent.overview_calendar import shift_months

                    anchor_match = re.search(
                        r"\bas[ -]of\s+(\d{4}-\d{2}-\d{2})\b", question, re.I
                    )
                    anchor = (
                        date.fromisoformat(anchor_match[1])
                        if anchor_match
                        else semantic_today()
                    )
                    metric_window = {
                        **metric_window,
                        "window": "date_range",
                        "start_date": (
                            shift_months(anchor, metric_window["n"]) + timedelta(days=1)
                        ).isoformat(),
                        "as_of": anchor.isoformat(),
                        "n": None,
                    }
                if metric_window.get("window") == "date_range":
                    seasons = window_seasons(
                        metric_window["start_date"], metric_window["as_of"]
                    )
            allow_fallback = (
                not has_time_scope(question) and not inherited and not pending
            )
            if allow_fallback:
                used = self.settings.season
                try:
                    snapshot, evidence = self.warehouse.load(seasons)
                    evidence.validate()
                    if not any(r["season"] == used for r in evidence.rows):
                        raise SemanticError(
                            "unsupported_coverage", f"No evidence for {used}"
                        )
                except SemanticError as exc:
                    if exc.code != "unsupported_coverage":
                        raise
                    snapshot, evidence, used = load_available_season(
                        self.warehouse.load,
                        season_candidates(question, self.settings.season),
                    )
                    seasons = [used]
                if overview and used != self.settings.season:
                    overview = (
                        comparison_scope(question, used)
                        if comparison
                        else overview_scope(
                            question, used, comparison=requested_comparison
                        )
                    )
                    overview["seasons"] = seasons
                if used != self.settings.season:
                    notice = fallback_notice(self.settings.season, used)
            else:
                try:
                    snapshot, evidence = self.warehouse.load(seasons)
                except SemanticError as exc:
                    primary_seasons = [
                        s
                        for s in seasons
                        if overview
                        and season_bounds(s)[0] <= overview["end"]
                        and season_bounds(s)[1] >= overview["start"]
                    ]
                    if (
                        exc.code != "unsupported_coverage"
                        or not primary_seasons
                        or primary_seasons == seasons
                    ):
                        raise
                    # The requested period stays fixed; only an unavailable baseline
                    # is omitted. build_overview withholds its comparisons.
                    snapshot, evidence = self.warehouse.load(primary_seasons)
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
                from app.agent.planner import explicit_metric_mentions

                if (
                    explicit_metric_mentions(original) == {"pts"}
                    and explicit_scope(original).get("window") == "date_range"
                    and len(resolve_overview_player(original, players, evidence.rows))
                    == 1
                ):
                    # A complete new dated request is not a terse scope reply.
                    pending = None
                    question = original
                    dated_points_eligible = True
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
                if (
                    award
                    and unicodedata.normalize("NFKD", chosen["player_name"])
                    .encode("ascii", "ignore")
                    .lower()
                    != unicodedata.normalize("NFKD", award["player_name"])
                    .encode("ascii", "ignore")
                    .lower()
                ):
                    raise SemanticError(
                        "invalid_evidence",
                        "Award identity does not match the warehouse player record.",
                    )
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
            if split:
                payload = answer_split(question, evidence, players, seasons[0])
                plan = payload["semantic_plan"]
                result = {"status": payload["status"]}
            elif overview:
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
                    dated_points_eligible=dated_points_eligible,
                    dated_points_question=original,
                    selected_season=seasons[0]
                    if len(seasons) == 1
                    else self.settings.season,
                    usage_callback=trace.add_usage if trace else None,
                    players=players,
                    # Stored route labels are routing state, not planner context.
                    conversation_context={
                        k: v for k, v in context.items() if k != "route"
                    },
                    teams=sorted(
                        {
                            r[k]
                            for r in evidence.rows
                            for k in ("team_abbr", "opponent_abbr")
                            if r.get(k)
                        }
                    ),
                )
                if pending:
                    from app.agent.question_intent import explicit_aggregation

                    if not explicit_aggregation(question) and any(
                        q.get("metric") == "pts"
                        and q.get("operation", "summary") == "summary"
                        and q.get("window") == "date_range"
                        and q.get("aggregation") == "total"
                        for q in plan.get("queries", [])
                    ):
                        raise SemanticError(
                            "clarification_required",
                            "Please restate the player and date interval, or specify total points versus points per game.",
                        )
                plan = normalize_reference_plan(
                    plan, inherited.get("excluded_player_ids", []), players
                )
                validated_points_scope = plan.pop("validated_points_scope", None)
                for query in plan.get("queries", []):
                    if metric_window.get("window") == "date_range":
                        query.update(metric_window)
                    if validated_points_scope:
                        query.update(validated_points_scope)
                    if query.get("window") == "date_range":
                        # Fresh metric requests need the same full-window coverage as
                        # inherited ones. Missing required archives must fail closed.
                        end = query.get("as_of") or max(
                            (
                                str(r["game_date"])
                                for r in evidence.rows
                                if r["season"] == query["season"]
                                and (
                                    query.get("season_type", "Regular Season") == "Both"
                                    or r["season_type"]
                                    == query.get("season_type", "Regular Season")
                                )
                            ),
                            default=semantic_today().isoformat(),
                        )
                        if metric_window.get("window") != "date_range":
                            anchor_match = re.search(
                                r"\bas[ -]of\s+(\d{4}-\d{2}-\d{2})\b", question, re.I
                            )
                            upper = (
                                min(
                                    semantic_today(),
                                    date.fromisoformat(anchor_match[1]),
                                )
                                if anchor_match
                                else semantic_today()
                            )
                            if date.fromisoformat(end) > upper:
                                end = upper.isoformat()
                                note = f"Using {end} as the latest allowed date for this request."
                                if note not in time_assumptions:
                                    time_assumptions.append(note)
                        query["seasons"] = window_seasons(query.get("start_date"), end)
                        query["as_of"] = end
                        if query.get("season") not in query["seasons"]:
                            query["season"] = query["seasons"][0]
                    if inherited.get("excluded_player_ids"):
                        if query.get("operation") != "rank":
                            raise SemanticError(
                                "clarification_required",
                                "Should I rank other players, excluding the player discussed above?",
                            )
                        query["player_name"] = None
                        query["excluded_player_ids"] = inherited["excluded_player_ids"]
                required_seasons = sorted(
                    set(seasons).union(
                        s
                        for q in plan.get("queries", [])
                        if q.get("window") == "date_range"
                        for s in q["seasons"]
                    ),
                    reverse=True,
                )
                if required_seasons and not set(required_seasons).issubset(seasons):
                    snapshot, evidence = self.warehouse.load(required_seasons)
                    seasons = required_seasons
                    players = (
                        self.warehouse.players(evidence)
                        if hasattr(self.warehouse, "players")
                        else source_players(evidence)
                    )
                    if selected_player:
                        players = [
                            p
                            for p in players
                            if p["player_id"] == selected_player["player_id"]
                            or p["player_name"].casefold()
                            != selected_player["player_name"].casefold()
                        ]
                result = execute_plan(plan, evidence, players)
                # Reuse the validated scope plan; only the default season changes.
                # No additional model calls, threshold relaxation, or date changes.
                retryable_plan = bool(plan.get("queries")) and all(
                    q.get("season") == seasons[0]
                    and not q.get("seasons")
                    and q.get("window", "season_to_date") == "season_to_date"
                    and not q.get("as_of")
                    and not q.get("start_date")
                    for q in plan["queries"]
                )
                if allow_fallback and retryable_plan:
                    for older in season_candidates(question, self.settings.season):
                        if older >= seasons[0] or result["status"] not in (
                            "unsupported_coverage",
                            "no_observations",
                        ):
                            continue
                        try:
                            candidate_snapshot, candidate_evidence, _ = (
                                load_available_season(self.warehouse.load, [older])
                            )
                        except SemanticError as exc:
                            if exc.code == "unsupported_coverage":
                                continue
                            raise
                        candidate_players = source_players(candidate_evidence)
                        candidate_plan = {
                            **plan,
                            "queries": [dict(q, season=older) for q in plan["queries"]],
                        }
                        candidate_result = execute_plan(
                            candidate_plan, candidate_evidence, candidate_players
                        )
                        if candidate_result["status"] == "ok":
                            plan, result = candidate_plan, candidate_result
                            snapshot, evidence, players = (
                                candidate_snapshot,
                                candidate_evidence,
                                candidate_players,
                            )
                            seasons = [older]
                            notice = fallback_notice(self.settings.season, older)
                            break
                        if candidate_result["status"] not in (
                            "unsupported_coverage",
                            "no_observations",
                        ):
                            break
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
            if award:
                payload["answer"] = award_intro(award) + "\n\n" + payload["answer"]
                payload["award_evidence"] = award
                payload["assumptions"].append(
                    "Award season: "
                    + award["season"]
                    + "; "
                    + award["season_basis"]
                    + ". Performance is computed from warehouse game records."
                )
            if time_assumptions and payload.get("status") in (
                "ok",
                "unsupported_coverage",
            ):
                payload["scope_assumptions"] = time_assumptions
                payload["period_options"] = period_options
                payload["assumptions"].extend(time_assumptions)
            payload["semantic_plan"] = plan
            payload["question_intent"] = intent
            if notice:
                payload["answer"] = notice + "\n\n" + payload["answer"]
                payload["assumptions"].append(notice)
                payload["season_fallback"] = {
                    "requested": self.settings.season,
                    "used": seasons[0],
                }
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
            if isinstance(exc, OverviewClarification):
                overview_pending = {"overview_clarification": exc.kind}
                question = request_question
            payload = _payload(str(exc), exc.code)
            if award:
                payload["answer"] = (
                    award_intro(award)
                    + "\n\nPerformance could not be verified: "
                    + str(exc)
                )
                payload["award_evidence"] = award
                payload["answerability"] = "partial"
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
            payload["overview_clarification"] = {
                "version": 1,
                "kind": (overview_pending or {}).get("overview_clarification"),
            }
        payload["agent_plan"] = {
            "route": "governed_metrics",
            "confidence": 1.0,
            "needs_clarification": payload["status"] == "clarification_required",
        }
        if route_shape is None:
            from app.agent.routes import semantic_shape as shape_of

            route_shape = shape_of(request_question)
        route_label = SEMANTIC_LABELS[route_shape]
        reason_code = (
            f"overview_{overview_pending['overview_clarification']}"
            if overview_pending and overview_pending.get("overview_clarification")
            else status_reason(payload)
        )
        route_hint(
            payload, route=route_label, source=route_source, reason_code=reason_code
        )
        if trace:
            trace.set_plan(route=route_label, confidence=1.0)
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
                    query_plan=overview_pending or payload.get("semantic_plan"),
                    route=route_label,
                    candidates=payload.get("clarification_options"),
                )
            else:
                store.clear_pending_clarification(conversation_id)
            if payload["status"] == "ok":
                next_context = analysis_context(original, payload)
                if not next_context.get("players"):
                    next_context["players"] = context.get("players", [])
                payload["conversation_context"] = next_context
                store.append_turn(
                    conversation_id,
                    question=saved_context_question(original, payload),
                    answer=payload["answer"],
                    max_turns=self.settings.agent_conversation_max_turns,
                    context=next_context,
                )
        return payload
