"""Source-backed player overviews sharing explicit metric samples and one scope."""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, timedelta

from app.agent.context_metrics import context_metrics
from app.agent.overview_calendar import MONTH_PATTERN, calendar_window, shift_months
from app.agent.overview_clarification import WINDOW_MESSAGE, OverviewClarification
from app.agent.overview_policy import (
    focus,
    metric_table,
    mixed_focus,
    presentation,
    strip_focus,
)
from app.agent.overview_positions import (
    add_position_comparisons,
    comparison_intent,
    peer_question,
    requested_group,
)
from app.agent.overview_summary import game_insights, summary_paragraphs
from app.agent.question_intent import has_specific_metric
from app.agent.semantics import SemanticError, aggregate, load_contract
from app.seasons import SEASONS, season_bounds

METRICS = (
    ("pts", "Points"),
    ("reb", "Rebounds"),
    ("ast", "Assists"),
    ("stl", "Steals"),
    ("blk", "Blocks"),
)


def wants_overview(question):
    original = peer_question(question.split("\nClarification:", 1)[0])
    return (
        bool(
            re.search(
                r"\b(perform(?:ing|ed|ance)?|overview|stats|statistics|profile|contribut(?:e[ds]?|ing|ions?)|assessment|all[ -](?:around|round)|scoring|shooting|playmaking|rebounding|defen[cs]e|defend(?:ed|ing)?)\b"
                r"|\bhow\b[^?\n]*\b(?:play(?:ed|ing)?|do(?:ing)?|done|fare[ds]?)\b",
                original,
                re.I,
            )
        )
        and not mixed_focus(original)
        and not has_specific_metric(
            strip_focus(original) if focus(original) != "general" else original
        )
        and not bool(
            re.search(
                r"\b(fantasy|rank|top|compare|versus|vs|opponent|against|home|away|road|who|which|leaders?|led|best|most|highest|percentages?|game[ -]logs?|game[ -]by[ -]game)\b",
                original,
                re.I,
            )
        )
    )


