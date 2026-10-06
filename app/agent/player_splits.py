"""Bounded descriptive player comparisons with explicit group membership."""

from __future__ import annotations

import re
from dataclasses import asdict
from datetime import date, timedelta

from app.agent.availability_ask import METRICS, _identities
from app.agent.metric_presentation import (
    GAME_COVERAGE_NOTE,
    game_coverage,
    game_coverage_label,
)
from app.agent.performance_overview import identity_profile
from app.agent.question_intent import split_kind
from app.agent.semantic_serving import requested_seasons
from app.agent.semantics import Query, SemanticError, compare_queries, load_contract
from app.seasons import SEASONS, season_bounds

DEFAULT_METRICS = ("pts", "reb", "ast", "min", "ts_pct")


def split_seasons(question, season):
    year = re.search(r"\b(20\d{2})\s+(?:playoffs?|postseason)\b", question, re.I)
    if year:
        number = int(year[1])
        question += f" {number - 1}-{str(number)[-2:]}"
    explicit = requested_seasons(question, season)
    if not re.search(r"\b20\d{2}[-–/](?:20)?\d{2}\b(?![-/]\d)", question):
        dates = re.findall(r"\b\d{4}-\d{2}-\d{2}\b", question)
        if dates:
            try:
                days = [date.fromisoformat(value) for value in dates]
            except ValueError as exc:
                raise SemanticError("invalid_scope", "Supply valid ISO dates.") from exc
            matches = [
                s
                for s in SEASONS
                if all(
                    season_bounds(s)[0] <= day <= season_bounds(s)[1] for day in days
                )
            ]
            if len(matches) != 1:
                raise SemanticError(
                    "unsupported_coverage",
                    "Split dates must fit one available season (2023–24 through 2025–26).",
                )
            return matches
    return explicit


def split_followup(question, previous):
    """Only a metric-only continuation inherits a split; new scope is explicit."""
    if re.fullmatch(r"(?:what about|compare|show)\s+(.+?)\??", question.strip(), re.I):
        remainder = re.sub(
            r"^(?:what about|compare|show)\s+", "", question.strip(), flags=re.I
        ).rstrip("?")
        for pattern in METRICS.values():
            if re.fullmatch(pattern, remainder, re.I):
                for old in METRICS.values():
                    previous = re.sub(
                        r"(?<!\w)(?:" + old + r")(?!\w)", " ", previous, flags=re.I
                    )
                return previous.rstrip("?. ") + f". Compare {remainder}."
    return question


