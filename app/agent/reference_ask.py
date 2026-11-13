"""Evidence-only similarity and league-reference answers. No narrative model calls."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import replace
from time import monotonic
from typing import Any

from app.agent.availability_ask import METRICS, _identities
from app.agent.execution import timed
from app.agent.game_filters import (
    COMPONENTS as FILTER_COMPONENTS,
)
from app.agent.game_filters import (
    extract_game_filters,
    filter_description,
    filter_scope,
)
from app.agent.independent_baseline import (
    IndependentBaselineSpec,
    build_independent_baseline,
    independent_baseline_seasons,
    parse_independent_baseline,
    period_label,
)
from app.agent.payload import answer_payload
from app.agent.performance_overview import identity_profile
from app.agent.scope_policy import screen_assists_message
from app.agent.semantic_serving import requested_seasons, source_players
from app.agent.semantics import (
    Query,
    SemanticError,
    aggregate,
    load_contract,
    run_query,
    select_query_rows,
)
from app.agent.time_defaults import resolve_recent_season

BASELINE = r"\b(?:league\s+(?:average|baseline)|against\s+(?:the\s+)?baseline)\b"
SIMILARITY = r"\b(?:similar(?:ity)?|resembles?|like|alike|nearest)\b"
BASELINE_RELATIVE_PERIOD = (
    r"\b(this|current|selected|last|previous|prior|these(?= playoffs\b)) "
    r"(regular[ -]season|postseason|playoffs?|season)(?:['’]s)?\b"
)


def recent_appearance_window(text: str):
    """Recognize a potential window without admitting its count or filters."""
    return re.search(
        r"\b(?:last|past)\s+(?:[a-z]+|\d+)\s+"
        r"(?:(?:regular[ -]season|playoff)\s+)?"
        r"(?:(?:home|away|road)\s+)?(?:games?|appearances?)\b",
        text,
        re.I,
    )


def recent_venue_mentions(text: str) -> tuple[bool, bool]:
    """Recognize rejected venue windows after masking player identities."""
    if not recent_appearance_window(text):
        return False, False
    # The caller masks identities before classifying venue, including venues
    # written before the window ("on the road over the last five games").
    scope = text
    return (
        bool(re.search(r"\bat home\b|\bhome (?:games?|appearances?)\b", scope, re.I)),
        bool(
            re.search(
                r"\bon the road\b|\b(?:away|road) (?:games?|appearances?)\b|"
                r"\b(?:games?|appearances?) away\b",
                scope,
                re.I,
            )
        ),
    )


def baseline_outcome_message(text: str) -> str | None:
    wins = re.search(
        r"\bin (?:wins|victories)\b|\b(?:wins|victories)[ -]only\b|"
        r"\bonly (?:in )?(?:wins|victories)\b|"
        r"\bgames (?:that )?(?:(?:(?:his|her|their|the) team|they|he|she) )?won\b(?!['’])",
        text,
        re.I,
    )
    losses = re.search(
        r"\b(?:losses|defeats)\b|\bgames (?:that )?"
        r"(?:(?:(?:his|her|their|the) team|they|he|she) )?lost\b",
        text,
        re.I,
    )
    if losses and (wins or re.search(r"\b(?:wins|victories)\b", text, re.I)):
        outcomes = r"(?:wins|victories|losses|defeats)"
        contrast = re.search(
            rf"\b{outcomes}\s+(?:vs\.?|versus|against|compared (?:with|to))\s+{outcomes}\b",
            text,
            re.I,
        )
        return (
            (
                "This league baseline cannot compare separate wins and losses baselines. "
                if contrast
                else "Your request mentions both wins and losses. Please specify whether you want one unfiltered baseline or separate outcome baselines. "
            )
            + "Outcome-specific league baselines are not supported; an unfiltered baseline includes all results in the requested period."
        )
    if wins:
        return (
            "This league baseline cannot filter player or league appearances to wins only. "
            "It supports all results within the same season, phase and date window; ask for that scope explicitly."
        )
    return None


def reference_kind(question: str) -> str | None:
    if re.search(BASELINE, question, re.I):
        return "league_baseline"
    if re.search(
        r"\b(?:similarity|resembles?|alike|nearest)\b|\bplayers? (?:like|similar)\b|\bsimilar (?:to|players?)\b|\b(?:who|which)\b.*\bsimilar\b",
        question,
        re.I,
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
    eligible = reference_kind(previous) == "league_baseline" or (
        reference_kind(previous) == "similarity"
        and re.search(
            r"\b(?:last|past)\s+(?:five|\d+)\s+(?:(?:regular[ -]season|playoff)\s+)?games?\b",
            previous,
            re.I,
        )
    )
    if eligible and any(
        re.fullmatch(pattern, metric, re.I) for pattern in METRICS.values()
    ):
        # Replace only the requested measure, retaining its grammatical slot.
        # Names and numeric predicate components are independent scope fields.
        protected: list[tuple[int, int]] = []
        names = [p["player_name"] for p in context.get("players", [])]
        names.extend(context.get("reference_identity_names", []))
        for name in names:
            protected.extend(
                match.span()
                for match in re.finditer(
                    r"(?<!\w)" + re.escape(name) + r"(?!\w)", previous, re.I
                )
            )
        components = "|".join(
            re.escape(name) for name in sorted(FILTER_COMPONENTS, key=len, reverse=True)
        )
        protected.extend(
            match.span()
            for match in re.finditer(
                rf"(?:\b(?:at least|more than|over|at most|no more than|fewer than|less than|under|exactly)\s+|[<>]=?\s*|=\s*)-?\d+(?:\.\d+)?\s+(?:{components})(?!\w)",
                previous,
                re.I,
            )
        )
        spans = {
            match.span()
            for pattern in METRICS.values()
            for match in re.finditer(
                r"(?<!\w)(?:" + pattern + r")(?!\w)", previous, re.I
            )
            if not any(
                start <= match.start() and match.end() <= end
                for start, end in protected
            )
        }
        ordered_spans = sorted(
            (start, end)
            for start, end in spans
            if not any(
                a <= start and end <= b and (a < start or end < b) for a, b in spans
            )
        )
        if not ordered_spans:
            return (
                previous + " " + metric
                if reference_kind(previous) == "similarity"
                else None
            )
        for start, end in reversed(ordered_spans):
            previous = previous[:start] + metric + previous[end:]
        return previous
    return None


class BaselinePeriodClarification(SemanticError):
    """A period question requiring restatement, not an identity selection."""

    def __init__(self):
        super().__init__(
            "clarification_required",
            "Which season and phase do you mean by the recent season or playoffs? "
            "Restate the full comparison with an explicit season and phase; "
            "'recent' alone means the last ten recorded appearances.",
        )


def resolve_baseline_relative_seasons(text: str, selected_season: str) -> str:
    """Reconcile every season anchor before any identifier can be consumed."""
    alias = BASELINE_RELATIVE_PERIOD
    matches = list(re.finditer(alias, text))
    if not matches:
        return text
    # This is also the host's warehouse-load scope. An alias that the host
    # cannot resolve to the same season must fail closed, rather than removing
    # its words and accidentally querying the selected season.
    seasons = set(requested_seasons(text, selected_season))
    year = int(selected_season[:4]) - 1
    previous = f"{year}-{str(year + 1)[-2:]}"

    def resolve(match):
        return (
            selected_season
            if match[1] in {"this", "current", "selected", "these"}
            else previous
        )

    seasons.update(resolve(match) for match in matches)
    if len(seasons) != 1:
        raise SemanticError(
            "unsupported_scope",
            "The requested season references do not identify one shared season. "
            "This league baseline cannot compare different seasons; specify the same season for the player and league.",
        )
    return re.sub(
        alias,
        lambda match: resolve(match) + (" " + match[2] if match[2] != "season" else ""),
        text,
    )


def validate_baseline_period_shape(text: str) -> None:
    """Keep temporal operations visible before the scalar grammar removes scope."""
    # A calendar-resolved recent season still denotes a full season operand.
    # Keep that distinction from an incidental year qualifying a shared window.
    text = re.sub(
        r"\b(?:most\s+)?recent\s+(?:nba\s+)?(?:regular[ -]season|season)\b",
        "full season",
        text,
    )
    if re.search(
        r"\brecent\s+(?:20\d{2}[-–/](?:20)?\d{2}\s+)?"
        r"(?:regular[ -]season|season|playoffs?|postseason)\b",
        text,
    ):
        raise BaselinePeriodClarification()
    if re.search(r"\btrend(?:s|ing)?\b", text):
        raise SemanticError(
            "unsupported_scope",
            "This league baseline returns one summary for a shared period. "
            "It cannot show a trend or changes across periods; ask for one period explicitly.",
        )
    # A range's 'to' is a date delimiter, not a comparison link. Mask it only
    # for shape validation; the actual parser still validates the full range.
    shape = re.sub(
        r"\bfrom \d{4}-\d{2}-\d{2} (?:through|to) \d{4}-\d{2}-\d{2}\b",
        " DATE_RANGE ",
        text,
    )
    baseline = r"\b(?:league\s+(?:average|baseline)|baseline)\b"
    # A bounded leading season clause qualifies the whole comparison, rather
    # than attaching a season only to the first side of its binary link.
    year = r"20\d{2}[-–/](?:20)?\d{2}(?![-/]\d)"
    phase = r"(?:regular[ -]season|playoffs?|postseason|season)"
    season = rf"(?:{year}(?: {phase})?|{phase} (?:in |for )?{year}|{BASELINE_RELATIVE_PERIOD}|full season)"
    shape = re.sub(
        rf"^(?:in|for|during|over) (?:the )?{season}(?:\s*,\s*|\s+)",
        "",
        shape,
    )
    # A leading comparison complement is a league operand. Its comma bounds
    # the operand, including metric/date modifiers; without a comma, the first
    # source-backed player token bounds the following focal clause.
    prefix = re.match(r"^(?:compared (?:to|with)|against|versus|vs)\s+", shape)
    preposed = False
    if prefix:
        boundary = shape.find(",", prefix.end())
        player = re.search(r"\bplayer\d+\b", shape)
        if player and (boundary < 0 or player.start() < boundary):
            boundary = player.start()
        if boundary >= 0 and re.search(baseline, shape[prefix.end() : boundary]):
            preposed = True
            league_operand = shape[prefix.end() : boundary]
            shape = shape[boundary:].lstrip(", ") + " against " + league_operand
    links = list(re.finditer(r"\b(?:versus|vs|against|with|to)\b", shape))
    # Only the player-versus-league relationship is representable here. A
    # second comparison, or one connecting periods rather than the baseline,
    # must not collapse into one last-N/date/season statistic.
    unsupported = len(links) > 1
    sides = None
    if len(links) == 1:
        link = links[0]
        sides = shape[: link.start()], shape[link.end() :]
    elif not links and shape.count(",") == 1:
        left, _, right = shape.partition(",")
        sides = left, right
    window = r"\blast \d+ games?\b|\brecent\b|\bDATE_RANGE\b"
    season_scope = r"\b(?:20\d{2}[-–/](?:20)?\d{2}|season|playoffs?|postseason)\b"
    if sides:
        left, right = sides
        if re.search(baseline, left) and re.search(r"\bplayer\d+\b", right):
            focal, league = right, left
        elif re.search(r"\bplayer\d+\b", left) and re.search(baseline, right):
            focal, league = left, right
        else:
            unsupported = True
            focal = league = ""
        focal_window = re.search(window, focal)
        league_window = re.search(window, league)
        # The existing grammar accepts a focal appearance/date window qualified
        # by a trailing league-side season. The reverse assigns a separate
        # window to the league and cannot define one shared focal calendar.
        if league_window and (focal_window or re.search(season_scope, focal)):
            unsupported = True
        if focal_window:
            league_name = re.search(baseline, league)
            if (
                re.search(BASELINE_RELATIVE_PERIOD, league)
                or re.search(
                    r"\b(?:full|entire|whole) season\b|\bseason[ -]to[ -]date\b", league
                )
                or (preposed and re.search(season_scope, league))
                or (
                    league_name
                    and re.search(season_scope, league[: league_name.start()])
                )
            ):
                unsupported = True
    elif re.search(window, shape) and re.search(season_scope, shape):
        # Scope-bearing operands without a reliable boundary cannot authorize
        # a single shared window merely because all words can be consumed.
        unsupported = True
    if unsupported:
        raise SemanticError(
            "unsupported_scope",
            "This league baseline supports a player-versus-league summary for one shared period. "
            "It cannot compare the season with a recent window or contrast other periods; ask for one period explicitly.",
        )


@timed("identity")
def parse_reference(
    question,
    players,
    season,
    kind,
    *,
    enforce_profile_season=False,
    period_question=None,
):
    filters = {}
    if kind == "league_baseline":
        tracking_text, _ = _identities(question, players)
        if message := screen_assists_message(tracking_text):
            raise SemanticError("unsupported_scope", message)
        extracted = extract_game_filters(question, players)
        question, filters = extracted.cleaned_question, extracted.query_fields
        if period_question:
            period_question = extract_game_filters(
                period_question, players
            ).cleaned_question
    resolved_question, time_notes = resolve_recent_season(question)
    if time_notes:
        period_question = period_question or question
        question = resolved_question
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
    if message := screen_assists_message(text):
        raise SemanticError("unsupported_scope", message)
    if kind == "league_baseline":
        original_shape = (
            _identities(period_question, players)[0] if period_question else text
        )
        independent = parse_independent_baseline(
            text, player, season, filters=filters, original_shape=original_shape
        )
        if independent is not None:
            return player, independent.player_query.season, independent
    if len(seasons) != 1:
        raise SemanticError("unsupported_scope", "Use one season for this comparison.")
    if (
        enforce_profile_season
        and kind == "similarity"
        and seasons != [season]
        and not recent_appearance_window(text)
    ):
        raise SemanticError(
            "unsupported_coverage",
            "The similarity publication must match the selected season.",
        )
    if kind == "league_baseline":
        resolved_text = resolve_baseline_relative_seasons(text, season)
        period_text = (
            _identities(period_question, players)[0] if period_question else text
        )
        validate_baseline_period_shape(period_text)
        text = resolved_text
        # Consume temporal connectors only next to a period this handler parses.
        # A dangling connector must still require a complete scope request.
        text = re.sub(
            r"\b(?:during|over)\s+(?:the\s+)?"
            r"(?=(?:20\d{2}[-–/](?:20)?\d{2}(?![-/]\d)|regular[ -]season|"
            r"playoffs?|postseason|last \d+ games?|recent)\b)",
            " ",
            text,
        )
    text = re.sub(r"player\d+|(?:20\d{2})[-–/](?:20)?\d{2}(?![-/]\d)", " ", text)
    text = re.sub(r"[’']s\b", " ", text)
    if kind == "similarity":
        home, road = recent_venue_mentions(text)
        if home or road:
            venue = (
                "home or road games"
                if home and road
                else "home games"
                if home
                else "road games"
            )
            raise SemanticError(
                "unsupported_scope",
                f"Recent statistical similarity cannot filter appearances to {venue}. "
                "It supports each player's last-N appearances across all venues; ask for that scope explicitly.",
            )
        games = re.search(
            r"\b(?:last|past)\s+(five|\d+)\s+(?:(?:regular[ -]season|playoff)\s+)?games?\b",
            text,
        )
        recent = None
        if games:
            n = 5 if games[1] == "five" else int(games[1])
            if not 2 <= n <= 20:
                raise SemanticError(
                    "unsupported_scope",
                    "Recent statistical comparisons support 2 to 20 recorded appearances.",
                )
            text = text[: games.start()] + " " + text[games.end() :]
            metrics = []
            for key, pattern in METRICS.items():
                text, found = re.subn(
                    r"(?<!\w)(?:" + pattern + r")(?!\w)", " ", text, flags=re.I
                )
                if found:
                    metrics.append(key)
            if any(m not in {"pts", "reb", "ast"} for m in metrics):
                raise SemanticError(
                    "clarification_required",
                    "Recent statistical similarity currently supports points, rebounds and assists. Which of those metrics should I compare?",
                )
            phase = (
                "Playoffs"
                if re.search(r"\b(?:playoffs?|postseason)\b", question, re.I)
                else "Regular Season"
            )
            if phase == "Playoffs" and re.search(r"regular[ -]season", question, re.I):
                raise SemanticError(
                    "unsupported_scope", "Choose one phase for the comparison."
                )
            text = re.sub(
                r"regular[ -]season|\b(?:playoffs?|postseason|over|per game|and|using|based on|stats|statistical|profile)\b",
                " ",
                text,
            )
            recent = {
                "n": n,
                "metrics": metrics or ["pts", "reb", "ast"],
                "metrics_defaulted": not metrics,
                "season_type": phase,
            }
        count = re.search(r"\b(?:top|show|find)\s+(\d+)\b", text)
        limit = int(count[1]) if count else 5
        if not 1 <= limit <= 6:
            raise SemanticError(
                "unsupported_scope",
                "Statistical Profile Similarity supports one to six neighbors.",
            )
        if count:
            text = text[: count.start()] + " " + text[count.end() :]
        text = re.sub(SIMILARITY, " ", text)
        text = re.sub(
            r"\b(?:who|which|are|is|the|most|players?|comps?|to|show|find|me|for|in|season|this|statistical|profile)\b",
            " ",
            text,
        )
        if text.strip(" ?.!,"):
            raise SemanticError(
                "unsupported_scope",
                "Statistical Profile Similarity supports a published season profile or a last-N appearance comparison using points, rebounds and assists. Other metrics, periods and filters require clarification.",
            )
        return player, seasons[0], dict(recent, limit=limit) if recent else limit

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
        r"\b(?:compare|compared|show|tell|me|how|did|does|perform|performed|against|versus|vs|with|to|the|for|in|season|this|per game|average|averaging|his)\b",
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
        **filters,
        **scope,
    )
    query.validate(metric)
    return player, seasons[0], query


@timed("calculation")
def build_baseline(evidence, player, query):
    """Appearance-weighted league baseline across the player's calendar window.

    No top-N truncation: all appearances in the shared interval are pooled.
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
    end = (
        max(r["game_date"] for r in player_games)
        if query.window == "last_n_games"
        else current["scope"]["as_of"]
    )
    if query.window == "last_n_games":
        current["scope"].update(window_start=start, window_end=end)
    peer_query = replace(
        query,
        player_id=None,
        operation="summary",
        window="date_range",
        start_date=start or f"{query.season[:4]}-07-01",
        as_of=end,
        n=None,
    )
    peers = select_query_rows(evidence, peer_query).rows
    metric = load_contract().metric(query.metric)
    baseline = aggregate(peers, metric, query.aggregation)
    baseline.pop("game_ids", None)  # Full peer membership belongs in source artifacts.
    membership_keys = sorted((r["season"], r["game_id"], r["player_id"]) for r in peers)
    membership = {
        "count": len(membership_keys),
        "sha256": hashlib.sha256(
            json.dumps(membership_keys, separators=(",", ":")).encode()
        ).hexdigest(),
        "key_fields": ["season", "game_id", "player_id"],
        "encoding": "SHA-256 of UTF-8 compact JSON sorted key tuples",
    }
    complete = all(
        v["value"] is not None and v["missing_component_games"] == 0
        for v in (row, baseline)
    )
    scale = 100 if metric.unit == "ratio" else 1
    difference = (row["value"] - baseline["value"]) * scale if complete else None
    return {
        "kind": "league_baseline",
        "contract_version": "league_baseline/2",
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
            **filter_scope(current["scope"]),
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
        "membership": membership,
        "provenance": current["provenance"],
        "warnings": current["warnings"]
        + ([row["sample_warning"]] if row["sample_warning"] else []),
    }


