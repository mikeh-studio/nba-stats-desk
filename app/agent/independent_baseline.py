"""Two explicitly bound player/league periods over existing governed rows.

A league appearance window means up to N matching recorded appearances per
player. It is intentionally distinct from the focal-calendar shared baseline.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from dataclasses import asdict, dataclass

from app.agent.availability_ask import METRICS
from app.agent.derived_series import explicit_period
from app.agent.semantics import (
    Query,
    SemanticError,
    aggregate,
    load_contract,
    run_query,
    select_query_rows,
)


@dataclass(frozen=True)
class IndependentBaselineSpec:
    player_query: Query
    league_query: Query


def _operands(text):
    text = " ".join(text.strip().rstrip("?.!").replace("’", "'").split())
    match = re.fullmatch(
        r"(?:compare )?(.+?) (?:against|versus|vs|with|to(?! \d{4}-\d{2}-\d{2})) (.+)",
        text,
        re.I,
    )
    if match:
        return match[1], match[2]
    return None


def _focal_operand(operand, player_id):
    prefix = re.fullmatch(rf"player{player_id}(?:'s)? (.+)", operand)
    if not prefix:
        return None
    for key, pattern in METRICS.items():
        match = re.fullmatch(
            rf"(?:{pattern})(?: per game)? (?P<period>(?:in|for|during|over|from) .+)",
            prefix[1],
        )
        if match:
            return key, match["period"]
    return None


def _league_operand(operand):
    match = re.fullmatch(
        r"(?:the )?league (?:average|baseline) (?P<period>(?:in|for|during|over|from) .+)",
        operand,
        re.I,
    )
    return match["period"] if match else None


def _period(phrase, season):
    phrase = re.sub(
        r"^((?:in|for|during|over) (?:the )?)(?:full|entire) ", r"\1", phrase
    )
    return explicit_period(phrase, season)


def _period_scopes(focal_period, league_period, selected_season):
    focal_scope = _period(focal_period, selected_season)
    league_scope = _period(league_period, selected_season)
    if focal_scope is None or league_scope is None:
        return None
    for phrase, scope, other in (
        (focal_period, focal_scope, league_scope),
        (league_period, league_scope, focal_scope),
    ):
        if scope.get("window") in {"last_n_games", "prior_n_games"}:
            if not re.search(r"\b20\d{2}-\d{2}\b", phrase):
                scope["season"] = other["season"]
            if not re.search(r"regular season|playoffs", phrase):
                scope["season_type"] = other["season_type"]
    return focal_scope, league_scope


def independent_baseline_seasons(
    question: str, selected_season: str
) -> list[str] | None:
    """Resolve complete literal date operands before loading any roster.

    This preflight does not authorize identity or execution. The source-bound
    parser repeats the exact period grammar after names and filters validate.
    Each date operand must fit one archive; an appearance operand can inherit
    that explicit archive instead of the app shell's selected season.
    """
    from app.agent.game_filters import extract_game_filters

    # Literal team abbreviations require their original case during predicate
    # extraction. Normalize only the remaining period text, after those clauses
    # have been removed by the same shared filter parser used at execution.
    operands = _operands(question)
    if not operands:
        return None
    focal_period = league_period = None
    for operand in operands:
        if period := _league_operand(operand):
            if league_period is not None:
                return None
            league_period = extract_game_filters(period).cleaned_question.casefold()
            continue
        for pattern in METRICS.values():
            match = re.fullmatch(
                rf".+? (?:{pattern})(?: per game)? (?P<period>(?:in|for|during|over|from) .+)",
                operand,
                re.I,
            )
            if match:
                if focal_period is not None:
                    return None
                focal_period = extract_game_filters(
                    match["period"]
                ).cleaned_question.casefold()
                break
        else:
            return None
    if focal_period is None or league_period is None:
        return None
    scopes = _period_scopes(focal_period, league_period, selected_season)
    if scopes is None or not any(
        scope.get("window") == "date_range" for scope in scopes
    ):
        return None
    return list(dict.fromkeys(scope["season"] for scope in scopes))


def parse_independent_baseline(
    text, player, selected_season, *, filters=None, original_shape=None
):
    """Recognize two entire operands without consuming unknown modifiers.

    A league-side bare season remains the established shared-scope grammar.
    ``full``/``entire`` or original recent-season wording makes its independent
    full-season meaning explicit. Original wording is used only for ownership,
    never to re-resolve a pinned season against a later clock.
    """
    operands = _operands(text)
    if not operands:
        return None
    focal, league, league_index = None, None, None
    for index, operand in enumerate(operands):
        if value := _focal_operand(operand, player["player_id"]):
            if focal is not None:
                return None
            focal = value
        elif (value := _league_operand(operand)) is not None:
            if league is not None:
                return None
            league, league_index = value, index
        else:
            return None
    if focal is None or league is None:
        return None
    metric_key, focal_period = focal
    scopes = _period_scopes(focal_period, league, selected_season)
    if scopes is None:
        return None
    focal_scope, league_scope = scopes
    # An unqualified appearance operand inherits the opposite operand's stated
    # season and phase, rather than the unrelated app shell's selected archive.
    if league_scope.get("window", "season_to_date") == "season_to_date":
        raw_operands = _operands(original_shape or "")
        raw_league = raw_operands[league_index] if raw_operands else league
        if not re.search(
            r"\b(?:full|entire)\b|\b(?:most )?recent (?:nba )?(?:regular season|season)\b",
            raw_league,
        ):
            return None
    metric = load_contract().metric(metric_key)
    aggregation = "ratio" if metric.denominator else "average"
    common = {
        "metric": metric.key,
        "aggregation": aggregation,
        "min_games": 1,
        **(filters or {}),
    }
    player_query = Query(**common, **focal_scope, player_id=player["player_id"])
    league_query = Query(**common, **league_scope)
    player_query.validate(metric)
    league_query.validate(metric)
    return IndependentBaselineSpec(player_query, league_query)


def period_label(scope):
    label = f"{scope['season']} {scope['season_type']}"
    if scope.get("window") == "last_n_games":
        return (
            label
            + f"; up to last {scope['n']} matching appearances"
            + (" per player" if scope.get("player_id") is None else "")
        )
    if scope.get("window") == "prior_n_games":
        return (
            label
            + f"; prior {scope['n']} matching appearances"
            + (" per player" if scope.get("player_id") is None else "")
        )
    if scope.get("window") == "date_range":
        return (
            label
            + f"; {scope['start_date']} through {scope.get('end_date') or scope['as_of']}"
        )
    return (
        label
        + f"; full recorded season through {scope.get('end_date') or scope['as_of']}"
    )


def build_independent_baseline(evidence, player, spec):
    if not evidence.cohort_complete:
        raise SemanticError(
            "unsupported_coverage",
            "Independent league periods require complete cohort coverage.",
        )
    current = run_query(evidence, spec.player_query)
    row = next(iter(current["rows"]), None)
    if row is None:
        raise SemanticError(
            "no_observations",
            "No player appearances match the requested player period.",
        )
    selection = select_query_rows(evidence, spec.league_query)
    peers = selection.rows
    metric = load_contract().metric(spec.player_query.metric)
    baseline = aggregate(peers, metric, spec.league_query.aggregation)
    baseline.pop("game_ids", None)
    keys = sorted((r["season"], r["game_id"], r["player_id"]) for r in peers)
    groups = defaultdict(list)
    for peer in peers:
        groups[peer["player_id"]].append(peer)
    windows = [
        {
            "player_id": pid,
            "observed_games": len(rows),
            "start_date": min(str(r["game_date"]) for r in rows),
            "end_date": max(str(r["game_date"]) for r in rows),
        }
        for pid, rows in sorted(groups.items())
    ]
    warnings = list(current["warnings"])
    if row["sample_warning"]:
        warnings.append(row["sample_warning"])
    partial_players = 0
    if spec.league_query.n and spec.league_query.window.endswith("games"):
        partial_players = sum(
            w["observed_games"] < spec.league_query.n for w in windows
        )
        if partial_players:
            warnings.append("league_partial_appearance_windows")
    if baseline["missing_component_games"]:
        warnings.append("league_incomplete_components")
    complete = all(
        v["value"] is not None and v["missing_component_games"] == 0
        for v in (row, baseline)
    )
    scale = 100 if metric.unit == "ratio" else 1
    baseline_scope = {
        **asdict(spec.league_query),
        "as_of": selection.anchor.isoformat(),
        "start_date": selection.start.isoformat() if selection.start else None,
        "end_date": selection.end.isoformat(),
        "selected_start_date": min((str(r["game_date"]) for r in peers), default=None),
        "selected_end_date": max((str(r["game_date"]) for r in peers), default=None),
        "weighting": "pooled_components" if metric.denominator else "player_appearance",
        "population": "all recorded player appearances, including the focal player",
        "minimum_appearances": 1,
        "player_count": len(groups),
        "appearance_window_definition": "up to N matching recorded appearances per player"
        if spec.league_query.window.endswith("games")
        else None,
        "partial_window_players": partial_players,
        "appearance_windows": windows,
    }
    through = {
        (
            phase if len(selection.seasons) == 1 else f"{season} {phase}"
        ): evidence.data_through[(season, phase)]
        for season, phase in selection.scopes
    }
    if any(selection.anchor.isoformat() > str(value) for value in through.values()):
        warnings.append("league_requested_as_of_exceeds_source_coverage")
    return {
        "kind": "league_baseline",
        "contract_version": "league_baseline/3",
        "independent_periods": True,
        "player_id": player["player_id"],
        "metric": metric.public(),
        "scope": current["scope"],
        "player": row,
        "baseline": baseline,
        "baseline_scope": baseline_scope,
        "difference": (row["value"] - baseline["value"]) * scale if complete else None,
        "difference_unit": "percentage points"
        if scale == 100
        else f"{metric.label} per game",
        "membership": {
            "count": len(keys),
            "sha256": hashlib.sha256(
                json.dumps(keys, separators=(",", ":")).encode()
            ).hexdigest(),
            "key_fields": ["season", "game_id", "player_id"],
            "encoding": "SHA-256 of UTF-8 compact JSON sorted key tuples",
        },
        "provenance": current["provenance"],
        "baseline_provenance": {
            "source": evidence.source,
            "snapshot_id": evidence.snapshot_id,
            "data_through": through,
            "model_built_at": evidence.model_built_at,
            "temporal_semantics": "retrospective_source_snapshot",
        },
        "warnings": list(dict.fromkeys(warnings)),
    }