def overview_scope(question, selected_season, today=None, *, comparison=None):
    comparison = comparison if comparison is not None else comparison_intent(question)
    question = peer_question(question)
    question = question.replace("–", "-")
    first = bool(re.search(r"\bfirst (?:two|2) appearances\b", question, re.I))
    if re.search(r"\bfirst \w+ (?:appearances|games)\b", question, re.I) and not first:
        raise SemanticError(
            "clarification_required",
            "Please use an explicit date range or first two appearances for this overview.",
        )
    if first:
        question = re.sub(r"\bfirst (?:two|2) appearances\b", "", question, flags=re.I)
        if not re.search(r"playoffs?|regular season", question, re.I):
            question += " regular season"
    from app.agent.question_intent import round_scope_message

    if message := round_scope_message(question):
        raise SemanticError("unsupported_scope", message)
    if not re.search(MONTH_PATTERN, question, re.I):
        question = re.sub(
            r"\b(20\d{2})\s+(playoffs?|postseason)\b",
            lambda m: f"{int(m[1]) - 1}-{m[1][-2:]} {m[2]}",
            question,
            flags=re.I,
        )
    today = today or date.today()
    from app.agent.time_defaults import resolve_time_defaults

    question, _, _ = resolve_time_defaults(question, today)
    recent = bool(re.search(r"\blast 10 games\b", question, re.I))
    if recent:
        question = re.sub(r"\blast 10 games\b", "", question, flags=re.I)
    explicit = re.search(
        r"\bfrom (\d{4}-\d{2}-\d{2}) (?:to|through) (\d{4}-\d{2}-\d{2})\b",
        question,
        re.I,
    )
    trailing = re.search(
        r"\b(?:past|last|preceding)\s+(\d+)\s+(months?|weeks?|days?)\b", question, re.I
    )
    end = today
    as_of = re.search(r"\bas[ -]of\s+(\d{4}-\d{2}-\d{2})\b", question, re.I)
    if as_of:
        try:
            end = date.fromisoformat(as_of[1])
        except ValueError as exc:
            raise SemanticError(
                "clarification_required", "Please provide a valid as-of date."
            ) from exc
    calendar_scope = calendar_window(question, end)
    if comparison.get("baseline_period") and (
        first or recent or explicit or trailing or calendar_scope or as_of
    ):
        raise OverviewClarification("window_baseline", WINDOW_MESSAGE)
    if calendar_scope and (explicit or trailing):
        raise SemanticError(
            "clarification_required",
            "Please specify a single calendar window or date range.",
        )
    if trailing and re.search(r"\b20\d{2}[-/]\d{2}\b(?![-/]\d)", question):
        raise SemanticError(
            "clarification_required",
            "Please use explicit start and end dates when combining a trailing window with a named season.",
        )
    if explicit:
        try:
            start, end = (date.fromisoformat(d) for d in explicit.groups())
        except ValueError as exc:
            raise SemanticError(
                "clarification_required", "Please provide valid start and end dates."
            ) from exc
    elif calendar_scope:
        start, end, calendar_month = calendar_scope
    elif trailing:
        n, unit = int(trailing[1]), trailing[2].lower()
        if not 1 <= n <= 1095:
            raise SemanticError(
                "clarification_required",
                "Please choose a positive window within the available three seasons.",
            )
        start = (
            shift_months(end, n) + timedelta(days=1)
            if unit.startswith("month")
            else end - timedelta(days=n * (7 if unit.startswith("week") else 1) - 1)
        )
    else:
        if re.search(r"\b(last|past|prior|previous|since|yesterday)\b", question, re.I):
            raise SemanticError(
                "clarification_required",
                "Please specify a date range (from YYYY-MM-DD through YYYY-MM-DD) or a number of days, weeks, or months.",
            )
        named = re.findall(r"\b20\d{2}-\d{2}\b(?!-\d)", question)
        if len(set(named)) > 1:
            raise SemanticError(
                "clarification_required",
                "For a combined overview, please specify the start and end dates.",
            )
        season = named[0] if named else selected_season
        if recent and as_of and not named:
            candidates = [
                s
                for s in SEASONS
                if season_bounds(s)[0] <= date.fromisoformat(as_of[1])
            ]
            if not candidates:
                raise SemanticError(
                    "unsupported_coverage",
                    "The as-of date precedes available archives.",
                )
            season = candidates[0]
        start, end = season_bounds(season)
        if (
            recent
            and as_of
            and not named
            and date.fromisoformat(as_of[1]).year == int(season[:4])
            and date.fromisoformat(as_of[1]).month >= 7
        ):
            # A recent-game request may need the preceding season during the
            # off-season/start of season; retain its explicit upper date/phase.
            previous = SEASONS[min(SEASONS.index(season) + 1, len(SEASONS) - 1)]
            start = season_bounds(previous)[0]
        end = min(end, today, date.fromisoformat(as_of[1]) if as_of else today)
    named_seasons = re.findall(r"\b20\d{2}-\d{2}\b(?!-\d)", question)
    if calendar_scope and any(
        start < season_bounds(season)[0] or end > season_bounds(season)[1]
        for season in named_seasons
    ):
        raise SemanticError(
            "clarification_required",
            "The calendar window conflicts with the named season. Please specify one date range.",
        )
    if end < start or end > today or (as_of and end > date.fromisoformat(as_of[1])):
        raise SemanticError(
            "clarification_required",
            "Please choose an ordered date range ending no later than today.",
        )
    previous_end = start - timedelta(days=1)
    previous_start = (
        shift_months(previous_end, int(trailing[1])) + timedelta(days=1)
        if trailing and trailing[2].lower().startswith("month")
        else start - (end - start + timedelta(days=1))
    )
    phases = ["Regular Season", "Playoffs"]
    if re.search(r"\b(?:excluding|except|without)\b", question, re.I):
        raise SemanticError(
            "clarification_required",
            "Please state the included phase: regular season, playoffs, or both.",
        )
    if re.search(r"\b(regular[ -]season|playoffs?|postseason)\b", question, re.I):
        regular = bool(re.search(r"regular[ -]season", question, re.I))
        playoffs = bool(re.search(r"playoffs?|postseason", question, re.I))
        phases = (["Regular Season"] if regular else []) + (
            ["Playoffs"] if playoffs else []
        )
    if re.search(
        r"\b(preseason|play-in|playin|play in tournament|summer league|quarter|injur)\w*",
        question,
        re.I,
    ):
        raise SemanticError(
            "unsupported_coverage",
            "This overview covers regular-season and playoff box scores only.",
        )
    if calendar_scope and calendar_month:
        previous_start = shift_months(start, 1)
        previous_end = start - timedelta(days=1)
    elif calendar_scope and re.search(r"\bpast month\b", question, re.I):
        previous_start = shift_months(previous_end, 1) + timedelta(days=1)
    previous_phases = list(phases)
    if not trailing and not explicit and not calendar_scope:
        if phases == ["Playoffs"]:
            # A full playoff overview compares against this season's regular season.
            previous_start, previous_end = start, end
            previous_phases = ["Regular Season"]
        else:
            previous_start = shift_months(start, 12)
            previous_end = shift_months(end, 12)
    if recent:
        # Actual last-N/prior-N bounds are resolved from this bounded pool later.
        previous_start, previous_end = start, end
        previous_phases = list(phases)
    if comparison.get("baseline_period"):
        previous_start, previous_end = (
            (shift_months(start, 12), shift_months(end, 12))
            if comparison["baseline_period"] == "prior_season"
            else (start, end)
        )
        previous_phases = comparison.get("baseline_phases") or list(phases)
    comparison_label = (
        f"{' + '.join(previous_phases)} from {previous_start} through {previous_end}"
    )
    seasons = [
        s
        for s in SEASONS
        if season_bounds(s)[0] <= end and season_bounds(s)[1] >= previous_start
    ]
    if not seasons or start < season_bounds(SEASONS[-1])[0]:
        raise SemanticError(
            "unsupported_coverage",
            "The requested period starts before available 2023–24 coverage.",
        )
    return dict(
        comparison_intent=comparison,
        recent_appearances=10 if recent else None,
        first_appearances=2 if first else None,
        start=start,
        end=end,
        previous_start=previous_start,
        previous_end=previous_end,
        phases=phases,
        previous_phases=previous_phases,
        comparison_label=comparison_label,
        seasons=seasons,
        chart_grain="daily" if (end - start).days < 31 else "monthly",
    )


