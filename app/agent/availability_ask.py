"""Resolve composable availability questions against source-backed identities.

Every meaningful phrase must be consumed. Unrecognized constraints are refused,
never dropped into a simpler statistics query. No fixed player pairs or model SQL.
"""

from __future__ import annotations

import logging
import re

from app.agent.followup import analysis_context
from app.agent.metric_presentation import (
    GAME_COVERAGE_NOTE,
    game_coverage,
    game_coverage_label,
)
from app.agent.performance_overview import scoped_identity_profile
from app.agent.research_ask import refusal
from app.agent.semantic_serving import requested_seasons, source_players
from app.agent.semantics import SemanticError
from app.availability import compare_availability, load_availability
from app.research import CORE_METRICS

METRICS = {
    "pts_per36": r"points per 36(?: minutes)?",
    "ast_per36": r"assists per 36(?: minutes)?",
    "ts_pct": r"true shooting(?: percentage| percent|%)?|ts%",
    "efg_pct": r"effective field goal(?: percentage| percent|%)?|efg%",
    "fg3_pct": r"(?:three.point|3.point|3pt) (?:percentage|percent)|3p%|fg3%",
    "fg_pct": r"field goal (?:percentage|percent)|fg%",
    "ft_pct": r"free throw (?:percentage|percent)|ft%",
    "fg3a": r"(?:three.point|3.point) attempts|fg3a",
    "fg3m": r"(?:three.point|3.point) (?:makes|made)|threes|3pm|fg3m",
    "fga": r"field goal attempts|fga",
    "fta": r"free throw attempts|fta",
    "pts": r"points|scoring|pts",
    "reb": r"rebounds|rebounding|reb",
    "ast": r"assists|ast",
    "stl": r"steals|stl",
    "blk": r"blocks|blk",
    "tov": r"turnovers|tov",
    "min": r"minutes|min",
    "plus_minus": r"plus.minus|\+/-",
}


def wants_availability(question):
    # These idioms ask for information; "out" is not a participation condition.
    question = re.sub(
        r"\b(?:find|figure|check|work|point|turns?)\s+out\b", "", question, flags=re.I
    )
    return bool(
        re.search(
            r"\b(?:without|out|absent|absence|sidelined|unavailable|availability|injured|injury)\b|\b(?:did not|didn.t) play\b|\b(?:missed games?|sat out)\b",
            question,
            re.I,
        )
    )


def availability_followup(question):
    return bool(
        re.search(
            r"^(?:include|exclude|and|what about|how about|only|now|instead|compare|show|use|in |from )|\b(?:both played|same games|same scope|significan\w*|reliable|consistent|pattern|causal|cause)\b",
            question.strip(),
            re.I,
        )
    )


def _identities(question, players):
    hits = []
    for player in players:
        for alias in {
            player["player_name"],
            *player["aliases"],
            player["player_name"].split()[0],
        }:
            for match in re.finditer(
                r"(?<!\w)" + re.escape(alias) + r"(?!\w)", question, re.I
            ):
                hits.append((match.start(), match.end(), player))
    # Keep longest names rather than resolving a surname inside a full name.
    hits = [
        h
        for h in hits
        if not any(
            x[0] <= h[0] and x[1] >= h[1] and x[1] - x[0] > h[1] - h[0] for x in hits
        )
    ]
    spans = {}
    for start, end, player in hits:
        spans.setdefault((start, end), {})[player["player_id"]] = player
    known_teams = set().union(
        *[
            set(next(iter(matches.values())).get("observed_teams", []))
            for matches in spans.values()
            if len(matches) == 1
        ]
    )
    for span, matches in spans.items():
        if len(matches) > 1 and known_teams:
            same_team = {
                pid: p
                for pid, p in matches.items()
                if known_teams.intersection(p.get("observed_teams", []))
            }
            if len(same_team) == 1:
                spans[span] = same_team
    if any(len(matches) != 1 for matches in spans.values()):
        raise SemanticError(
            "clarification_required",
            "Please give both players’ full names; an alias matches more than one player.",
        )
    selected = {}
    for (start, end), matches in sorted(spans.items(), reverse=True):
        player = next(iter(matches.values()))
        selected[player["player_id"]] = player
        question = question[:start] + f" PLAYER{player['player_id']} " + question[end:]
    question = re.sub(r"(PLAYER\d+)(?:\s+\1)+", r"\1", question)
    return " ".join(question.lower().split()), selected