@timed("calculation")
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


@timed("rendering")
def render_reference(evidence, player):
    payload = answer_payload(
        "",
        status="ok",
        semantic_evidence=evidence,
        player_profile=identity_profile(player, []),
    )
    if evidence["kind"] == "similarity":
        payload["answer"] = (
            f"Statistical Profile Similarity: {evidence['rows'][0]['player_name']} is the closest published match to {player['player_name']} in {evidence['scope']['season']} (profile similarity score {evidence['rows'][0]['similarity_score']:.4f}). This compares measured statistical and available physical features, not equivalent playing style, player quality, or a probability. See [how the comparison works](/similarity-methodology)."
        )
        payload["tables"] = [
            {
                "title": "Statistical Profile Similarity — published season profile",
                "columns": [
                    {"key": "player", "label": "Player"},
                    {"key": "score", "label": "Profile similarity score (0–1)"},
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
        if evidence.get("independent_periods"):
            focal_period = period_label(evidence["scope"])
            league_period = period_label(scope)
            payload["answer"] = (
                f"{player['player_name']} ({focal_period}): {fmt(a['display_value'])} {metric['label']}; "
                f"league baseline ({league_period}): {fmt(b['display_value'])}. "
                + (
                    "The difference is unavailable because a comparison sample is incomplete."
                    if delta is None
                    else f"Player minus league: {delta:+.1f} {evidence['difference_unit']}."
                )
            )
            table = payload["tables"][0]
            table["title"] += " — independent periods"
            table["columns"].append({"key": "period", "label": "Selected period"})
            table["rows"][0].append(focal_period)
            table["rows"][1].append(league_period)
            payload["assumptions"] = [
                f"Player period: {focal_period}.",
                f"League period: {league_period}.",
                "These are two independently selected periods; the league window is not the focal player's calendar interval. This compares descriptive averages, not a trend or statistical significance.",
                "League baseline: all matching recorded appearances, including this player, with at least one appearance per player. Count averages are appearance-weighted; shooting ratios pool complete components, never average game percentages.",
                "Missing inputs remain unknown; partial averages use only complete observations and suppress the difference and chart. Zero denominators are unavailable.",
                f"Player source: {evidence['provenance']['source']}; snapshot {evidence['provenance']['snapshot_id']}; data through {evidence['provenance']['data_through']}.",
                f"League source: {evidence['baseline_provenance']['source']}; snapshot {evidence['baseline_provenance']['snapshot_id']}; data through {evidence['baseline_provenance']['data_through']}.",
                *evidence["warnings"],
            ]
            if scope.get("appearance_window_definition"):
                payload["assumptions"].append(
                    f"League window: up to {scope['n']} matching recorded appearances per player, selected independently before pooling. "
                    f"{scope['player_count']} players contributed {b['observed_games']} appearances; "
                    f"{scope['partial_window_players']} players had fewer than {scope['n']}. Their actual appearances are retained, without invented games. "
                    f"Observed league dates: {scope['selected_start_date']} through {scope['selected_end_date']}."
                )
    from app.agent.claim_renderers import reference_tables

    reference_tables(payload, player)
    return payload


def answer_reference(
    agent,
    question,
    kind,
    conversation_id=None,
    trace=None,
    selected_player=None,
    *,
    route_context,
):
    started = monotonic()
    original = question
    store = agent.conversation_store if conversation_id else None
    context = route_context.context
    inherited_reference = route_context.reference_followup_question is not None
    question = route_context.reference_question
    period_clarification = False
    try:
        seasons = (
            independent_baseline_seasons(question, agent.settings.season)
            if kind == "league_baseline"
            else None
        ) or requested_seasons(question, agent.settings.season)
        if agent.semantic_agent is None:
            raise SemanticError(
                "unsupported_coverage",
                "A governed statistics source is required for this comparison.",
            )
        if (
            kind == "similarity"
            and seasons != [agent.settings.season]
            and not recent_appearance_window(question)
        ):
            # Fail fast for an obvious published-profile request. A raw-name
            # window match only defers this decision: parse_reference repeats
            # the authoritative check after masking source-backed identities.
            raise SemanticError(
                "unsupported_coverage",
                "The similarity publication must match the selected season.",
            )
        warehouse = agent.semantic_agent.warehouse
        # These reference/similarity routes are not part of the rich serving gate.
        _, source = getattr(warehouse, "facts", warehouse).load(seasons)
        if not source.cohort_complete:
            raise SemanticError(
                "unsupported_coverage",
                "League and similarity comparisons require complete cohort coverage.",
            )
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
            question,
            players,
            agent.settings.season,
            kind,
            enforce_profile_season=True,
            period_question=route_context.period_question,
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
        if isinstance(spec, IndependentBaselineSpec):
            evidence = build_independent_baseline(source, player, spec)
            payload = render_reference(evidence, player)
        elif kind == "similarity" and isinstance(spec, dict):
            from app.agent.recent_similarity import (
                build_recent_similarity,
                render_recent_similarity,
            )

            evidence = build_recent_similarity(source, player, season, spec)
            payload = render_recent_similarity(evidence, player)
        else:
            evidence = (
                build_similarity(agent.repo, player, season, spec)
                if kind == "similarity"
                else build_baseline(source, player, spec)
            )
            payload = render_reference(evidence, player)
        if note := filter_description(evidence.get("scope", {})):
            payload.setdefault("assumptions", []).append(note)
        if kind == "league_baseline" and evidence["difference"] is not None:
            metric = evidence["metric"]
            unit = "%" if metric["unit"] == "ratio" else "per game"
            payload["charts"] = [
                {
                    "type": "bar",
                    "title": f"{metric['label']} — player versus league"
                    + (
                        " — independent periods"
                        if evidence.get("independent_periods")
                        else ""
                    ),
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
                                    "meta": f"{group['valid_games']} complete player appearances"
                                    + (
                                        "; "
                                        + period_label(
                                            evidence["scope"]
                                            if label == player["player_name"]
                                            else evidence["baseline_scope"]
                                        )
                                        if evidence.get("independent_periods")
                                        else ""
                                    ),
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
            "reference_identity_names": [
                player["player_name"],
                *player.get("aliases", []),
            ],
            "scope": evidence["scope"],
            **(
                {"baseline_scope": evidence["baseline_scope"]}
                if evidence.get("independent_periods")
                else {}
            ),
            "metrics": [evidence["metric"]["key"]]
            if kind == "league_baseline"
            else evidence.get("metrics", []),
        }
    except SemanticError as exc:
        payload = answer_payload(str(exc), status=exc.code)
        period_clarification = isinstance(exc, BaselinePeriodClarification)
        if (
            exc.code == "clarification_required"
            and not period_clarification
            and "players" in locals()
        ):
            from app.agent.performance_overview import resolve_overview_player

            options = resolve_overview_player(question, players, source.rows)
            payload["clarification_options"] = [
                dict(p, label=p["player_name"]) for p in options
            ]
    payload["visualization"] = {
        "selection": "rule",
        "reason": "Reference rendering owns its evidence-derived charts; no model selection is needed.",
    }
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
        if payload["status"] == "clarification_required" and not period_clarification:
            store.set_pending_clarification(
                conversation_id,
                question=question,
                query_plan={"kind": kind},
                route=kind,
                candidates=payload.get("clarification_options"),
            )
        else:
            store.clear_pending_clarification(conversation_id)
        if payload["status"] == "ok":
            store.append_turn(
                conversation_id,
                question=original,
                answer=payload["answer"],
                max_turns=agent.settings.agent_conversation_max_turns,
                context=dict(payload["conversation_context"], route=kind),
            )
    return payload