def identity_profile(player, rows):
    appearances = sorted(
        (r for r in rows if r["player_id"] == player["player_id"]),
        key=lambda r: str(r["game_date"]),
    )
    return {
        "player": {
            "player_id": player["player_id"],
            "player_name": player["player_name"],
            "team_abbr": appearances[-1].get("team_abbr") if appearances else None,
            "games_sampled": len(appearances),
            "headshot_url": f"https://cdn.nba.com/headshots/nba/latest/1040x760/{player['player_id']}.png",
        },
        "profile_url": f"/players/{player['player_id']}",
    }


def scoped_identity_profile(player, rows, scope):
    """Describe the answer's identity and scope, without current ranking data.

    Callers supply only the appearances used by the answer. Frozen studies that
    do not publish those rows omit team and appearance counts rather than looking
    them up from a different season.
    """
    profile = identity_profile(player, rows)
    phase = scope.get("phase", "Regular Season")
    phase = "regular season and playoffs" if phase == "Both" else phase.lower()
    profile["scopeLabel"] = (
        f"{scope['season']} {phase} · "
        f"{scope.get('start') or 'season start'} through "
        f"{scope.get('end') or 'latest source date'}"
    )
    return profile


def resolve_overview_player(question, players, rows, selected=None):
    """Resolve only observed names/aliases; never choose between colliding IDs."""
    original, _, reply = question.partition("\nClarification:")
    original = original.split("\nSelected player:", 1)[0]

    def mentioned(text):
        hits = []
        for p in players:
            for name in (p["player_name"], *p.get("aliases", [])):
                for match in re.finditer(
                    r"(?<!\w)" + re.escape(name) + r"(?!\w)", text, re.I
                ):
                    hits.append((match.start(), match.end(), p))
        return list(
            {
                p["player_id"]: p
                for start, end, p in hits
                if not any(
                    a <= start and b >= end and b - a > end - start for a, b, _ in hits
                )
            }.values()
        )

    candidates = mentioned(original)
    if selected:
        allowed = candidates or players
        return [p for p in allowed if p["player_id"] == selected.get("player_id")]
    if reply and candidates:
        direct = mentioned(reply)
        candidate_ids = {p["player_id"] for p in candidates}
        direct = [p for p in direct if p["player_id"] in candidate_ids]
        if len(direct) == 1:
            return direct
        by_team_or_id = []
        for p in candidates:
            team = identity_profile(p, rows)["player"]["team_abbr"]
            if re.search(r"\b" + str(p["player_id"]) + r"\b", reply) or (
                team and re.search(r"\b" + re.escape(team) + r"\b", reply, re.I)
            ):
                by_team_or_id.append(p)
        if len(by_team_or_id) == 1:
            return by_team_or_id
    return candidates or mentioned(reply)