def parse_request(question, players, season, previous=None, teams=()):
    if re.search(r"\bplayer\d+\b", question, re.I):
        raise SemanticError(
            "clarification_required",
            "Use player names rather than internal identity tokens.",
        )
    text, named = _identities(" ".join(question.split()), players)
    prior = previous or {}
    if named:
        if len(named) != 2:
            raise SemanticError(
                "clarification_required",
                "Name one focal player and one teammate for this comparison.",
            )
        teammate_matches = list(
            re.finditer(
                r"\bwithout\s+player(\d+)|\bplayer(\d+)\s+(?:(?:was|is|were|has been|being)\s+)?(?:out|absent|sidelined|unavailable|missing|did not play|didn.t play|missed games?|sat out)\b|\bplayer(\d+)\s*[\x27’]s\s+absence\b",
                text,
            )
        )
        if not teammate_matches:
            raise SemanticError(
                "unsupported_scope",
                "Specify which player to analyze and which teammate was out, for example “How did A play when B was out?”",
            )
        exposures = {
            int(next(g for g in match.groups() if g)) for match in teammate_matches
        }
        if len(exposures) != 1:
            raise SemanticError(
                "unsupported_scope", "Analyze one focal/exposure direction at a time."
            )
        exposure = exposures.pop()
        focal = next(pid for pid in named if pid != exposure)
        request = dict(
            player_id=focal,
            teammate_id=exposure,
            season=season,
            phase="Both",
            start=None,
            end=None,
            home_away=None,
            opponent=None,
            metrics=list(CORE_METRICS),
            aggregation="average",
        )
        # Repeated same-pair questions in a chat retain explicit prior scope.
        if prior.get("player_id") == focal and prior.get("teammate_id") == exposure:
            request.update(prior)
    elif prior:
        request = dict(prior)
    else:
        raise SemanticError(
            "clarification_required",
            "Please name the player and teammate using their full names or recognized aliases.",
        )
    request.setdefault("include_limited_minutes", False)
    request.setdefault("minutes_threshold", 0.5)
    if re.search(r"\b(include|exclude) limited[ -]minute appearances\b", text):
        request["include_limited_minutes"] = bool(re.search(r"\binclude limited", text))
        text = re.sub(
            r"\b(?:include|exclude) limited[ -]minute appearances\b", " ", text
        )
    threshold = re.search(r"\b(\d+(?:\.\d+)?)% minutes threshold\b", text)
    if threshold:
        value = float(threshold[1]) / 100
        if not 0 < value <= 1:
            raise SemanticError(
                "invalid_scope", "Minutes threshold must be above 0% and at most 100%."
            )
        request["minutes_threshold"] = value
        text = text[: threshold.start()] + " " + text[threshold.end() :]
    text = re.sub(r"player\d+", " ", text)
    selected = requested_seasons(question, request["season"])
    if len(selected) != 1:
        raise SemanticError(
            "unsupported_scope", "Choose one season for this availability comparison."
        )
    if selected[0] != request["season"]:
        request.update(start=None, end=None)
    request["season"] = selected[0]
    text = re.sub(
        r"\b20\d{2}[-–/]\d{2}(?!\d|[-/])|\b(?:last|previous|prior|this) season\b",
        " ",
        text,
    )
    dates = re.search(
        r"\bfrom (\d{4}-\d{2}-\d{2}) (?:through|to) (\d{4}-\d{2}-\d{2})", text
    )
    if dates:
        from datetime import date

        start, end = [date.fromisoformat(d).isoformat() for d in dates.groups()]
        if start > end:
            raise SemanticError("invalid_scope", "Start date must precede end date.")
        request.update(start=start, end=end)
        text = text[: dates.start()] + " " + text[dates.end() :]
    regular = bool(re.search(r"\bregular[ -]season\b", text))
    playoffs = bool(re.search(r"\b(?:playoffs?|postseason)\b", text))
    if (
        regular
        and playoffs
        and re.search(r"\b(?:compare|versus|vs)\b", text)
        and not re.search(r"\b(?:both|including|combined)\b", text)
    ):
        raise SemanticError(
            "unsupported_scope",
            "A phase-versus-phase comparison requires separate analyses; the phases were not pooled.",
        )
    if regular or playoffs:
        request["explicit_phase"] = True
        request["phase"] = (
            "Both"
            if regular and playoffs
            else "Regular Season"
            if regular
            else "Playoffs"
        )
        text = re.sub(r"\bregular[ -]season\b|\b(?:playoffs?|postseason)\b", " ", text)
    venues = []
    for pattern, venue in [
        (r"\bat home\b|\bhome games\b", "home"),
        (r"\bon the road\b|\baway games\b", "away"),
    ]:
        if re.search(pattern, text):
            venues.append(venue)
            request["home_away"] = venue
            text = re.sub(pattern, " ", text)
    if len(venues) > 1:
        raise SemanticError(
            "unsupported_scope", "Choose one venue filter or omit it for all venues."
        )
    text = re.sub(r"did not play|didn.t play", "absent", text)
    opponent = re.search(r"\bagainst ([a-z]{2,3})\b", text)
    if opponent:
        abbreviation = opponent[1].upper()
        if abbreviation not in teams:
            raise SemanticError(
                "clarification_required",
                "Use an opponent abbreviation present in the selected evidence.",
            )
        request["opponent"] = abbreviation
        text = text[: opponent.start()] + " " + text[opponent.end() :]
    metrics = []
    for key, pattern in METRICS.items():
        pattern = r"(?<!\w)(?:" + pattern + r")(?!\w)"
        if re.search(pattern, text):
            metrics.append(key)
            text = re.sub(pattern, " ", text)
    if metrics:
        request["metrics"] = metrics
    if re.search(r"\btotal\w*\b", text) and re.search(
        r"\baverag\w*\b|\bper game\b", text
    ):
        raise SemanticError(
            "unsupported_scope",
            "Choose averages or totals for this comparison, rather than mixing aggregations.",
        )
    if re.search(r"\b(?:totals?|in total)\b", text):
        request["aggregation"] = "total"
    elif re.search(r"\b(?:averag\w*|per game)\b", text):
        request["aggregation"] = "average"
    # No leftover constraint may disappear into an ordinary season summary.
    allowed = r"\b(?:tell|me|how|did|does|do|was|is|were|has|been|play|played|perform|performed|performance|stats|statistics|when|while|with|without|out|absent|sidelined|unavailable|absence|being|missing|missed|sat|in|the|a|an|and|or|of|for|during|only|what|about|show|compare|compared|versus|vs|to|both|together|games|game|same|scope|now|instead|use|please|including|can|you|average|averages|averaged|per|total|totals)\b"
    leftover = re.sub(allowed, " ", text)
    leftover = re.sub(r"['’]s\b|[\s,?.!:;()]+", "", leftover)
    if leftover:
        raise SemanticError(
            "unsupported_scope",
            "I cannot apply every requested condition yet. Supported availability comparisons include per-game or total box scores, shooting rates, one season, explicit date ranges, season phase, home/away and opponent abbreviation. No condition was dropped.",
        )
    return request