def parse_split(question, players, season):
    kind = split_kind(question)
    if not kind:
        raise SemanticError(
            "unsupported_scope",
            "Specify a phase, venue, recent/prior or before/after comparison.",
        )
    if re.search(r"\bplayer\d+\b", question, re.I):
        raise SemanticError(
            "invalid_scope", "Use a player name, not an internal token."
        )
    question = question.split("\nSelected player:", 1)[0]
    text, named = _identities(" ".join(question.split()), players)
    if len(named) != 1:
        raise SemanticError(
            "clarification_required",
            "Name one player for this split comparison, using a full name if ambiguous.",
        )
    player = next(iter(named.values()))
    text = re.sub(r"\bplayer\d+\b", " ", text)
    seasons = split_seasons(question, season)
    playoff_year = re.search(r"\b(20\d{2})\s+(?:playoffs?|postseason)\b", text)
    if playoff_year:
        year = int(playoff_year[1])
        resolved = requested_seasons(f"{year - 1}-{str(year)[-2:]}", season)
        if seasons != [season] and seasons != resolved:
            raise SemanticError("invalid_scope", "Conflicting season and playoff year.")
        seasons = resolved
        text = text[: playoff_year.start()] + " playoffs " + text[playoff_year.end() :]
    if len(seasons) != 1:
        raise SemanticError(
            "clarification_required", "Choose one season for this split comparison."
        )
    season = seasons[0]
    text = re.sub(
        r"\b20\d{2}[-–/](?:20)?\d{2}\b(?![-/]\d)|\b(?:this|last|previous|prior|current) season\b",
        " ",
        text,
    )
    start, end = season_bounds(season)
    end = min(end, date.today())
    dates = re.findall(
        r"\bfrom (\d{4}-\d{2}-\d{2}) (?:through|to) (\d{4}-\d{2}-\d{2})", text
    )
    if len(dates) > 1:
        raise SemanticError(
            "clarification_required", "Use one shared date range for this split."
        )
    try:
        if dates:
            start, end = map(date.fromisoformat, dates[0])
            text = re.sub(
                r"\bfrom \d{4}-\d{2}-\d{2} (?:through|to) \d{4}-\d{2}-\d{2}", " ", text
            )
        cutoff = re.search(r"\bas[ -]of (\d{4}-\d{2}-\d{2})", text)
        if cutoff:
            if dates and end != date.fromisoformat(cutoff[1]):
                raise SemanticError("invalid_scope", "Conflicting end dates.")
            end = date.fromisoformat(cutoff[1])
            text = text[: cutoff.start()] + " " + text[cutoff.end() :]
    except ValueError as exc:
        raise SemanticError("invalid_scope", "Supply valid ISO dates.") from exc
    bounds = season_bounds(season)
    if not bounds[0] <= start <= end <= min(bounds[1], date.today()):
        raise SemanticError(
            "unsupported_coverage",
            "Dates must be ordered and inside the selected season, ending no later than today.",
        )
    phase = (
        "Playoffs"
        if re.search(r"\b(?:playoffs?|postseason)\b", text)
        else "Regular Season"
    )
    if re.search(r"regular[ -]season", text) and re.search(
        r"\b(?:playoffs?|postseason)\b", text
    ):
        phase = "Both"
    base = dict(
        season=season,
        player_id=player["player_id"],
        season_type=phase,
        window="date_range",
        start_date=str(start),
        as_of=str(end),
    )
    groups = []
    if kind == "phase":
        groups = [
            ("Playoffs", dict(base, season_type="Playoffs")),
            ("Regular Season", dict(base, season_type="Regular Season")),
        ]
    elif kind == "venue":
        groups = [
            ("Away", dict(base, home_away="AWAY")),
            ("Home", dict(base, home_away="HOME")),
        ]
        text = re.sub(r"\b(?:home|away|road)\b", " ", text)
    elif kind == "recent_prior":
        last = re.search(r"\blast (\d+) games?\b", text)
        prior = re.search(r"\b(?:prior|previous|preceding) (\d+) games?\b", text)
        assert last and prior
        if last[1] != prior[1] or not 1 <= int(last[1]) <= 100 or dates:
            raise SemanticError(
                "clarification_required",
                "Use equal last-N and prior-N appearance windows (1–100) with an optional as-of date, not a separate date range.",
            )
        base.pop("start_date")
        groups = [
            (
                f"Last {last[1]} games",
                dict(base, window="last_n_games", n=int(last[1])),
            ),
            (
                f"Prior {prior[1]} games",
                dict(base, window="prior_n_games", n=int(prior[1])),
            ),
        ]
        text = re.sub(r"\b(?:last|prior|previous|preceding) \d+ games?\b", " ", text)
    else:
        event = re.search(
            r"\bbefore (?:and|vs\.?|versus) after (\d{4}-\d{2}-\d{2})\b", text
        )
        if not event:
            raise SemanticError(
                "clarification_required",
                "Give the event date as “before and after YYYY-MM-DD”, the season, and optional overall date range. I will not infer a trade or injury date.",
            )
        try:
            boundary = date.fromisoformat(event[1])
        except ValueError as exc:
            raise SemanticError("invalid_scope", "Supply a valid event date.") from exc
        if not start < boundary <= end:
            raise SemanticError(
                "invalid_scope",
                "Event date must leave a nonempty calendar interval on both sides.",
            )
        groups = [
            (f"On/after {boundary}", dict(base, start_date=str(boundary))),
            (f"Before {boundary}", dict(base, as_of=str(boundary - timedelta(days=1)))),
        ]
        text = text[: event.start()] + " " + text[event.end() :]
    text = re.sub(r"\bregular[ -]season\b|\b(?:playoffs?|postseason)\b", " ", text)
    # A shared opponent or venue filter composes with every split.
    opponent = re.search(r"\bagainst ([a-z]{2,3})\b", text)
    if opponent:
        for _, spec in groups:
            spec["opponent_abbr"] = opponent[1].upper()
        text = text[: opponent.start()] + " " + text[opponent.end() :]
    for pattern, value in [
        (r"\bat home\b", "HOME"),
        (r"\bon the road\b|\baway games\b", "AWAY"),
    ]:
        if re.search(pattern, text):
            if any("home_away" in spec for _, spec in groups):
                raise SemanticError("invalid_scope", "Conflicting venue filters.")
            for _, spec in groups:
                spec["home_away"] = value
            text = re.sub(pattern, " ", text)
    metrics = []
    for key, pattern in METRICS.items():
        pattern = r"(?<!\w)(?:" + pattern + r")(?!\w)"
        if re.search(pattern, text):
            metrics.append(key)
            text = re.sub(pattern, " ", text)
    aggregation = "total" if re.search(r"\btotals?\b", text) else "average"
    if aggregation == "total" and re.search(r"\baverages?\b|\bper game\b", text):
        raise SemanticError(
            "clarification_required", "Choose totals or per-game averages."
        )
    interpretive = bool(
        re.search(r"\b(?:why|explains?|sustainable|better|improve[ds]?)\b", text)
    )
    text = re.sub(
        r"\b(?:how|did|does|do|is|was|has|been|play|played|perform|performed|performance|stats|statistics|compare|compared|versus|vs|and|with|in|the|on|at|a|an|for|during|what|show|me|tell|please|can|you|to|both|of|between|difference|gap|home|away|game|games|average|averages|per|total|totals|why|explain|explains|better|improve|improved|sustainable)\b",
        " ",
        text,
    )
    remainder = re.sub(r"['’]s\b|[\s,?.!:;()/–-]+", "", text)
    if remainder:
        raise SemanticError(
            "unsupported_scope",
            "I cannot preserve every requested condition. Supported player splits are regular season versus playoffs, home versus away, last N versus prior N games, and before/after an explicit date, with an optional date range and opponent abbreviation. No condition was dropped.",
        )
    return dict(
        kind=kind,
        player=player,
        groups=groups,
        metrics=metrics or list(DEFAULT_METRICS),
        aggregation=aggregation,
        interpretive=interpretive,
        season=season,
        start=str(start),
        end=str(end),
    )