def build_overview(question, evidence, players, scope, selected=None):
    evidence.validate()
    candidates = resolve_overview_player(question, players, evidence.rows, selected)
    empty = dict(
        assumptions=[],
        tables=[],
        charts=[],
        metric_definitions=[],
        followups=[],
        player_profile=None,
        tool_calls=[],
        semantic_evidence=None,
    )
    if len(candidates) != 1:
        return {
            **empty,
            "status": "clarification_required",
            "answer": "Which player do you mean? Select an option, or reply with their full name, team abbreviation, or player ID.",
            "clarification_options": [
                {
                    "player_id": p["player_id"],
                    "player_name": p["player_name"],
                    "team_abbr": identity_profile(p, evidence.rows)["player"].get(
                        "team_abbr"
                    ),
                    "label": f"{p['player_name']} (ID {p['player_id']})",
                }
                for p in candidates
            ],
        }
    player = candidates[0]
    if scope.get("recent_appearances") or scope.get("first_appearances"):
        scope = dict(scope)
        scope["previous_phases"] = list(scope["phases"])
        games = sorted(
            (
                r
                for r in evidence.rows
                if r["player_id"] == player["player_id"]
                and r["season_type"] in scope["phases"]
                and scope["start"]
                <= date.fromisoformat(str(r["game_date"]))
                <= scope["end"]
            ),
            key=lambda r: (str(r["game_date"]), r["game_id"]),
        )
        if games:
            n = scope.get("first_appearances") or scope["recent_appearances"]
            chosen, baseline = (
                (games[:n], [])
                if scope.get("first_appearances")
                else (games[-n:], games[-2 * n : -n])
            )
            scope["start"] = date.fromisoformat(str(chosen[0]["game_date"]))
            scope["end"] = date.fromisoformat(str(chosen[-1]["game_date"]))
            scope["previous_end"] = scope["start"] - timedelta(days=1)
            scope["previous_start"] = (
                date.fromisoformat(str(baseline[0]["game_date"]))
                if baseline
                else scope["previous_end"]
            )
            scope["comparison_label"] = (
                "Prior recorded appearances in the same season and phases"
            )
            scope["chart_grain"] = "daily"
    start, end = scope["start"], scope["end"]

    def rows_between(lo, hi, phases):
        return [
            r
            for r in evidence.rows
            if r["season_type"] in phases
            and lo <= date.fromisoformat(str(r["game_date"])) <= hi
        ]

    current = rows_between(start, end, scope["phases"])
    previous_phases = scope.get("previous_phases", scope["phases"])
    previous = rows_between(
        scope["previous_start"], scope["previous_end"], previous_phases
    )
    own = [r for r in current if r["player_id"] == player["player_id"]]
    prior = [r for r in previous if r["player_id"] == player["player_id"]]
    contract = load_contract()
    baseline_covered = scope["previous_start"] >= season_bounds(SEASONS[-1])[0]
    for season in scope["seasons"]:
        bounds = season_bounds(season)
        for phase in scope["phases"]:
            if (
                bounds[0] <= end
                and bounds[1] >= start
                and (season, phase) not in evidence.covered_scopes
            ):
                raise SemanticError(
                    "unsupported_coverage", f"Missing {season} {phase} source coverage."
                )
        for phase in previous_phases:
            if (
                bounds[0] <= scope["previous_end"]
                and bounds[1] >= scope["previous_start"]
                and (season, phase) not in evidence.covered_scopes
            ):
                baseline_covered = False
    grain = scope.get("chart_grain", "daily" if (end - start).days < 31 else "monthly")
    metrics, charts = [], []
    grouped = defaultdict(list)
    for row in current:
        grouped[row["player_id"]].append(row)
    for key, label in METRICS:
        metric = contract.metric(key)
        value = aggregate(own, metric, "average")
        baseline = aggregate(prior, metric, "average")
        delta = (
            value["value"] - baseline["value"]
            if baseline_covered
            and value["value"] is not None
            and baseline["value"] is not None
            and not value["missing_component_games"]
            and not baseline["missing_component_games"]
            else None
        )
        buckets = defaultdict(list)
        for row in own:
            buckets[
                str(row["game_date"]) if grain == "daily" else str(row["game_date"])[:7]
            ].append(row)
        points = []
        for bucket, rows in sorted(buckets.items()):
            stats = aggregate(rows, metric, "average")
            if stats["value"] is not None:
                points.append(
                    {
                        "x": bucket,
                        "y": stats["value"],
                        "meta": f"{bucket} · {stats['valid_games']} / {len(rows)} games with {label.lower()} data",
                    }
                )
        # Do not draw a continuous trend across missing metric observations.
        if points and not value["missing_component_games"]:
            charts.append(
                {
                    "type": "line",
                    "title": f"{key.upper()} — {'game-by-game' if grain == 'daily' else 'monthly average'}",
                    "grain": grain,
                    "x_label": "Game date"
                    if grain == "daily"
                    else "Month with appearances",
                    "y_label": label + " per game",
                    "series": [{"key": key, "label": label, "points": points}],
                }
            )
        metrics.append(
            {
                "key": key,
                "label": label,
                **value,
                "previous": baseline,
                "change": delta,
                "trend": {"grain": grain, "points": points},
                **({"monthly": points} if grain == "monthly" else {}),
            }
        )
    phase = " + ".join(scope["phases"])
    extra = context_metrics(own, prior, baseline_covered=baseline_covered)
    from app.agent.overview_percentiles import add_percentiles

    add_percentiles(metrics + extra, grouped, player["player_id"], contract.min_games)
    add_position_comparisons(
        metrics + extra,
        grouped,
        player["player_id"],
        scope,
        evidence.position_context,
        question,
    )
    insights = game_insights(own)
    display_policy = presentation(
        metrics + extra, question, len(own), contract.min_games, insights=insights
    )
    scope = {
        **scope,
        "previous_appearances": len(prior),
        "comparison_intent": scope.get(
            "comparison_intent", comparison_intent(question)
        ),
    }
    paragraphs = summary_paragraphs(
        player["player_name"],
        metrics + extra,
        scope,
        len(own),
        insights,
        display_policy["profile"],
    )
    from app.agent.overview_insights import VERSION, select_insights

    selected_insights = (
        select_insights(
            metrics + extra,
            scope,
            phase.replace("Regular Season", "regular-season")
            .replace("Playoffs", "playoff")
            .replace(" + ", " and "),
            display_policy["profile"],
        )
        if own
        else []
    )
    if selected_insights:
        display_policy["takeaways"] = [
            dict(
                insight_id=f["id"],
                area=f["area"],
                title=f["title"],
                text=f["summary"],
                role="context",
                icon="shield" if f["area"] == "Defensive activity" else "chart-bar",
            )
            for f in selected_insights
        ]
    if len(selected_insights) == 1 and selected_insights[0].get("game_variation"):
        v = selected_insights[0]["game_variation"]
        if (
            scope["comparison_intent"]["requested_group"]
            and not scope["comparison_intent"]["baseline_requested"]
            and not selected_insights[0]["peer_keys"]
        ):
            # Keep the direct unavailable answer in prose; useful game evidence
            # belongs in the sidebar instead of repeating the missing cohort.
            display_policy["takeaways"] = []
        display_policy["takeaways"].append(
            dict(
                insight_id=selected_insights[0]["id"],
                area="Variation",
                title="Game-to-game spread",
                text=v["spread_text"],
                role="context",
                icon="chart-bar",
            )
        )
        if v["peak_text"]:
            display_policy["takeaways"].append(
                dict(
                    insight_id=selected_insights[0]["id"],
                    area="Standout",
                    title="Highest recorded game",
                    text=v["peak_text"],
                    role="context",
                    icon="chart-bar",
                )
            )
    from app.agent.overview_variation import FOCUS_METRICS

    focus_key = FOCUS_METRICS[display_policy["profile"]]
    focal_metric = next(m for m in metrics + extra if m["key"] == focus_key)
    peer = focal_metric.get("peer_comparison", {})
    requested = requested_group(question)
    from app.agent.overview_insights import missing_focus

    missing_requested = missing_focus(metrics + extra, display_policy["profile"])
    fulfilled = not missing_requested and (
        not requested
        or (peer.get("kind") == "position" and peer.get("mode") != "withheld")
    )
    fulfillment = {
        "status": "complete" if fulfilled else "partial",
        "requested_group": requested,
        "metric_key": focus_key,
        "reason": None
        if fulfilled
        else "incomplete_requested_metric"
        if missing_requested
        else peer.get("fallback_reason") or "ineligible_comparison",
    }
    if 0 < len(own) < contract.min_games:
        display_policy["takeaways"] = []
    answer = "\n\n".join(paragraphs)
    return {
        **empty,
        "status": "ok" if own else "no_observations",
        "answer": answer,
        "clarification_options": [],
        "player_profile": identity_profile(player, own),
        "tables": [
            metric_table(
                metrics + extra, display_policy["primary_keys"], "Key metrics"
            ),
            metric_table(
                metrics + extra,
                [
                    m["key"]
                    for m in metrics + extra
                    if m["key"] not in display_policy["primary_keys"]
                ],
                "Additional metrics",
            ),
        ],
        "charts": charts,
        "assumptions": [
            f"Requested dates: {start} through {end}; {phase}. All matching available seasons are combined.",
            f"Comparison: {scope.get('comparison_label', str(scope['previous_start']) + ' through ' + str(scope['previous_end']))}."
            + (
                " Baseline extends outside available archives; changes are unavailable."
                if not baseline_covered
                else ""
            ),
            f"Percentiles compare qualified players in the same dates and phases, with at least {contract.min_games} appearances and complete components. Shooting ranks require 100 FGA for FG%/TS%/eFG%, 50 FTA for FT%, or 50 3PA for 3P%. TOV ranks favor fewer turnovers; MIN describes exposure. Ties share the percentage of other qualified players strictly worse. These provisional project rules are not NBA official qualification.",
            f"Source: {evidence.source}; data through {max(evidence.data_through.values())}. No games or missing values are fabricated.",
            "Charts show "
            + (
                "game-by-game values on recorded game dates"
                if grain == "daily"
                else "monthly per-game averages for months with appearances"
            )
            + "; gaps without appearances are omitted.",
            "Shooting percentages use aggregate makes and attempts; percentage changes are percentage points. TS% uses the 0.44 free-throw approximation. Per-36 rates are descriptive, not causal effects.",
        ],
        "metric_definitions": [
            {
                "key": m[0],
                "label": m[1],
                "definition": "Sum of recorded "
                + m[1].lower()
                + " divided by games with a recorded value",
                "unit": "per game",
            }
            for m in METRICS
        ]
        + [{k: m[k] for k in ("key", "label", "definition", "unit")} for m in extra],
        "semantic_evidence": {
            "presentation": display_policy,
            "insight_version": VERSION,
            "insights": selected_insights,
            "request_fulfillment": fulfillment,
            "snapshot_id": evidence.snapshot_id,
            "summary_paragraphs": paragraphs,
            "summary_context": paragraphs[-1] if len(paragraphs) > 1 else "",
            "game_insights": insights,
            "scope": {
                k: str(v) if isinstance(v, date) else v for k, v in scope.items()
            },
            "metrics": metrics + extra,
            "game_ids": [r["game_id"] for r in own],
            "player_id": player["player_id"],
            "source": evidence.source,
            "data_through": str(max(evidence.data_through.values())),
            "min_games": contract.min_games,
        },
    }