def render_answer(result, players):
    request = result["request"]
    by_id = {p["player_id"]: p for p in players}
    focal, teammate = (by_id[request[k]] for k in ("player_id", "teammate_id"))
    counts = {k: len(v) for k, v in result["groups"].items()}
    selected = result["metrics"]
    covered_phases = result["covered_phases"]
    effective_phase = "Both" if len(covered_phases) == 2 else covered_phases[0]
    phase_label = (
        "regular season and playoffs"
        if effective_phase == "Both"
        else effective_phase.lower()
    )
    coverage_note = (
        " "
        + ", ".join(result["missing_phases"])
        + " are not covered by this source; only the labeled phase is included."
        if result["missing_phases"]
        else ""
    )
    display_end = min(
        request["end"] or result["source_through"], result["source_through"]
    )

    def display(value):
        return "unavailable" if value is None else f"{value:.1f}"

    statements = []
    rows = []
    charts = []
    for metric in selected:
        a, b = (metric["groups"][k] for k in ("both_played", "did_not_play"))
        label = metric["label"]
        unit = (
            "%"
            if metric["unit"] == "%"
            else "per 36 minutes"
            if metric["metric"].endswith("per36")
            else "total"
            if request["aggregation"] == "total"
            else "per game"
        )
        if len(statements) < 3:
            statements.append(
                f"{display(b['display_value'])} {label} ({unit}), versus {display(a['display_value'])} when both played"
            )
        rows.append(
            [
                label,
                display(a["display_value"]),
                display(b["display_value"]),
                display(metric["difference"]),
                ("% (values); percentage points (difference)" if unit == "%" else unit),
                game_coverage(a["valid_games"], a["observed_games"]),
                game_coverage(b["valid_games"], b["observed_games"]),
            ]
        )
        if all(
            v["value"] is not None and not v["missing_component_games"] for v in (a, b)
        ):
            charts.append(
                dict(
                    type="bar",
                    title=f"{focal['player_name']} · {label}",
                    x_label="Teammate status",
                    y_label=f"{label} ({unit})",
                    description=f"{focal['player_name']}; {request['season']} {phase_label}; {request['start'] or 'season start'} through {display_end}. Verified non-participation versus both played; not shared court time or a causal effect.{coverage_note}",
                    series=[
                        dict(
                            key=metric["metric"],
                            label=label,
                            points=[
                                dict(
                                    x="Both played",
                                    y=a["display_value"],
                                    meta=f"{game_coverage(a['valid_games'], a['observed_games'])} games with data",
                                ),
                                dict(
                                    x=f"{teammate['player_name']} did not play",
                                    y=b["display_value"],
                                    meta=f"{game_coverage(b['valid_games'], b['observed_games'])} games with data",
                                ),
                            ],
                        )
                    ],
                )
            )
    scope = {
        **request,
        "start": request["start"] or result["observed_start"],
        "end": display_end,
        "phase": effective_phase,
    }
    qualifier = ", ".join(
        filter(None, [request.get("home_away"), request.get("opponent")])
    )
    answer = (
        f"In {counts['did_not_play']} {'appearance' if counts['did_not_play'] == 1 else 'appearances'} where {teammate['player_name']} did not play, {focal['player_name']} recorded "
        + "; ".join(statements)
        + "."
    )
    if not counts["did_not_play"]:
        answer = f"No eligible appearances for {focal['player_name']} with {teammate['player_name']} confirmed not playing remain in this scope. The absence comparison is unavailable; the game details explain exclusions."
    if 0 < counts["did_not_play"] < 5:
        answer += " This is a very small verified sample; it should not be treated as a typical performance level."
    answer += (
        f"\n\nThe comparison group contains {counts['both_played']} same-team games where both appeared. {request['season']} · {phase_label} · {scope['start']} through {scope['end']}"
        + (f" · {qualifier}" if qualifier else "")
        + "."
    )
    missing = sum(result["excluded"].values())
    answer += coverage_note
    if missing:
        answer += (
            f" {missing} other focal appearances were excluded: "
            + ", ".join(
                f"{key.replace('_', ' ')} ({n})"
                for key, n in result["excluded"].items()
            )
            + "."
        )
    if any(
        g["missing_component_games"] for m in selected for g in m["groups"].values()
    ):
        answer += "\n\nSome statistics have missing components. The table shows games with data for each stat; incomplete differences are withheld."
    answer += "\n\nThese are observed differences, not evidence that the absence caused them. Did not play does not establish an injury cause."
    policy = result["policy"]
    answer += (
        f"\n\n{result['scope_appearances']} scoped records: {counts['both_played']} included when both played + "
        f"{counts['did_not_play']} included when the teammate did not play + {missing} excluded. "
        + (
            "Limited-minute appearances are included. "
            if policy["include_limited_minutes"]
            else f"Appearances below {policy['minutes_threshold']:.0%} of either participating player's usual minutes are excluded. "
        )
        + "Usual minutes is the median of the previous 10 appearances this season (at least 5 required), using only earlier games. "
        "Insufficient baselines stay included and are marked in the game details. Limited minutes do not establish an injury."
    )
    game_details = []
    for group in result["groups"].values():
        game_details.extend({**g, "reason": "included"} for g in group)
    game_details.extend(result["excluded_games"])
    audit_rows = []
    for game in sorted(game_details, key=lambda g: (g["game_date"], g["game_id"])):
        checks = "; ".join(
            f"{by_id[c['player_id']]['player_name']}: {c['minutes']:g} min; "
            + (
                "insufficient baseline"
                if c["baseline_minutes"] is None
                else f"usual {c['baseline_minutes']:g}, cutoff {c['cutoff_minutes']:g} ({c['status'].replace('_', ' ')})"
            )
            for c in game["minutes_checks"]
        )
        audit_rows.append(
            [
                game["game_date"],
                game["opponent"],
                game["participation"].replace("_", " "),
                game["reason"].replace("_", " "),
                game["report_status"],
                checks or "—",
                "; ".join(game["warnings"]) or "—",
            ]
        )

    public = {k: v for k, v in result.items() if k != "rows"}
    profile = scoped_identity_profile(focal, result["rows"], scope)
    profile["sampleLabel"] = (
        f"{len(result['rows'])} appearances included · {result['scope_appearances']} records in scope"
    )
    return dict(
        status="ok",
        answer=answer,
        tables=[
            dict(
                title=f"{focal['player_name']} — {teammate['player_name']} availability",
                columns=[
                    dict(key=str(i), label=v)
                    for i, v in enumerate(
                        [
                            "Stat",
                            "Both played",
                            f"{teammate['player_name']} did not play",
                            "Difference (did not play − both)",
                            "Unit",
                            game_coverage_label("both played"),
                            game_coverage_label("teammate did not play"),
                        ]
                    )
                ],
                rows=rows,
                description=GAME_COVERAGE_NOTE,
            ),
            dict(
                title="Game inclusion details",
                collapsible=True,
                description="Every scoped record is included once or excluded once. Excluded limited-minute appearances still count as games played. Missing participation stays unknown.",
                columns=[
                    dict(key=str(i), label=v)
                    for i, v in enumerate(
                        [
                            "Date",
                            "Opponent",
                            "Participation",
                            "Decision",
                            "Pregame report",
                            "Minutes and baseline",
                            "Data notes",
                        ]
                    )
                ],
                rows=audit_rows,
            ),
        ],
        charts=charts[:1],
        player_profile=profile,
        availability_evidence=public,
        availability_scope=request,
        conversation_context=dict(
            version=1,
            analysis_type="availability",
            players=[
                dict(player_id=p["player_id"], player_name=p["player_name"], role=role)
                for p, role in ((focal, "focal"), (teammate, "teammate"))
            ],
            scope=dict(
                season=request["season"],
                start=request["start"],
                end=request["end"],
                phases=covered_phases,
            ),
            metrics=request["metrics"],
            availability_scope=request,
        ),
        followups=[
            "What about assists?",
            "Only the playoffs",
            "What about points per 36 minutes?",
            "Exclude limited-minute appearances"
            if request["include_limited_minutes"]
            else "Include limited-minute appearances",
            "Use a 60% minutes threshold",
        ],
        assumptions=[
            "Final positive minutes establish participation even if a pregame report said Out. Non-participation requires verified same-team zero minutes or a valid Out report with no appearance; missing records alone do not establish absence.",
            "Corrected historical statistics and retrospectively collected reports; not a reconstruction of what was known at the time.",
        ],
        tool_calls=[],
        clarification_options=[],
    )