def answer_split(question, evidence, players, season):
    request = parse_split(question, players, season)
    teams = {r.get("opponent_abbr") for r in evidence.rows}
    if any(
        spec.get("opponent_abbr") and spec["opponent_abbr"] not in teams
        for _, spec in request["groups"]
    ):
        raise SemanticError(
            "clarification_required",
            "Use an opponent abbreviation present in this source.",
        )
    contract = load_contract()
    results, rows, definitions, used_ids = [], [], [], set()
    for key in request["metrics"]:
        metric = contract.metric(key)
        aggregation = (
            "ratio" if "ratio" in metric.aggregations else request["aggregation"]
        )
        queries = [
            Query(metric=key, aggregation=aggregation, **spec)
            for _, spec in request["groups"]
        ]
        result = compare_queries(evidence, *queries)
        result["queries"] = [asdict(q) for q in queries]
        results.append(result)
        values, coverage = [], []
        for side in ("current", "baseline"):
            sample = next(iter(result[side]["rows"]), {})
            used_ids.update(sample.get("game_ids", []))
            value = sample.get("value")
            if value is not None and metric.unit == "ratio":
                value *= 100
            values.append(
                "Unavailable"
                if value is None
                else f"{value:.2f}" + ("%" if metric.unit == "ratio" else "")
            )
            coverage.append(
                game_coverage(
                    sample.get("valid_games", 0), sample.get("observed_games", 0)
                )
            )
        delta = result["difference"]
        rows.append(
            [
                metric.label
                + (
                    " per game"
                    if aggregation == "average"
                    else " total"
                    if aggregation == "total"
                    else ""
                ),
                *values,
                "Unavailable"
                if delta is None
                else f"{delta:+.2f}" + (" pp" if metric.unit == "ratio" else ""),
                *coverage,
            ]
        )
        definitions.append(
            dict(
                key=key,
                label=metric.label,
                unit=metric.unit,
                definition=metric.numerator
                + (f" / ({metric.denominator})" if metric.denominator else ""),
            )
        )
    labels = [label for label, _ in request["groups"]]
    all_observed = all(
        result[side]["rows"] for result in results for side in ("current", "baseline")
    )
    limits = "These are observed box-score differences, not evidence of causation, future performance or overall player quality."
    answer = f"{request['player']['player_name']}: {labels[0]} versus {labels[1]} in {request['season']}. Differences are {labels[0]} minus {labels[1]}. "
    highlights = [
        f"{row[0]}: {row[1]} versus {row[2]}"
        for row in rows[:3]
        if row[3] != "Unavailable"
    ]
    if highlights:
        answer += "; ".join(highlights) + ". "
    if not all_observed:
        answer += "At least one group has no recorded appearances; missing results are unavailable, not zero. "
    if any(r["difference"] is None for r in results):
        answer += "Some differences are withheld because inputs or denominators are incomplete. "
    answer += limits
    profile_rows = [
        r
        for r in evidence.rows
        if r["player_id"] == request["player"]["player_id"] and r["game_id"] in used_ids
    ]
    axis = {
        "phase": "regular season vs playoffs",
        "venue": "home vs away",
        "recent_prior": f"last {request['groups'][0][1].get('n')} games vs prior {request['groups'][1][1].get('n')} games",
        "before_after": f"before and after {request['groups'][0][1].get('start_date')}",
    }[request["kind"]]
    first = request["groups"][0][1]
    phase_label = (
        (
            "both regular season and playoffs"
            if first["season_type"] == "Both"
            else first["season_type"]
        )
        if request["kind"] != "phase"
        else ""
    )
    time_label = (
        f"as of {request['end']}"
        if request["kind"] == "recent_prior"
        else f"from {request['start']} through {request['end']}"
    )
    metric_labels = [
        dict(pts_per36="points per 36", ast_per36="assists per 36").get(
            key, contract.metric(key).label
        )
        for key in request["metrics"]
    ]
    canonical = f"Compare {request['player']['player_name']} {' and '.join(metric_labels)} {axis} {request['season']} {phase_label} {time_label} {request['aggregation']}"
    if first.get("opponent_abbr"):
        canonical += f" against {first['opponent_abbr']}"
    if request["kind"] != "venue" and first.get("home_away"):
        canonical += " at home" if first["home_away"] == "HOME" else " on the road"
    return dict(
        status="ok" if all_observed else "no_observations",
        answer=answer,
        answerability="partial" if request["interpretive"] else "descriptive",
        assumptions=[
            f"{request['season']}; {request['start']} through {request['end']}; {request['aggregation']} counts, pooled shooting ratios.",
            "Games with data counts complete metric inputs out of appearances in that group. Small samples are not season-wide evidence.",
        ],
        tables=[
            dict(
                title="Player split comparison",
                description=GAME_COVERAGE_NOTE
                + " Differences use unrounded values. "
                + f"Requested dates: {request['start']} through {request['end']}.",
                columns=[
                    dict(key=f"c{i}", label=label)
                    for i, label in enumerate(
                        [
                            "Metric",
                            *labels,
                            "Difference",
                            game_coverage_label(labels[0]),
                            game_coverage_label(labels[1]),
                        ]
                    )
                ],
                rows=rows,
            )
        ],
        charts=[],
        metric_definitions=definitions,
        followups=[],
        clarification_options=[],
        player_profile=identity_profile(request["player"], profile_rows),
        semantic_evidence=dict(
            kind="player_split",
            version="player_splits/1",
            request=request,
            comparisons=results,
            context_question=canonical,
        ),
        conversation_context=dict(
            analysis_type="player_split",
            question=question,
            players=[{k: request["player"][k] for k in ("player_id", "player_name")}],
            split_question=canonical,
            metrics=request["metrics"],
            scope={},
        ),
        semantic_plan=dict(
            status="compare", model_calls=0, queries=[r["queries"] for r in results]
        ),
    )
