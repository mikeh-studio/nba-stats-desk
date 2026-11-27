"""Opponent splits computed from governed player-game facts."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from math import isclose
from statistics import mean, pstdev
from typing import Any

from app.agent.catalog import load_semantic_catalog
from app.agent.game_filters import extract_game_filters
from app.agent.name_matching import mask_player_tokens
from app.agent.payload import answer_payload
from app.agent.player_comparison import comparison_sides
from app.agent.scope_policy import (
    COLLECTIVE_MATCHUP,
    explicit_aggregation,
    explicit_metric_mentions,
    explicit_phase,
    explicit_scope,
    extract_metrics,
    requested_playoff_round,
    round_scope_message,
)
from app.agent.semantic_serving import requested_seasons, seasons_in_dates
from app.agent.semantics import (
    Evidence,
    Metric,
    Query,
    SemanticError,
    _game_result,
    aggregate,
    load_contract,
    select_query_rows,
)

VERSION = "opponent_splits/1"
DEFAULT_METRICS = ("pts", "ts_pct", "fg_pct", "fg3_pct", "ast", "reb", "plus_minus")
SCORE_METRICS = ("pts", "ts_pct", "plus_minus")
SMALL_SAMPLE = 3
_METRIC_REPLY = "\nOpponent split metrics: "
_OPPONENT_CUE = re.compile(
    r"\b(?:opponent[ -](?:splits?|breakdown)|(?:split|breakdown)s? (?:by|across) opponent"
    rf"|{COLLECTIVE_MATCHUP}"
    r"|by opponent|(?:toughest|hardest|easiest|best|worst)\s+(?:matchups?|opponents?)"
    r"|(?:which|what) (?:opponent|team)\b[^?\n]*\bstruggl\w*[^?\n]*\bagainst"
    r"|\bstruggl\w*\s+(?:the\s+)?(?:most\s+)?against which (?:opponent|team))\b",
    re.I,
)
_LAST_GAMES = re.compile(
    r"\b(?:last|past|most recent)\s+(\d+)\s+(?:(?:home|away|road|playoffs?|regular[ -]season)\s+)?(?:games?|appearances?)\b",
    re.I,
)
_UNCONSUMED_GAME_WINDOW = re.compile(
    r"\b(?:last|past|most recent|first|previous|prior|preceding|next)\s+"
    r"(?:(?!(?:round|season|games?|appearances?|and|or)\b)[\w-]+\s+){0,4}(?:games?|appearances?)\b",
    re.I,
)
_OPPONENT_FILTER = re.compile(
    r"\b(?:against|versus|vs\.?)\s+"
    r"(?!(?:most|the most|which (?:team|opponent)|in|during|over|from|at|on)\b)\w",
    re.I,
)
_LEAGUE_REFERENCE = re.compile(
    r"\b(?:league(?:'s|’s)?\s+(?:average|baseline)|"
    r"(?:relative to|compared (?:to|with)|versus|vs\.?)\s+(?:the\s+)?league)\b",
    re.I,
)
_WITHIN_GAME_SCOPE = re.compile(
    r"\b(?:quarters?|halves|half|overtime|clutch|q[1-4]|[1-4]q"
    r"|(?:first|second|third|fourth|[1-4](?:st|nd|rd|th))\s+period"
    r"|period\s+[1-4])\b",
    re.I,
)
_UNSUPPORTED_SCOPE = re.compile(
    r"\b(?:career|all[ -]time|excluding|except|head[ -]to[ -]head"
    r"|(?:last|past|preceding) [\w-]+ weeks?|play[ -]in|preseason|playoff series|NBA Cup"
    r"|(?:in|during|for) (?:playoff )?game [1-7]"
    r"|today|yesterday|tonight|since|before|after|until)\b",
    re.I,
)


def wants_opponent_splits(question: str) -> bool:
    return bool(_OPPONENT_CUE.search(question)) and not comparison_sides(question)


def opponent_splits_followup(question: str, previous: str | None) -> str | None:
    """Carry the previous split scope forward only for a metric-only reply."""
    if not previous:
        return None
    metric_text = re.sub(
        r"^(?:and|what about|how about|show(?: me)?|compare|now)\s+",
        "",
        question.strip(),
        flags=re.I,
    ).strip(" ?.!")
    rest, extracted = extract_metrics(metric_text)
    keys = explicit_metric_mentions(metric_text) | set(extracted)
    # Also recognize an exact catalog alias beyond the shared split grammar.
    metric_labels = {
        str(alias).casefold()
        for metric in load_semantic_catalog().list_metrics()
        if metric["key"] in keys
        for alias in (metric["key"], metric["label"], *metric.get("aliases", []))
    }
    if keys and (
        metric_text.casefold() in metric_labels
        or not re.sub(r"\band\b|[\s,&/]", "", rest, flags=re.I)
    ):
        return previous + _METRIC_REPLY + metric_text
    return None


def opponent_split_query(question: str, season: str) -> Query:
    """Resolve scope before starting a governed evidence read."""
    question = question.partition(_METRIC_REPLY)[0].split("\nSelected player:", 1)[0]
    if _LEAGUE_REFERENCE.search(question):
        raise SemanticError(
            "unsupported_scope",
            "Opponent splits do not support a league-average comparison. Ask for opponent splits or a league comparison in separate questions.",
        )
    scope_text = re.sub(COLLECTIVE_MATCHUP, " ", question, flags=re.I)
    if _OPPONENT_FILTER.search(scope_text):
        raise SemanticError(
            "unsupported_scope",
            "Opponent splits cover all opponents in the sample. Ask for a separate single-opponent summary.",
        )
    if _UNCONSUMED_GAME_WINDOW.search(_LAST_GAMES.sub(" ", question)):
        raise SemanticError(
            "unsupported_scope",
            "Use a numeric last-N-games window, such as last 3 games.",
        )
    if _WITHIN_GAME_SCOPE.search(question):
        raise SemanticError(
            "unsupported_scope", "Opponent splits are available for whole games only."
        )
    round_key = requested_playoff_round(question)
    if message := round_scope_message(question, round_coverage=True):
        raise SemanticError("unsupported_scope", message)
    phase = "Playoffs" if round_key else explicit_phase(question)
    if phase == "Both":
        raise SemanticError(
            "clarification_required",
            "Choose either the regular season or the playoffs.",
        )
    if _UNSUPPORTED_SCOPE.search(question):
        raise SemanticError(
            "unsupported_scope",
            "Choose one season or playoff round, an explicit date range, or a last-N-games window.",
        )
    scope = explicit_scope(scope_text)
    dates = re.findall(r"\b\d{4}-\d{2}-\d{2}\b", question)
    if len(dates) > 2 or (
        dates and not (scope.get("as_of") or re.search(r"\bas[ -]of\b", question, re.I))
    ):
        raise SemanticError(
            "clarification_required", "Use one explicit date range or an as-of date."
        )
    named = re.search(r"\b20\d{2}[-–/]\d{2,4}\b(?![-/–]\d)", question)
    if playoff_year := re.search(
        r"\b(20\d{2})\s+(?:playoffs?|postseason)\b", question, re.I
    ):
        year = int(playoff_year[1])
        question += f" {year - 1}-{year % 100:02}"
        named = playoff_year
    seasons = requested_seasons(question, season)
    if scope.get("window") == "date_range":
        dated_seasons = seasons_in_dates(scope["start_date"], scope["as_of"])
        if len(dated_seasons) != 1 or (named and seasons != dated_seasons):
            raise SemanticError(
                "clarification_required", "Choose dates within one matching NBA season."
            )
        seasons = dated_seasons
    if len(seasons) != 1:
        raise SemanticError(
            "clarification_required", "Choose one NBA season for these splits."
        )
    if last_games := _LAST_GAMES.findall(question):
        if len(set(last_games)) != 1 or scope.get("window"):
            raise SemanticError(
                "clarification_required", "Choose one window for these splits."
            )
        scope.update(window="last_n_games", n=int(last_games[0]))
    if as_of := re.search(r"\bas[ -]of\s+(\d{4}-\d{2}-\d{2})\b", question, re.I):
        if scope.get("as_of", as_of[1]) != as_of[1]:
            raise SemanticError(
                "clarification_required", "Choose one end date for these splits."
            )
        scope["as_of"] = as_of[1]
    query = Query(
        metric="pts",
        aggregation="average",
        season=seasons[0],
        season_type=phase or load_contract().default_phase,
        playoff_round=round_key,
        **scope,
    )
    query.validate(load_contract().metric(query.metric))
    return query


@dataclass(frozen=True)
class OpponentSplitsRequest:
    query: Query
    player: dict[str, Any]
    metrics: tuple[str, ...]
    aggregation: str = "average"


def parse_opponent_splits(
    question: str,
    players: list[dict[str, Any]],
    season: str,
    *,
    query: Query | None = None,
) -> OpponentSplitsRequest:
    if re.search(r"\bplayer\d+\b", question, re.I):
        raise SemanticError("invalid_scope", "Use a player name.")
    base, _, metric_reply = question.partition(_METRIC_REPLY)
    base = base.split("\nSelected player:", 1)[0]
    masked, named = mask_player_tokens(base, players)
    if len(named) != 1:
        raise SemanticError(
            "clarification_required", "Please name one player for the opponent splits."
        )
    player = next(iter(named.values()))
    query = query or opponent_split_query(base, season)
    filters = extract_game_filters(base, [player]).query_fields
    # Uppercase stat shorthand can resemble a team code. MIN is a team code.
    team = filters.get("team_abbr", "")
    if team != "MIN" and explicit_metric_mentions(team):
        filters.pop("team_abbr")
    if set(filters) - {"home_away"}:
        raise SemanticError(
            "unsupported_scope",
            "Opponent splits support a single home or away filter.",
        )
    keys = explicit_metric_mentions(metric_reply or masked)
    keys.update(extract_metrics(metric_reply or masked)[1])
    contract = load_contract()
    metrics = (
        tuple(key for key in contract.metrics if key in keys)
        if keys
        else DEFAULT_METRICS
    )
    return OpponentSplitsRequest(
        replace(
            query,
            player_id=player["player_id"],
            home_away=filters.get("home_away", query.home_away),
        ),
        {key: player[key] for key in ("player_id", "player_name")},
        metrics,
        "total"
        if explicit_aggregation(metric_reply or masked) == "total"
        else "average",
    )


def _summary(rows: Sequence[Mapping[str, Any]], metric: Metric) -> dict[str, Any]:
    aggregation = (
        "ratio" if metric.denominator else "total" if metric.key == "gp" else "average"
    )
    summary = aggregate(rows, metric, aggregation)
    if not metric.denominator:
        summary["total"] = summary["numerator"]
    return summary


def _record(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    results = [_game_result(row) for row in rows]
    wins, losses = results.count("W"), results.count("L")
    return {
        "games": len(rows),
        "wins": wins,
        "losses": losses,
        "unknown_outcomes": len(rows) - wins - losses,
    }


def compute_opponent_splits(
    evidence: Evidence, request: OpponentSplitsRequest
) -> dict[str, Any]:
    query = request.query
    selection = select_query_rows(evidence, query)
    rows = [row for row in selection.rows if row["player_id"] == query.player_id]
    if not rows:
        raise SemanticError(
            "unsupported_coverage", "No verified player appearances in this scope."
        )
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        opponent = row.get("opponent_abbr")
        if not isinstance(opponent, str) or not re.fullmatch(r"[A-Z]{2,4}", opponent):
            raise SemanticError(
                "incomplete_evidence",
                "Opponent identity is missing from the selected games.",
            )
        groups[opponent].append(row)

    contract = load_contract()
    definitions = {
        key: contract.metric(key)
        for key in dict.fromkeys((*request.metrics, *SCORE_METRICS))
    }
    baseline = {key: _summary(rows, metric) for key, metric in definitions.items()}
    game_values = {
        key: {row["game_id"]: _summary([row], metric)["value"] for row in rows}
        for key, metric in definitions.items()
    }
    scales = {}
    for key in SCORE_METRICS:
        values = [value for value in game_values[key].values() if value is not None]
        if (
            len(values) >= 2
            and (spread := pstdev(values)) > 0
            and baseline[key]["value"] is not None
        ):
            scales[key] = spread
    opponents: list[dict[str, Any]] = []
    for opponent, games in groups.items():
        metrics = {}
        for key, metric in definitions.items():
            summary = _summary(games, metric)
            value, reference = summary["value"], baseline[key]["value"]
            summary["difference"] = (
                (value - reference) * (100 if metric.unit == "ratio" else 1)
                if value is not None and reference is not None
                else None
            )
            metrics[key] = summary
        components = {
            key: (baseline[key]["value"] - metrics[key]["value"]) / spread
            for key, spread in scales.items()
            if metrics[key]["value"] is not None
        }
        opponents.append(
            {
                "opponent_abbr": opponent,
                **_record(games),
                "metrics": metrics,
                "small_sample": len(games) < SMALL_SAMPLE,
                "sample_label": "Small sample" if len(games) < SMALL_SAMPLE else "—",
                "score_components": components,
                "struggle_score": mean(components.values())
                if scales and components.keys() == scales.keys()
                else None,
            }
        )
    ranked = [row for row in opponents if row["struggle_score"] is not None]
    opponents.sort(
        key=lambda row: (
            row["struggle_score"] is None,
            -(row["struggle_score"] or 0),
            -row["games"],
            row["opponent_abbr"],
        )
    )
    # Report extremes only where at least two opponents have distinct scores.
    best = (
        min(
            ranked,
            key=lambda row: (
                row["struggle_score"],
                -row["games"],
                row["opponent_abbr"],
            ),
        )
        if ranked
        else None
    )
    toughest = (
        opponents[0]
        if best
        and not isclose(
            opponents[0]["struggle_score"], best["struggle_score"], abs_tol=1e-9
        )
        else None
    )
    if toughest is None:
        best = None
    games = (
        [
            {
                "game_id": row["game_id"],
                "game_date": str(row["game_date"]),
                "team_abbr": row.get("team_abbr"),
                "opponent_abbr": row["opponent_abbr"],
                "game_result": _game_result(row)
                if _game_result(row) in ("W", "L")
                else None,
                "metrics": {
                    key: {"value": game_values[key][row["game_id"]]}
                    for key in request.metrics
                },
            }
            for row in groups[toughest["opponent_abbr"]]
            if toughest is not None
        ]
        if toughest
        else []
    )
    assumptions = [
        "Struggle score averages the deficits in points, true shooting and plus-minus relative to the player's in-scope baseline. Each deficit is divided by that metric's population standard deviation across the sampled games. Higher means a tougher observed matchup.",
        "Scores use the same available, varying metrics across opponents. Each opponent needs usable data for every active metric. Plus-minus reflects team scoring margin during the player's minutes.",
        "Shooting rates use summed components. Counting stats and shooting rates use games with their required data. Changes are from the player's in-scope per-game averages or pooled rates. Shooting changes are in percentage points.",
    ]
    if any(row["small_sample"] for row in opponents):
        assumptions.append(
            "Opponents with fewer than three appearances are marked as small samples. Their scores can be sensitive to a single game."
        )
    if any(row["unknown_outcomes"] for row in opponents):
        assumptions.append(
            "The W–L record counts verified results. Unknown outcomes are shown separately."
        )
    return {
        "assumptions": assumptions,
        "version": VERSION,
        "player": request.player,
        "scope": {
            "season": query.season,
            "season_type": query.season_type,
            "playoff_round": query.playoff_round,
            "home_away": query.home_away,
            "window": query.window,
            "n": query.n,
            "start_date": str(selection.start) if selection.start else None,
            "as_of": str(selection.end),
            "first_game_date": str(rows[-1]["game_date"]),
            "last_game_date": str(rows[0]["game_date"]),
            "source": evidence.source,
            "snapshot_id": evidence.snapshot_id,
            "data_through": evidence.data_through[selection.scopes[0]],
        },
        "metric_definitions": {
            key: {"key": key, "unit": metric.unit, "label": metric.label}
            for key, metric in definitions.items()
        },
        "display_metrics": list(request.metrics),
        "aggregation": request.aggregation,
        "baseline": {**_record(rows), "metrics": baseline},
        "score_scales": scales,
        "opponents": opponents,
        "toughest_opponent": toughest["opponent_abbr"] if toughest else None,
        "best_opponent": best["opponent_abbr"] if best else None,
        "toughest_opponent_games": games,
    }


def answer_opponent_splits(
    question: str,
    evidence: Evidence,
    players: list[dict[str, Any]],
    season: str,
    *,
    query: Query | None = None,
) -> dict[str, Any]:
    from app.agent.claim_renderers import opponent_splits_tables

    request = parse_opponent_splits(question, players, season, query=query)
    result = compute_opponent_splits(evidence, request)
    prior_question = question.partition(_METRIC_REPLY)[0].split(
        "\nSelected player:", 1
    )[0]
    if request.query.season not in prior_question:
        prior_question += f" ({request.query.season})"
    if not request.query.playoff_round and not explicit_phase(prior_question):
        prior_question += f" {request.query.season_type}"
    payload = answer_payload(
        "",
        status="ok",
        ask_route={"route": "opponent_splits", "source": "deterministic"},
        opponent_splits=result,
        semantic_plan={"status": "opponent_splits", "model_calls": 0},
        conversation_context={
            "players": [request.player],
            "scope": {"season": request.query.season},
            "opponent_splits_question": prior_question,
        },
    )
    opponent_splits_tables(payload)
    return payload