def answer_availability(agent, question, conversation_id=None, trace=None):
    if trace:
        trace.route = "availability"
    try:
        previous = None
        if conversation_id and agent.conversation_store:
            turns = agent.conversation_store.get_turns(
                conversation_id, max_turns=agent.settings.agent_conversation_max_turns
            )
            context = next(
                (turn.context for turn in reversed(turns) if turn.context), {}
            )
            previous = context.get("availability_scope")
        loaded = load_availability(agent.settings.research_availability_path)
        players = source_players(loaded[1])
        selected_season = requested_seasons(
            question, (previous or {}).get("season", agent.settings.season)
        )
        for player in players:
            player["observed_teams"] = sorted(
                {
                    r["team_abbr"]
                    for r in loaded[1].rows
                    if r["player_id"] == player["player_id"]
                    and r["season"] in selected_season
                }
            )
        request = parse_request(
            question,
            players,
            agent.settings.season,
            previous,
            {r["team_abbr"] for r in loaded[1].rows},
        )
        result = compare_availability(loaded, request)
        payload = render_answer(result, players)
        payload["conversation_id"] = conversation_id
        if conversation_id and agent.conversation_store:
            agent.conversation_store.append_turn(
                conversation_id,
                question=question,
                answer=payload["answer"],
                max_turns=agent.settings.agent_conversation_max_turns,
                context=analysis_context(question, payload),
            )
        if trace:
            trace.outcome = "answered"
        return payload
    except SemanticError as exc:
        return refusal(str(exc), code=exc.code, trace=trace)
    except (OSError, ValueError, KeyError, TypeError):
        logging.getLogger(__name__).exception(
            "Availability evidence or request validation failed"
        )
        return refusal(
            "Availability evidence or scope could not be validated. No overall statistics were substituted.",
            code="invalid_availability_evidence",
            trace=trace,
        )
