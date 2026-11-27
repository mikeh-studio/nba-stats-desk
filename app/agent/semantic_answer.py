"""Ask-compatible answers from governed evidence, without model-authored numbers."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from time import monotonic
from typing import TYPE_CHECKING, Any

from app.agent.award_lookup import award_intro, resolve_award_question, wants_award
from app.agent.clock import today as semantic_today
from app.agent.conversation import ConversationStore, PendingClarification
from app.agent.execution import timed
from app.agent.followup import analysis_context, inherits_analysis, resolve_followup
from app.agent.history import saved_context_question
from app.agent.intelligence import plan_question
from app.agent.metric_presentation import (
    GAME_COVERAGE_NOTE,
    GAMES_IN_SCOPE,
    GAMES_WITH_DATA,
    game_coverage,
)
from app.agent.name_matching import (
    PlayerNameError,
    identity_payload,
    identity_replay_scope,
    name_matching_entrypoint,
    prepare_names,
)
from app.agent.overview_clarification import OverviewClarification, resume_overview
from app.agent.performance_overview import (
    build_overview,
    identity_profile,
    overview_scope,
    resolve_overview_player,
)
from app.agent.plan_execution import execute_plan
from app.agent.player_comparison import (
    build_comparison,
    comparison_scope,
    comparison_sides,
)
from app.agent.player_splits import answer_split
from app.agent.route_labels import route_hint, status_reason
from app.agent.routes import semantic_shape, supersedes_pending
from app.agent.scope_policy import (
    explicit_phase,
    explicit_scope,
    has_phase_cue,
    question_scope,
    split_kind,
)
from app.agent.semantic_serving import (
    fallback_notice,
    has_time_scope,
    load_available_season,
    requested_seasons,
    season_candidates,
    seasons_in_dates,
    source_players,
)
from app.agent.semantic_source import capture_timestamp
from app.agent.semantics import Evidence, Query, SemanticError, load_contract, run_query
from app.agent.source_policy import get_source_policy
from app.seasons import DEFAULT_SEASON, season_bounds

if TYPE_CHECKING:
    from app.agent.derived_series import DerivedSeriesPlan
    from app.agent.service import StatsAgent
    from app.agent.serving_warehouse import PreparedAttempt

# Semantic shapes (app.agent.routes) and the payload labels that tell them apart.
SEMANTIC_LABELS = {
    "award": "award",
    "player_split": "player_split",
    "player_comparison": "player_comparison",
    "overview": "player_overview",
    "metrics": "governed_metrics",
}


def plain_scoped_overview(question: str) -> bool:
    """Recognize only simple overviews whose prior period is optional.

    Under a restricted source policy, unrecognized modifiers retain the baseline
    dependency. This deliberately favors a scope refusal over silently omitting
    a comparison the user may have requested.
    """
    if re.search(
        r"\b(?:yoy|year[ -]+(?:over|on)[ -]+year|season[ -]+(?:over|on)[ -]+season)\b",
        question,
        re.I,
    ):
        return False
    name = r"(?P<player>[\w'’.-]+(?:\s+[\w'’.-]+){0,5}?)"
    season = r"20\d{2}[-–/](?:20\d{2}|\d{2})"
    phase = r"(?:regular[ -]season|playoffs?|postseason)"
    dates = r"from \d{4}-\d{2}-\d{2} (?:to|through) \d{4}-\d{2}-\d{2}"
    period = rf"(?: (?:in|during|for) (?:the )?(?:{season} {phase}|{phase} {season})| {dates}(?: {phase})?)"
    templates = (
        rf"(?:give me (?:a|an) (?:player )?(?:overview|profile|assessment|summary) of|"
        rf"tell me about|summari[sz]e) {name}(?: (?:overall )?(?:performance|stats|statistics))?{period}",
        rf"how (?:did|has) {name} (?:perform|performed|play|played|do|done){period}",
        rf"{name} (?:overall )?performance{period}",
    )
    for template in templates:
        match = re.fullmatch(template + r"[.!?]?", question.strip(), re.I)
        if match is None:
            continue
        words = match["player"].split()
        # The source roster will still resolve the identity. Lowercase clauses
        # cannot be swallowed as a player's name by the intentionally small grammar.
        if all(
            word[0].isupper() or word in {"de", "del", "da", "van", "von"}
            for word in words
        ):
            return True
    return False


def normalize_metric_plan_scope(
    plan,
    *,
    metric_window,
    appearances,
    question,
    time_assumptions,
    window_seasons,
    inherited,
):
    """One date/exclusion normalization path for facts and prepared execution."""
    validated_points_scope = plan.pop("validated_points_scope", None)
    for query in plan.get("queries", []):
        if metric_window.get("window") == "date_range" and not plan.get(
            "explicit_period_operands"
        ):
            query.update(metric_window)
        if validated_points_scope:
            query.update(validated_points_scope)
        if query.get("window") == "date_range":
            # Fresh metric requests need the same full-window coverage as
            # inherited ones. Missing required archives must fail closed.
            end = query.get("as_of") or max(
                (
                    str(r["game_date"])
                    for r in appearances
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
                    r"\bas[ -]of\s+(\d{4}-\d{2}-\d{2})\b",
                    question,
                    re.I,
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
    return plan


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
    reference["identity"] = {"player_id": player_id, "player_name": name}
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
    from app.agent.claim_renderers import leaderboard_reference

    leaderboard_reference(payload)


# Planner/engine invariants are diagnostics, not answers. The status code keeps
# the failure visible to traces and evaluation; the reader gets plain language.
PLANNING_FAILURE_MESSAGES = frozenset(
    {
        "Comparison requires compatible individual summaries",
        "Invalid query count/status",
        "Invalid query count",
        "Invalid plan fields",
        "Invalid plan types",
        "Planner did not return a complete plan",
        "Planner selected an unresolved player reference",
        "Invalid serving execution",
    }
)
PLANNING_FAILURE = (
    "I couldn't turn this question into a reliable statistics query, so no numbers "
    "are shown. Naming the statistic and season helps, for example: "
    "“Who led the league in points per game in 2025-26?”"
)
DATA_FAILURE = (
    "The data for this question failed validation, so no numbers are shown. "
    "This is a data problem, not a problem with how the question was asked."
)


def user_message(message: str, status: str) -> str:
    if status == "invalid_plan" or message in PLANNING_FAILURE_MESSAGES:
        return PLANNING_FAILURE
    if status in ("invalid_evidence", "invalid_data", "duplicate_grain"):
        return DATA_FAILURE
    return message


def _payload(message: str, status: str) -> dict[str, Any]:
    from app.agent.payload import answer_payload

    return answer_payload(user_message(message, status), status=status)


def qualification_label(cohort):
    if cohort.get("team_game_percent"):
        return f"minimum {cohort['team_game_percent']}% of team games"
    return f"minimum {cohort['min_games']} games"


def leader_statement(section, metric, scope, names):
    """Lead a ranking with who ranked first; the table carries the rest."""
    ranked = [
        r for r in section["rows"] if r.get("rank") and r["display_value"] is not None
    ]
    leaders = [r for r in ranked if r["rank"] == 1]
    if not leaders:
        return []

    def shown(row):
        unit = "%" if metric["unit"] == "ratio" else ""
        return f"{row['display_value']:.1f}{unit}"

    per = " per game" if scope["aggregation"] == "average" else ""
    who = " and ".join(names.get(r["player_id"], str(r["player_id"])) for r in leaders)
    population = f" for {scope['team_abbr']}" if scope.get("team_abbr") else ""
    lead = (
        f"{who} ranked first{population} with {shown(leaders[0])} {metric['label']}{per}"
        + (
            f" across {leaders[0]['valid_games']} games"
            if len(leaders) == 1 and scope["aggregation"] == "average"
            else ""
        )
        + (" (tied)." if len(leaders) > 1 else ".")
    )
    runner = next((r for r in ranked if r["rank"] > 1), None)
    if runner:
        lead += f" Next: {names.get(runner['player_id'], str(runner['player_id']))} ({shown(runner)})."
    cohort = section["cohort"]
    lead += f" {cohort['size']} players qualified ({qualification_label(cohort)})."
    return [lead]


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
    if evidence.get("kind") == "game_performance":
        from app.agent.game_performance import render_game_performance

        return render_game_performance(evidence)
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
    from app.agent.scoring_report import game_log_context, log_summary

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
        if scope["operation"] == "game_log":
            context = game_log_context(scope)
            section["scope_presentation"] = "calendar_dates"
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
            statements.append(log_summary(section, name))
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
            if len(sections) == 1 and section["status"] == "ok":
                payload["conversation_context"] = {
                    "analysis_type": "game_log",
                    "players": [
                        {k: player[k] for k in ("player_id", "player_name")}
                        for player in players
                        if player["player_id"] == scope["player_id"]
                    ],
                    "scope": scope,
                    "metrics": [metric["key"]],
                }
                if section.get("scoring_sources"):
                    payload["followups"] = [
                        "Show scoring game by game",
                        "Show me his assists game by game.",
                    ]
            continue
        cohort = section["cohort"]
        payload["assumptions"].append(
            f"Ranking cohort: {cohort['size']} eligible players; {qualification_label(cohort)}; {cohort['excluded_players']} excluded."
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
        elif scope["operation"] == "rank":
            statements.extend(leader_statement(section, metric, scope, names))
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
    from app.agent.claim_renderers import metric_tables

    metric_tables(payload, players)
    from app.agent.game_filters import filter_description

    for _, section in sections:
        if note := filter_description(section["scope"]):
            if note not in payload["assumptions"]:
                payload["assumptions"].append(note)
    return payload


def _availability_refusal(question: str, trace: Any) -> dict[str, Any] | None:
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
    return None


@dataclass
class _SemanticTurnState:
    original: str
    question: str
    conversation_id: str | None = None
    client: Any = None
    model: str = ""
    selected_player: dict[str, Any] | None = None
    trace: Any = None
    progress_callback: Any = None
    started: float = field(default_factory=monotonic)
    award_request: bool = False
    store: ConversationStore | None = None
    pending: PendingClarification | None = None
    context: dict[str, Any] = field(default_factory=dict)
    resume_error: OverviewClarification | None = None
    leader_plan: dict[str, Any] | None = None
    ranked: dict[str, Any] | None = None
    dated_points_eligible: bool = False
    identity_replay_question: str = ""
    inherited: dict[str, Any] = field(default_factory=dict)
    route_shape: str | None = None
    route_source: str = "deterministic"
    request_question: str = ""
    retained_series_plan: DerivedSeriesPlan | None = None
    award: dict[str, Any] | None = None
    intent: dict[str, Any] = field(default_factory=dict)
    requested_comparison: dict[str, Any] = field(default_factory=dict)
    position_group: str | None = None
    time_assumptions: list[str] = field(default_factory=list)
    period_options: list[dict[str, Any]] = field(default_factory=list)
    planner_original: str = ""
    split: str | None = None
    comparison: list[str] | None = None
    overview: dict[str, Any] | None = None
    seasons: list[str] = field(default_factory=list)
    metric_window: dict[str, Any] = field(default_factory=dict)
    named_seasons: bool = False
    allow_fallback: bool = False
    prepared_attempt: PreparedAttempt | None = None
    load_evidence: Callable[[list[str]], tuple[dict[str, Any], Evidence]] | None = None
    snapshot: dict[str, Any] = field(default_factory=dict)
    plan: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] = field(default_factory=dict)
    players: list[dict[str, Any]] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)
    evidence: Evidence | None = None
    dev_phase_notice: str | None = None
    notice: str | None = None
    publication_pin: str | None = None
    pending_publication: str | None = None
    retry_plan: dict[str, Any] | None = None
    restored_clarification: bool = False
    publication: Any = None
    publication_stat: Any = None
    publication_opened: bool = False
    game_question: Any = None


class SemanticAsk:
    def __init__(self, settings: Any, warehouse: Any, conversation_store: Any = None):
        self.settings = settings
        self.warehouse = warehouse
        self.store = conversation_store

    @name_matching_entrypoint
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
        turn = _SemanticTurnState(
            original=question,
            question=question,
            client=client,
            model=model,
            conversation_id=conversation_id,
            selected_player=selected_player,
            trace=trace,
            progress_callback=progress_callback,
        )
        if early_payload := self.precheck_semantic_route(turn):
            return early_payload
        try:
            served = self._load_evidence_and_players(turn)
            if not served:
                self._execute_semantic_branch(turn)
            self._enrich_payload(turn)
        except (PlayerNameError, SemanticError) as exc:
            return self._finalize_payload(turn, error=exc)
        return self._finalize_payload(turn)

    def precheck_semantic_route(
        self, turn: _SemanticTurnState
    ) -> dict[str, Any] | None:
        if refusal := _availability_refusal(turn.original, turn.trace):
            return refusal
        self._prepare_turn_context(turn)
        try:
            return self._resolve_scope_and_shape(turn)
        except (PlayerNameError, SemanticError) as exc:
            return self._finalize_payload(turn, error=exc)

    def preview_route(self, question: str, conversation_id: str) -> dict[str, Any]:
        # Resolve pre-evidence routing for the offline route evaluator.
        turn = _SemanticTurnState(question, question, conversation_id=conversation_id)
        if early_payload := self.precheck_semantic_route(turn):
            return early_payload
        assert turn.route_shape is not None
        payload = {"status": "ok"} if turn.overview else {}
        return route_hint(
            payload, route=SEMANTIC_LABELS[turn.route_shape], source=turn.route_source
        )

    def _prepare_turn_context(self, turn: _SemanticTurnState) -> None:
        original = question = turn.original
        conversation_id = turn.conversation_id
        selected_player = turn.selected_player
        progress_callback = turn.progress_callback
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
        replay_publication, restored_clarification = identity_replay_scope()
        publication_pin = context.get("publication_id") or replay_publication
        if award_request:
            context = {}
            pending = None
        pending_publication = pending.publication_id if pending else None
        publication_pin = publication_pin or pending_publication
        retry_plan = None
        if (
            pending is not None
            and pending_publication
            and pending.question == original
            and (pending.query_plan or {}).get("publication_retry")
        ):
            retry_plan = pending.query_plan
            pending = None
            restored_clarification = True
        elif pending and pending_publication:
            restored_clarification = bool(
                (pending.query_plan or {}).get("restored_publication_clarification")
            )
        if restored_clarification:
            context = {}
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
        ranked = None
        if context and not selected_player and not resumed_overview:
            from app.agent.reference_planner import conversation_leader_plan

            ranked = conversation_leader_plan(original, context, self.settings.season)
            if ranked:
                leader_plan = ranked
                context = {}
                if ranked["queries"]:
                    # Load exactly the archive the inherited population refers to.
                    question = (
                        f"{original.rstrip('?.! ')} ({ranked['queries'][0]['season']})"
                    )
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
        identity_replay_question = question if resumed_overview else original
        if pending:
            if selected_player:
                question = pending.question
            else:
                question = f"{pending.question}\nClarification: {question}"
            identity_replay_question = question
        elif store and not context and not resumed_overview:
            if turns and re.search(
                r"\b(his|him|their|them|instead|same)\b", question, re.I
            ):
                dated_points_eligible = False
                # Without authorized successful scope, keep the unresolved current
                # question. Prior question text must not cross the provider boundary.
                question = original
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
        if selected_player and not re.search(r"\nSelected player:", question):
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
        turn.question = turn.request_question = question
        turn.selected_player = selected_player
        turn.award_request = award_request
        turn.store = store
        turn.pending = pending
        turn.context = context
        turn.resume_error = resume_error
        turn.leader_plan = leader_plan
        turn.ranked = ranked
        turn.dated_points_eligible = dated_points_eligible
        turn.identity_replay_question = identity_replay_question
        turn.inherited = inherited
        turn.route_shape = route_shape
        turn.route_source = route_source
        turn.publication_pin = publication_pin
        turn.pending_publication = pending_publication
        turn.retry_plan = retry_plan
        turn.restored_clarification = restored_clarification

    def _resolve_scope_and_shape(
        self, turn: _SemanticTurnState
    ) -> dict[str, Any] | None:
        series_followup = turn.context.get(
            "analysis_type"
        ) == "derived_series" and inherits_analysis(turn.original)
        if series_followup:
            from app.agent.derived_series import (
                derived_series_followup_plan,
                derived_series_question,
            )

            turn.retained_series_plan = derived_series_followup_plan(
                turn.original, turn.context
            )
            if turn.retained_series_plan is None:
                raise SemanticError(
                    "clarification_required",
                    "Restate the player, metric and series operation for this follow-up; the requested series change is not supported.",
                )
            turn.question = derived_series_question(turn.retained_series_plan)
            turn.request_question = turn.question
            turn.inherited = {
                "seasons": turn.retained_series_plan.query.get("seasons")
                or [turn.retained_series_plan.query["season"]]
            }
        if turn.resume_error:
            raise turn.resume_error
        prior_award = turn.context.get("award_evidence") or {}
        if (
            prior_award.get("performance_phase") == "Finals"
            and re.search(r"\b(?:his|him|he|their|them)\b", turn.original, re.I)
            and not explicit_phase(
                turn.original, include_both_cues=False, separated_postseason=False
            )
        ):
            turn.award = prior_award
            raise SemanticError(
                "unsupported_scope",
                "Finals-only performance is not connected. Specify regular-season or full-playoff performance to use game-level statistics.",
            )
        if turn.award_request:
            if turn.publication_pin:
                raise SemanticError(
                    "unsupported_scope",
                    "Start a separate analysis to look up an award or use a different source.",
                )
            turn.award = resolve_award_question(turn.original, self.settings.season)
            if not turn.award["performance_requested"]:
                return self._finalize_award_lookup(turn, turn.award)
            if turn.award["performance_phase"] == "Finals":
                raise SemanticError(
                    "unsupported_scope",
                    "Finals-only game scope is not connected. Ask for this winner's regular-season or full-playoff performance explicitly; I will not substitute either for Finals performance.",
                )
            turn.question = turn.award["performance_question"]
            turn.inherited = {}
            turn.selected_player = {
                k: turn.award[k] for k in ("player_id", "player_name")
            }
        from app.agent.overview_positions import requested_groups

        groups = requested_groups(turn.question)
        if len(groups) > 1:
            raise OverviewClarification(
                "groups",
                "Choose one comparison group: guards, wings/forwards, or bigs/centers. Multiple groups need separate comparisons.",
            )
        from app.agent.publication_ask_source import parse_publication_stat

        open_publication = getattr(self.warehouse, "open_publication", None)
        turn.publication_stat = (
            parse_publication_stat(turn.question.partition("\nSelected player: ")[0])
            if open_publication
            and not turn.inherited
            and not turn.award
            and (not turn.pending or turn.selected_player)
            else None
        )
        if (
            open_publication
            and turn.publication_stat
            and turn.publication_stat.metric in load_contract().metrics
        ):
            stat_seasons = (
                seasons_in_dates(
                    turn.publication_stat.first, turn.publication_stat.last
                )
                if turn.publication_stat.first
                else requested_seasons(turn.question, self.settings.season)
            )
            if turn.publication_pin or DEFAULT_SEASON in stat_seasons:
                turn.publication = open_publication(turn.publication_pin)
                turn.publication_opened = True
        if not turn.publication_opened or turn.publication is None:
            turn.publication_stat = None

        turn.intent = question_scope(turn.question)
        if turn.award:
            turn.intent["kind"] = "award_lookup_then_performance"
        if turn.intent["status"] and not (
            turn.publication_stat and turn.intent["kind"] == "quantity_aggregation"
        ):
            raise SemanticError(
                turn.intent["status"], turn.intent["message"] or "Unsupported question"
            )
        from app.agent.overview_positions import (
            comparison_intent,
            peer_question,
            requested_group,
        )
        from app.agent.time_defaults import resolve_time_defaults

        turn.requested_comparison = comparison_intent(turn.question)
        turn.position_group = requested_group(turn.question)
        initial_shape = semantic_shape(turn.question)
        turn.route_shape = turn.route_shape or initial_shape
        if turn.position_group and initial_shape != "overview":
            # A cohort constraint must not disappear in a scalar/league plan
            # or a two-player scorecard that cannot represent it.
            message = (
                "Choose a two-player scorecard without a group ranking, or name one player for a comparison with "
                + {
                    "Guard": "guards",
                    "Wing": "wings/forwards",
                    "Big": "bigs/centers",
                }[turn.position_group]
                + "."
                if initial_shape == "player_comparison"
                else "Position comparisons currently need one player and a broad scoring, playmaking, rebounding, or defensive-activity question. Please specify that comparison."
            )
            raise OverviewClarification("composition", message)
        turn.question, turn.time_assumptions, turn.period_options = (
            resolve_time_defaults(
                peer_question(turn.question)
                if initial_shape == "overview"
                else turn.question,
                context=turn.context,
            )
        )
        # The planner also reads the user's wording as current_question; give it
        # the same resolved period so "last year" is not shifted a second time
        # relative to the already-resolved selected season.
        try:
            turn.planner_original = resolve_time_defaults(
                turn.original, context=turn.context
            )[0]
        except SemanticError:
            turn.planner_original = turn.original
        if turn.position_group:
            turn.question += (
                " among "
                + {"Guard": "guards", "Wing": "wings", "Big": "big men"}[
                    turn.position_group
                ]
            )
        shape = "metrics" if turn.publication_stat else semantic_shape(turn.question)
        turn.route_shape = "award" if turn.award_request else shape
        turn.split = split_kind(turn.question) if shape == "player_split" else None
        turn.comparison = (
            comparison_sides(turn.question) if shape == "player_comparison" else None
        )
        turn.overview = (
            comparison_scope(turn.question, self.settings.season)
            if turn.comparison
            else overview_scope(
                turn.question,
                self.settings.season,
                comparison=turn.requested_comparison,
            )
            if shape == "overview"
            else None
        )
        if turn.overview and not turn.comparison:
            turn.overview["comparison_intent"] = turn.requested_comparison
        turn.seasons = (
            turn.overview["seasons"]
            if turn.overview
            else requested_seasons(turn.question, self.settings.season)
        )
        if turn.ranked and turn.ranked["queries"]:
            turn.seasons = [turn.ranked["queries"][0]["season"]]
        source_policy = get_source_policy(
            getattr(getattr(self.warehouse, "source", None), "source_policy", None)
        )
        if (
            source_policy is not None
            and turn.overview
            and not turn.comparison
            and not turn.overview.get("comparison_intent", {}).get("baseline_period")
            and plain_scoped_overview(turn.question)
        ):
            # A plain overview adds an optional prior-period comparison.
            # Keep every season in the requested window, even if disallowed,
            # so policy still rejects required scope rather than shortening it.
            # Retain overview's original seasons/previous bounds: missing
            # baseline coverage must withhold changes and comparison insights.
            turn.seasons = [
                s
                for s in turn.seasons
                if s in dict(source_policy.relations)
                or (
                    season_bounds(s)[0] <= turn.overview["end"]
                    and season_bounds(s)[1] >= turn.overview["start"]
                )
            ]
        if turn.split:
            from app.agent.player_splits import split_seasons

            turn.seasons = split_seasons(turn.question, self.settings.season)
        if turn.inherited.get("seasons") and not turn.overview:
            turn.seasons = [s for s in turn.inherited["seasons"] if s]
        turn.named_seasons = bool(
            re.search(r"\b20\d{2}[-–/](?:20\d{2}|\d{2})\b(?![-/–]\d)", turn.question)
        )
        if not turn.overview and not turn.split:
            from app.agent.game_performance import parse_game_question

            turn.game_question = parse_game_question(turn.question)
            turn.metric_window = (
                turn.game_question.dates
                if turn.game_question
                else explicit_scope(turn.question)
            )
            if turn.game_question and turn.game_question.relative:
                from app.agent.game_performance import anchor_game_question

                turn.question = anchor_game_question(
                    turn.question, turn.game_question.day
                )
                turn.identity_replay_question = turn.question.partition(
                    "\nSelected player: "
                )[0]
                turn.time_assumptions.append(
                    f"Using {turn.game_question.day} on the NBA Eastern calendar."
                )
            if turn.metric_window.get("window") == "last_n_months" and any(
                "rolling" in note for note in turn.time_assumptions
            ):
                from app.agent.overview_calendar import shift_months

                anchor_match = re.search(
                    r"\bas[ -]of\s+(\d{4}-\d{2}-\d{2})\b", turn.question, re.I
                )
                anchor = (
                    date.fromisoformat(anchor_match[1])
                    if anchor_match
                    else semantic_today()
                )
                turn.metric_window = {
                    **turn.metric_window,
                    "window": "date_range",
                    "start_date": (
                        shift_months(anchor, turn.metric_window["n"])
                        + timedelta(days=1)
                    ).isoformat(),
                    "as_of": anchor.isoformat(),
                    "n": None,
                }
            if turn.metric_window.get("window") == "date_range":
                turn.seasons = self._window_seasons(
                    turn, turn.metric_window["start_date"], turn.metric_window["as_of"]
                )
        primary = (
            [
                s
                for s in turn.seasons
                if season_bounds(s)[0] <= turn.overview["end"]
                and season_bounds(s)[1] >= turn.overview["start"]
            ]
            if turn.overview
            else list(turn.seasons)
        )
        if turn.publication_pin and not open_publication:
            raise SemanticError(
                "publication_unavailable",
                "This conversation's publication is unavailable on this host.",
            )
        if open_publication and (turn.publication_pin or DEFAULT_SEASON in primary):
            if not turn.publication_opened:
                turn.publication = open_publication(turn.publication_pin)
            if turn.publication is not None:
                turn.publication.load(primary)
                if turn.split or turn.position_group:
                    raise SemanticError(
                        "unsupported_coverage",
                        "This publication supports box-score summaries, game logs, and player comparisons. This additional analysis is unavailable.",
                    )
                if re.search(
                    r"\b(?:entire|whole|completed|full)\s+(?:regular[- ]season|season)\b",
                    turn.original,
                    re.I,
                ):
                    raise SemanticError(
                        "unsupported_coverage",
                        "This publication covers part of the season. Ask for a period within its verified dates.",
                    )
                if turn.overview:
                    explicit_phases = bool(
                        re.search(
                            r"\b(?:regular[ -]season|playoffs?|postseason|both phases|all phases)\b",
                            turn.question,
                            re.I,
                        )
                    )
                    if explicit_phases:
                        turn.publication.check_phase(turn.overview["phases"])
                    else:
                        turn.overview["phases"] = [turn.publication.phase]
                    if not (turn.overview["explicit_start"] or turn.inherited):
                        turn.overview["start"] = max(
                            turn.overview["start"], turn.publication.first
                        )
                    if not (turn.overview["explicit_end"] or turn.inherited):
                        turn.overview["end"] = min(
                            turn.overview["end"], turn.publication.last
                        )
                    turn.publication.check_window(
                        turn.overview["start"], turn.overview["end"]
                    )
                    if turn.overview["start"] > turn.overview["end"]:
                        raise SemanticError(
                            "unsupported_coverage",
                            "The requested period is outside the verified publication.",
                        )
                    turn.overview["chart_grain"] = (
                        "daily"
                        if (turn.overview["end"] - turn.overview["start"]).days < 31
                        else "monthly"
                    )
                    explicit_baseline = bool(
                        turn.requested_comparison.get("baseline_period")
                        or turn.requested_comparison.get("baseline_phases")
                    )
                    previous = (
                        turn.overview.get("previous_phases") or turn.overview["phases"]
                    )
                    if not explicit_phases and not explicit_baseline:
                        turn.overview["previous_phases"] = [turn.publication.phase]
                        previous = turn.overview["previous_phases"]
                        turn.overview["comparison_label"] = (
                            f"{turn.publication.phase} from {turn.overview['previous_start']} through {turn.overview['previous_end']}"
                        )
                    has_baseline = turn.publication.first <= turn.overview[
                        "previous_start"
                    ] <= turn.overview["previous_end"] <= turn.publication.last and set(
                        previous
                    ) == {turn.publication.phase}
                    if explicit_baseline and not has_baseline:
                        raise SemanticError(
                            "unsupported_coverage",
                            "The requested baseline is outside this publication. Start a separate analysis for another source.",
                        )
                    turn.overview["baseline_coverage_available"] = has_baseline
                    turn.overview["allow_recent_comparison"] = (
                        not has_baseline
                        and not turn.comparison
                        and not explicit_baseline
                        and not re.search(
                            r"\b(?:baseline|usual|normal|compared? (?:with|to|against)|versus)\b",
                            turn.original,
                            re.I,
                        )
                        and not turn.overview.get("recent_appearances")
                        and not turn.overview.get("first_appearances")
                    )
                else:
                    if (
                        turn.game_question
                        and not turn.publication.first
                        <= date.fromisoformat(turn.game_question.day)
                        <= turn.publication.last
                    ):
                        raise SemanticError(
                            "unsupported_coverage",
                            f"No verified publication coverage for {turn.game_question.day}. The authorized release covers {turn.publication.first} through {turn.publication.last}.",
                        )
                    turn.publication.check_window(
                        turn.metric_window.get("start_date"),
                        turn.metric_window.get("as_of"),
                    )
                turn.seasons = primary
        turn.allow_fallback = (
            turn.publication is None
            and not has_time_scope(turn.question)
            and not turn.inherited
            and not turn.pending
            and not turn.ranked
        )
        return None

    def _finalize_award_lookup(
        self, turn: _SemanticTurnState, award: dict[str, Any]
    ) -> dict[str, Any]:
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
        payload["conversation_id"] = turn.conversation_id
        payload["conversation_context"] = {
            "route": "award",
            "award_evidence": award,
            "players": [{k: award[k] for k in ("player_id", "player_name")}],
            "scope": {
                "season": award["season"],
                "start": season_bounds(award["season"])[0].isoformat(),
                "end": season_bounds(award["season"])[1].isoformat(),
                "phases": [award["performance_phase"]],
            },
        }
        if turn.store and turn.conversation_id:
            turn.store.clear_pending_clarification(turn.conversation_id)
            turn.store.append_turn(
                turn.conversation_id,
                question=turn.original,
                answer=payload["answer"],
                max_turns=self.settings.agent_conversation_max_turns,
                context=payload["conversation_context"],
            )
        route_hint(payload, route="award", source=turn.route_source)
        if turn.trace:
            turn.trace.set_plan(route="award", confidence=1.0)
            turn.trace.outcome = "answered"
        return payload

    def _window_seasons(
        self, turn: _SemanticTurnState, start: str, end: str
    ) -> list[str]:
        covered = seasons_in_dates(start, end)
        inherited_order = [s for s in turn.inherited.get("seasons", []) if s in covered]
        covered = inherited_order + [s for s in covered if s not in inherited_order]
        if turn.named_seasons and not set(covered).issubset(
            requested_seasons(turn.question, self.settings.season)
        ):
            raise SemanticError(
                "clarification_required",
                "The dates conflict with the named season. Please choose one scope.",
            )
        return covered

    def _load_evidence_and_players(self, turn: _SemanticTurnState) -> bool:
        """Load request evidence and report whether prepared serving resolved it."""
        if (
            turn.publication is None
            and hasattr(self.warehouse, "prepare")
            and not turn.overview
            and not turn.split
            and not turn.award
            and not turn.pending
            and not turn.inherited
            and not turn.selected_player
            and not turn.context
            and not turn.leader_plan
            and not re.search(r"\b(?:rolling|cumulative|trend)\b", turn.question, re.I)
            and not turn.allow_fallback
            and all(
                season_bounds(s)[1] < date.fromisoformat(capture_timestamp()[:10])
                for s in turn.seasons
            )
        ):

            def plan_prepared(identities, teams):
                turn.question, identities = prepare_names(turn.question, identities)
                return plan_question(
                    turn.client,
                    settings=self.settings,
                    model=turn.model,
                    question=turn.question,
                    selected_season=turn.seasons[0]
                    if len(turn.seasons) == 1
                    else self.settings.season,
                    players=identities,
                    teams=teams,
                    usage_callback=turn.trace.add_usage if turn.trace else None,
                    deterministic=getattr(
                        self.settings, "ask_deterministic_planning_enabled", False
                    ),
                    dated_points_eligible=turn.dated_points_eligible,
                    dated_points_question=(
                        prepare_names(turn.planner_original, identities, strict=False)[
                            0
                        ]
                        if turn.dated_points_eligible
                        else turn.planner_original
                    ),
                )

            turn.prepared_attempt = self.warehouse.prepare(
                plan_prepared,
                seasons=turn.seasons,
                question=turn.question,
                normalizer=lambda plan, appearances: normalize_metric_plan_scope(
                    plan,
                    metric_window=turn.metric_window,
                    appearances=appearances,
                    question=turn.question,
                    time_assumptions=turn.time_assumptions,
                    window_seasons=lambda start, end: self._window_seasons(
                        turn, start, end
                    ),
                    inherited=turn.inherited,
                ),
            )
        turn.load_evidence = (
            turn.publication.load
            if turn.publication is not None
            else self.warehouse.facts.load
            if turn.prepared_attempt is not None
            and turn.prepared_attempt.result is None
            else (
                lambda requested: self.warehouse.load_for_request(
                    requested, rich=bool(turn.overview or turn.split)
                )
            )
            if hasattr(self.warehouse, "load_for_request")
            else self.warehouse.load
        )
        if (
            turn.prepared_attempt is not None
            and turn.prepared_attempt.result is not None
        ):
            turn.seasons = list(turn.prepared_attempt.required_seasons) or turn.seasons
            turn.plan, turn.result, turn.players = (
                turn.prepared_attempt.plan,
                turn.prepared_attempt.result,
                turn.prepared_attempt.players,
            )
            turn.payload = render_answer(turn.result, turn.players)
            if turn.payload.get("player_profile"):
                turn.payload["player_profile"] = identity_profile(
                    turn.payload["player_profile"]["player"],
                    turn.prepared_attempt.appearances,
                )
            turn.snapshot = {"capture": turn.prepared_attempt.source_query}
            return True
        self._load_archive(turn)
        return False

    def _load_archive(self, turn: _SemanticTurnState) -> None:
        load_evidence = turn.load_evidence
        assert load_evidence is not None
        if turn.allow_fallback:
            used = self.settings.season
            try:
                turn.snapshot, turn.evidence = load_evidence(turn.seasons)
                turn.evidence.validate()
                if not any(r["season"] == used for r in turn.evidence.rows):
                    raise SemanticError(
                        "unsupported_coverage", f"No evidence for {used}"
                    )
            except SemanticError as exc:
                if exc.code != "unsupported_coverage":
                    raise
                turn.snapshot, turn.evidence, used = load_available_season(
                    load_evidence,
                    season_candidates(turn.question, self.settings.season),
                )
                turn.seasons = [used]
            if turn.overview and used != self.settings.season:
                turn.overview = (
                    comparison_scope(turn.question, used)
                    if turn.comparison
                    else overview_scope(
                        turn.question, used, comparison=turn.requested_comparison
                    )
                )
                turn.overview["seasons"] = turn.seasons
            if used != self.settings.season:
                turn.notice = fallback_notice(self.settings.season, used)
        else:
            try:
                turn.snapshot, turn.evidence = load_evidence(turn.seasons)
            except SemanticError as exc:
                primary_seasons = [
                    s
                    for s in turn.seasons
                    if turn.overview
                    and season_bounds(s)[0] <= turn.overview["end"]
                    and season_bounds(s)[1] >= turn.overview["start"]
                ]
                if (
                    exc.code != "unsupported_coverage"
                    or not primary_seasons
                    or primary_seasons == turn.seasons
                ):
                    raise
                # The requested period stays fixed; only an unavailable baseline
                # is omitted. build_overview withholds its comparisons.
                turn.snapshot, turn.evidence = load_evidence(primary_seasons)
        self._prepare_players(turn)

    def _prepare_players(self, turn: _SemanticTurnState) -> None:
        evidence = turn.evidence
        assert evidence is not None
        turn.players = (
            self.warehouse.players(evidence)
            if hasattr(self.warehouse, "players")
            else source_players(evidence)
        )
        if (
            turn.publication is None
            and not evidence.cohort_complete
            and turn.overview
            and not has_phase_cue(turn.question, "Playoffs", separated_postseason=False)
            and not re.search(r"\b(?:both|all phases)\b", turn.question, re.I)
        ):
            turn.overview = {
                **turn.overview,
                "phases": ["Regular Season"],
                "previous_phases": ["Regular Season"],
            }
            turn.dev_phase_notice = "This preview covers regular-season box scores only; this overview uses the regular season."
        if (
            turn.publication is None
            and not evidence.cohort_complete
            and (
                turn.comparison
                or turn.split
                or turn.position_group
                or re.search(
                    r"\b(rank|rankings|percentiles?|baseline|cohort|peers|compar(?:e[sd]?|ing|isons?)|versus|vs|other players)\b",
                    turn.question,
                    re.I,
                )
            )
        ):
            raise SemanticError(
                "unsupported_coverage",
                "League and peer comparisons are unavailable for partial ESPN dev coverage. Ask for a player's recorded box-score metrics.",
            )
        if turn.context.get("browser_recovered") and any(
            not any(
                p["player_id"] == hint["player_id"]
                and p["player_name"].casefold() == hint["player_name"].casefold()
                for p in turn.players
            )
            for hint in turn.context.get("players", [])
        ):
            raise SemanticError(
                "unsupported_coverage",
                "Saved player context does not match this source. Please restate the full player name and dates.",
            )
        if turn.pending and not turn.selected_player and not turn.overview:
            from app.agent.scope_policy import explicit_metric_mentions

            if (
                explicit_metric_mentions(turn.original) == {"pts"}
                and explicit_scope(turn.original).get("window") == "date_range"
                and len(
                    resolve_overview_player(turn.original, turn.players, evidence.rows)
                )
                == 1
            ):
                # A complete new dated request is not a terse scope reply.
                turn.pending = None
                turn.question = turn.original
                turn.dated_points_eligible = True
        if turn.pending and not turn.selected_player and not turn.overview:
            resolved = resolve_overview_player(
                turn.question, turn.players, evidence.rows
            )
            if len(resolved) == 1:
                turn.selected_player = resolved[0]
        if turn.selected_player and not turn.comparison:
            matches = [
                p
                for p in turn.players
                if p["player_id"] == turn.selected_player.get("player_id")
            ]
            if not matches:
                raise SemanticError(
                    "unsupported_coverage",
                    "Selected player is absent from this source scope",
                )
            # A selection resolves ambiguous aliases only to this source ID.
            chosen = matches[0]
            if (
                turn.award
                and unicodedata.normalize("NFKD", chosen["player_name"])
                .encode("ascii", "ignore")
                .lower()
                != unicodedata.normalize("NFKD", turn.award["player_name"])
                .encode("ascii", "ignore")
                .lower()
            ):
                raise SemanticError(
                    "invalid_evidence",
                    "Award identity does not match the warehouse player record.",
                )
            turn.players = [
                dict(
                    p,
                    aliases=[]
                    if p["player_id"] != chosen["player_id"]
                    else p["aliases"],
                )
                for p in turn.players
                if p["player_id"] == chosen["player_id"]
                or p["player_name"].casefold() != chosen["player_name"].casefold()
            ]
        if not turn.overview:
            restored_selection = (
                turn.selected_player
                if turn.publication is not None
                and turn.publication_pin
                and not turn.pending
                else None
            )
            turn.question, turn.players = prepare_names(
                turn.question,
                turn.players,
                selected=restored_selection,
                restored_choices=(
                    [
                        {**choice, "mention": mention}
                        for mention, choice in turn.retry_plan["choices"].items()
                    ]
                    if turn.retry_plan is not None and turn.publication is not None
                    else turn.context.get("publication_identity_choices", ())
                    if restored_selection
                    else ()
                ),
            )

    def _execute_semantic_branch(self, turn: _SemanticTurnState) -> None:
        from app.agent.derived_series import derived_series_plan
        from app.agent.game_filters import extract_game_filters

        evidence = turn.evidence
        assert evidence is not None
        extracted = extract_game_filters(turn.question) if not turn.split else None
        series_plan = (
            turn.retained_series_plan
            or derived_series_plan(
                extracted.cleaned_question,
                turn.seasons[0],
                [p["player_name"] for p in turn.players],
            )
            if extracted
            else None
        )
        if series_plan:
            assert extracted is not None
            self._execute_series(turn, series_plan, extracted.query_fields)
        elif re.search(r"\b(?:rolling|cumulative|trend)\b", turn.question, re.I):
            raise SemanticError(
                "unsupported_scope",
                "Specify one player, statistic and period for a rolling N-game average, cumulative count, or game trend.",
            )
        elif turn.split:
            turn.payload = answer_split(
                turn.question, evidence, turn.players, turn.seasons[0]
            )
            turn.plan = turn.payload["semantic_plan"]
            turn.result = {"status": turn.payload["status"]}
        elif turn.overview:
            self._execute_overview(turn, evidence, turn.overview)
        else:
            self._execute_metrics(turn, evidence)

    def _execute_series(
        self,
        turn: _SemanticTurnState,
        series_plan: DerivedSeriesPlan,
        query_fields: dict[str, Any],
    ) -> None:
        from app.agent.derived_series import render_derived_series_answer

        load_evidence = turn.load_evidence
        assert load_evidence is not None and turn.evidence is not None
        if turn.publication is not None:
            turn.publication.check_plan({"queries": [series_plan.query]})
        if series_plan.query.get("window") == "date_range":
            series_plan.query["seasons"] = seasons_in_dates(
                series_plan.query["start_date"], series_plan.query["as_of"]
            )
            if not set(series_plan.query["seasons"]).issubset(turn.seasons):
                turn.snapshot, turn.evidence = load_evidence(
                    series_plan.query["seasons"]
                )
                turn.seasons = series_plan.query["seasons"]
        turn.payload = render_derived_series_answer(
            series_plan,
            turn.evidence,
            turn.players,
            query_fields=query_fields,
        )
        turn.plan = {
            "status": turn.payload["status"],
            "queries": [series_plan.query],
            "model_calls": 0,
            "message": "Governed appearance series",
        }
        turn.result = {"status": turn.payload["status"]}

    def _execute_overview(
        self, turn: _SemanticTurnState, evidence: Evidence, overview: dict[str, Any]
    ) -> None:
        if turn.comparison:
            turn.payload = build_comparison(
                turn.question,
                evidence,
                turn.players,
                overview,
                turn.selected_player,
                (turn.pending.query_plan or {}).get("comparison_choices")
                if turn.pending
                else turn.retry_plan.get("comparison_choices")
                if turn.retry_plan is not None and turn.publication is not None
                else turn.context.get("publication_comparison_choices")
                if turn.publication is not None
                and turn.publication_pin
                and turn.selected_player
                and turn.context.get("browser_recovered")
                else None,
            )
        else:
            turn.payload = build_overview(
                turn.question, evidence, turn.players, overview, turn.selected_player
            )
        turn.plan = {
            "status": turn.payload["status"],
            "queries": [],
            "message": "Player comparison"
            if turn.comparison
            else "Performance overview",
            "model_calls": 0,
            "comparison_choices": turn.payload.get("comparison_choices", {}),
        }
        turn.result = {"status": turn.payload["status"]}

    def _execute_metrics(self, turn: _SemanticTurnState, evidence: Evidence) -> None:
        load_evidence = turn.load_evidence
        assert load_evidence is not None
        from app.agent.publication_ask_source import parse_publication_stat

        publication_plan = None
        if turn.publication is not None and turn.publication_stat:
            resolved_stat = parse_publication_stat(
                turn.question.partition("\nSelected player: ")[0]
            )
            if resolved_stat is None:
                raise SemanticError(
                    "invalid_scope", "Please restate the player and statistic."
                )
            publication_plan = turn.publication.stat_plan(resolved_stat)
        turn.plan = (
            turn.leader_plan
            or (turn.prepared_attempt.plan if turn.prepared_attempt else None)
            or publication_plan
            or plan_question(
                turn.client,
                settings=self.settings,
                model=turn.model,
                question=turn.question,
                selected_player_name=turn.selected_player["player_name"]
                if turn.selected_player
                else None,
                dated_points_eligible=turn.dated_points_eligible,
                deterministic=getattr(
                    self.settings,
                    "ask_deterministic_planning_enabled",
                    False,
                ),
                dated_points_question=(
                    prepare_names(turn.planner_original, turn.players, strict=False)[0]
                    if turn.dated_points_eligible
                    else turn.planner_original
                ),
                selected_season=turn.seasons[0]
                if len(turn.seasons) == 1
                else self.settings.season,
                usage_callback=turn.trace.add_usage if turn.trace else None,
                players=turn.players,
                # Stored route labels are routing state, not planner context.
                conversation_context={
                    k: v for k, v in turn.context.items() if k != "route"
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
        )
        if turn.pending:
            from app.agent.scope_policy import explicit_aggregation

            if not explicit_aggregation(turn.question) and any(
                q.get("metric") == "pts"
                and q.get("operation", "summary") == "summary"
                and q.get("window") == "date_range"
                and q.get("aggregation") == "total"
                for q in turn.plan.get("queries", [])
            ):
                raise SemanticError(
                    "clarification_required",
                    "Please restate the player and date interval, or specify total points versus points per game.",
                )
        turn.plan = normalize_reference_plan(
            turn.plan, turn.inherited.get("excluded_player_ids", []), turn.players
        )
        turn.plan = normalize_metric_plan_scope(
            turn.plan,
            metric_window=turn.metric_window,
            appearances=evidence.rows,
            question=turn.question,
            time_assumptions=turn.time_assumptions,
            window_seasons=lambda start, end: self._window_seasons(turn, start, end),
            inherited=turn.inherited,
        )
        if turn.publication is not None:
            turn.publication.check_plan(turn.plan)
        required_seasons = sorted(
            set(turn.seasons).union(
                s
                for q in turn.plan.get("queries", [])
                if q.get("window") == "date_range"
                for s in q["seasons"]
            ),
            reverse=True,
        )
        if required_seasons and not set(required_seasons).issubset(turn.seasons):
            turn.snapshot, evidence = load_evidence(required_seasons)
            turn.evidence = evidence
            turn.seasons = required_seasons
            turn.players = (
                self.warehouse.players(evidence)
                if hasattr(self.warehouse, "players")
                else source_players(evidence)
            )
            if turn.selected_player:
                turn.players = [
                    p
                    for p in turn.players
                    if p["player_id"] == turn.selected_player["player_id"]
                    or p["player_name"].casefold()
                    != turn.selected_player["player_name"].casefold()
                ]
        turn.question, turn.players = prepare_names(turn.question, turn.players)
        turn.result = execute_plan(
            turn.plan,
            evidence,
            turn.players,
            **(
                {"shadow": self.warehouse.serving_shadow}
                if turn.publication is None
                and getattr(self.warehouse, "serving_shadow", None) is not None
                else {}
            ),
        )
        self._retry_metrics_in_older_season(turn, load_evidence)
        evidence_after_retry = turn.evidence
        assert evidence_after_retry is not None
        turn.payload = render_answer(turn.result, turn.players)
        if (
            publication_plan
            and turn.plan.get("status") == "compare"
            and turn.result["status"] == "ok"
            and not turn.payload.get("player_profile")
        ):
            profiles = []
            for side in ("current", "baseline"):
                section = turn.result["evidence"][side]
                player_id = section["scope"]["player_id"]
                player = next(p for p in turn.players if p["player_id"] == player_id)
                game_ids = set(section["rows"][0]["game_ids"])
                profiles.append(
                    identity_profile(
                        player,
                        [row for row in evidence.rows if row["game_id"] in game_ids],
                    )
                )
            turn.payload["player_profiles"] = profiles
        if (
            turn.payload["status"] == "ok"
            and len(turn.inherited.get("excluded_player_ids", [])) == 1
        ):
            add_leaderboard_reference(
                turn.payload,
                turn.result,
                evidence_after_retry,
                turn.players,
                turn.inherited["excluded_player_ids"][0],
            )
        if turn.inherited.get("assumption") and turn.payload["status"] == "ok":
            turn.payload["assumptions"].append(turn.inherited["assumption"])
            turn.payload["answer"] = (
                turn.inherited["assumption"] + "\n\n" + turn.payload["answer"]
            )
        if (
            turn.payload.get("player_profile")
            and (turn.payload.get("semantic_evidence") or {}).get("kind")
            != "game_performance"
        ):
            player = turn.payload["player_profile"]["player"]
            turn.payload["player_profile"] = identity_profile(
                player, evidence_after_retry.rows
            )

    def _retry_metrics_in_older_season(
        self,
        turn: _SemanticTurnState,
        load_evidence: Callable[[list[str]], tuple[dict[str, Any], Evidence]],
    ) -> None:
        # Reuse the validated scope plan; only the default season changes.
        # No additional model calls, threshold relaxation, or date changes.
        retryable_plan = bool(turn.plan.get("queries")) and all(
            q.get("season") == turn.seasons[0]
            and not q.get("seasons")
            and q.get("window", "season_to_date") == "season_to_date"
            and not q.get("as_of")
            and not q.get("start_date")
            for q in turn.plan["queries"]
        )
        if turn.allow_fallback and retryable_plan:
            for older in season_candidates(turn.question, self.settings.season):
                if older >= turn.seasons[0] or turn.result["status"] not in (
                    "unsupported_coverage",
                    "no_observations",
                ):
                    continue
                try:
                    candidate_snapshot, candidate_evidence, _ = load_available_season(
                        load_evidence, [older]
                    )
                except SemanticError as exc:
                    if exc.code == "unsupported_coverage":
                        continue
                    raise
                try:
                    candidate_players = prepare_names(
                        turn.question, source_players(candidate_evidence)
                    )[1]
                except PlayerNameError:
                    # Missing older-roster identity must not replace the current result.
                    continue
                candidate_plan = {
                    **turn.plan,
                    "queries": [dict(q, season=older) for q in turn.plan["queries"]],
                }
                candidate_result = execute_plan(
                    candidate_plan, candidate_evidence, candidate_players
                )
                if candidate_result["status"] == "ok":
                    turn.plan, turn.result = candidate_plan, candidate_result
                    turn.snapshot, turn.evidence, turn.players = (
                        candidate_snapshot,
                        candidate_evidence,
                        candidate_players,
                    )
                    turn.seasons = [older]
                    turn.notice = fallback_notice(self.settings.season, older)
                    break
                if candidate_result["status"] not in (
                    "unsupported_coverage",
                    "no_observations",
                ):
                    break

    def _enrich_payload(self, turn: _SemanticTurnState) -> None:
        payload = turn.payload
        option_rows: Sequence[Mapping[str, Any]]
        if (
            turn.prepared_attempt is not None
            and turn.prepared_attempt.result is not None
        ):
            option_rows = turn.prepared_attempt.appearances
        else:
            assert turn.evidence is not None
            option_rows = turn.evidence.rows
        if not turn.overview and not turn.split:
            for option in payload.get("clarification_options", []):
                option["team_abbr"] = identity_profile(option, option_rows)[
                    "player"
                ].get("team_abbr")
                option["label"] = option["player_name"]
        if turn.award:
            payload["answer"] = award_intro(turn.award) + "\n\n" + payload["answer"]
            payload["award_evidence"] = turn.award
            payload["assumptions"].append(
                "Award season: "
                + turn.award["season"]
                + "; "
                + turn.award["season_basis"]
                + ". Performance is computed from warehouse game records."
            )
        if turn.time_assumptions and payload.get("status") in (
            "ok",
            "unsupported_coverage",
        ):
            payload.setdefault("scope_assumptions", []).extend(turn.time_assumptions)
            payload["period_options"] = turn.period_options
            payload["assumptions"].extend(turn.time_assumptions)
        payload["semantic_plan"] = turn.plan
        payload["question_intent"] = turn.intent
        if turn.notice:
            payload["answer"] = turn.notice + "\n\n" + payload["answer"]
            payload["assumptions"].append(turn.notice)
            payload["season_fallback"] = {
                "requested": self.settings.season,
                "used": turn.seasons[0],
            }
        if turn.snapshot.get("coverage_notes"):
            note = " ".join(
                turn.snapshot["coverage_notes"]
                + ([turn.dev_phase_notice] if turn.dev_phase_notice else [])
            )
            identity_clarification = (
                bool(payload.get("clarification_options"))
                or payload["status"] == "not_found"
                or payload.get("semantic_plan", {}).get("name_matching")
            )
            if not identity_clarification and turn.publication is None:
                payload["answer"] = note + "\n\n" + payload["answer"]
                payload.setdefault("scope_assumptions", []).extend(
                    turn.snapshot.get("scope_assumptions", [])
                )
            payload["assumptions"].extend(turn.snapshot["coverage_notes"])
            payload["source_coverage"] = turn.snapshot.get("review_metadata")
        payload["source_query"] = {
            k: v for k, v in turn.snapshot.get("capture", {}).items() if k != "sql"
        }
        payload["tool_calls"] = [
            {
                "name": "governed_metrics",
                "args": {"seasons": turn.seasons},
                "status": turn.result["status"],
                "result": {
                    "contract": "nba_semantics/0.1",
                    "source_query": payload["source_query"],
                },
            }
        ]

    def _finalize_payload(
        self,
        turn: _SemanticTurnState,
        *,
        error: PlayerNameError | SemanticError | None = None,
    ) -> dict[str, Any]:
        payload = turn.payload
        overview_pending = None
        if isinstance(error, PlayerNameError):
            source_query = getattr(error, "source_query", None)
            if source_query:
                turn.snapshot = {"capture": source_query}
            # Replay raw wording so time defaults and conversation context are
            # resolved once again, with their original disclosures intact.
            turn.question = turn.identity_replay_question
            payload = identity_payload(
                error,
                turn.question,
                turn.conversation_id,
                turn.trace.model_calls
                if turn.trace
                else turn.plan.get("model_calls", 0),
            )
            payload["assumptions"].extend(turn.snapshot.get("coverage_notes", []))
            payload["source_coverage"] = turn.snapshot.get("review_metadata")
            payload["source_query"] = {
                k: v for k, v in turn.snapshot.get("capture", {}).items() if k != "sql"
            }
            payload["tool_calls"] = [
                {
                    "name": "governed_metrics",
                    "args": {"seasons": turn.seasons},
                    "status": payload["status"],
                    "result": {"source_query": payload["source_query"]},
                }
            ]
        elif isinstance(error, SemanticError):
            if isinstance(error, OverviewClarification):
                overview_pending = {"overview_clarification": error.kind}
                turn.question = turn.request_question
            payload = _payload(str(error), error.code)
            if turn.award:
                payload["answer"] = (
                    award_intro(turn.award)
                    + "\n\nPerformance could not be verified: "
                    + str(error)
                )
                payload["award_evidence"] = turn.award
                payload["answerability"] = "partial"
        if turn.publication is not None:
            try:
                self.warehouse.recheck_publication(turn.publication)
            except SemanticError as exc:
                payload = _payload(str(exc), exc.code)
            else:
                payload["publication_id"] = turn.publication.publication_id
                payload["source_coverage"] = turn.publication.provenance
        if payload["status"] == "publication_unavailable" and turn.publication_pin:
            payload["publication_retry"] = {
                "publication_id": turn.publication_pin,
                "question": turn.pending.question if turn.pending else turn.original,
                **(turn.selected_player or {}),
                "comparison_choices": (
                    (turn.pending.query_plan or {}).get("comparison_choices")
                    if turn.pending
                    else turn.context.get("publication_comparison_choices")
                ),
                "choices": {
                    choice["mention"]: choice
                    for choice in turn.context.get("publication_identity_choices", ())
                }
                if turn.selected_player
                else {},
            }
            if turn.retry_plan is not None:
                payload["publication_retry"].update(
                    restore_clarification=True,
                    choices=turn.retry_plan["choices"],
                    comparison_choices=turn.retry_plan.get("comparison_choices"),
                )
        if turn.trace:
            turn.trace.add_tool(
                name="governed_metrics",
                args={"selected_season": self.settings.season},
                status=payload["status"],
                duration_ms=round((monotonic() - turn.started) * 1000),
                result={
                    "status": payload["status"],
                    "source_query": payload.get("source_query"),
                },
            )
        if turn.progress_callback:
            turn.progress_callback(
                {
                    "type": "tool_end",
                    "name": "governed_metrics",
                    "status": payload["status"],
                    "duration_ms": round((monotonic() - turn.started) * 1000),
                }
            )
        payload["conversation_id"] = turn.conversation_id
        if payload["status"] == "clarification_required":
            payload["clarification_question"] = turn.question
            payload["overview_clarification"] = {
                "version": 1,
                "kind": (overview_pending or {}).get("overview_clarification"),
            }
        payload["agent_plan"] = {
            "route": "governed_metrics",
            "confidence": 1.0,
            "needs_clarification": payload["status"] == "clarification_required",
        }
        if turn.route_shape is None:
            turn.route_shape = semantic_shape(turn.request_question)
        route_label = SEMANTIC_LABELS[turn.route_shape]
        reason_code = (
            f"overview_{overview_pending['overview_clarification']}"
            if overview_pending and overview_pending.get("overview_clarification")
            else status_reason(payload)
        )
        route_hint(
            payload,
            route=route_label,
            source=turn.route_source,
            reason_code=reason_code,
        )
        if turn.trace:
            turn.trace.set_plan(route=route_label, confidence=1.0)
            turn.trace.outcome = (
                "clarified"
                if payload["status"] == "clarification_required"
                else "answered"
                if payload["status"] == "ok"
                else payload["status"]
            )
        if turn.store and turn.conversation_id:
            if payload["status"] == "clarification_required":
                pending_plan = overview_pending or payload.get("semantic_plan")
                if turn.restored_clarification:
                    pending_plan = {
                        **(pending_plan or {}),
                        "restored_publication_clarification": True,
                    }
                turn.store.set_pending_clarification(
                    turn.conversation_id,
                    question=turn.question,
                    query_plan=pending_plan,
                    route=route_label,
                    candidates=payload.get("clarification_options"),
                    publication_id=payload.get("publication_id"),
                )
            elif not (
                turn.pending_publication
                and payload["status"] == "publication_unavailable"
            ):
                turn.store.clear_pending_clarification(turn.conversation_id)
            if payload["status"] == "ok":
                next_context = analysis_context(turn.original, payload)
                if not next_context.get("players"):
                    next_context["players"] = turn.context.get("players", [])
                if turn.publication is not None:
                    next_context["publication_id"] = turn.publication.publication_id
                    if isinstance(next_context.get("scope"), dict):
                        next_context["scope"].setdefault(
                            "season", turn.publication.season
                        )
                payload["conversation_context"] = next_context
                turn.store.append_turn(
                    turn.conversation_id,
                    question=saved_context_question(turn.original, payload),
                    answer=payload["answer"],
                    max_turns=self.settings.agent_conversation_max_turns,
                    context=next_context,
                )
        return payload


def try_published_three_point_leaders(
    agent: StatsAgent,
    cleaned_question: str,
    context: dict[str, Any],
    conversation_id: str | None,
    *,
    runtime: Any,
    selected_player: dict[str, Any] | None,
    keep_pending: bool,
) -> dict[str, Any] | None:
    from app.agent.payload import answer_payload

    published = getattr(agent.repo, "active_three_point_leaders", None)
    if published and runtime is None and selected_player is None:
        from app.seasons import DEFAULT_SEASON
        from app.serving.three_point_leaders import (
            asks_for_three_point_leaders,
        )

        if (
            agent.settings.season == DEFAULT_SEASON
            and get_source_policy(getattr(agent.repo, "source_policy", None)) is None
            and not keep_pending
            and asks_for_three_point_leaders(cleaned_question, DEFAULT_SEASON, context)
        ):
            try:
                answer = published()
            except (ValueError, OSError, KeyError):
                answer = answer_payload(
                    "The current publication has no verified three-point leaderboard available.",
                    status="unsupported_coverage",
                )
            if (
                context.get("publication_id")
                and answer is not None
                and (answer.get("status") == "ok" or answer.get("publication_id"))
                and answer.get("publication_id") != context["publication_id"]
            ):
                answer = answer_payload(
                    "This conversation uses a different publication. Start a separate analysis for the current leaderboard.",
                    status="publication_unavailable",
                )
            if answer is not None:
                answer["conversation_id"] = conversation_id
                route_hint(answer, route="governed_metrics", source="deterministic")
                if (
                    conversation_id
                    and agent.conversation_store
                    and answer["status"] == "ok"
                ):
                    next_context = analysis_context(cleaned_question, answer)
                    answer["conversation_context"] = next_context
                    agent.conversation_store.append_turn(
                        conversation_id,
                        question=cleaned_question,
                        answer=answer["answer"],
                        context=next_context,
                        max_turns=agent.settings.agent_conversation_max_turns,
                    )
                return answer
    return None
