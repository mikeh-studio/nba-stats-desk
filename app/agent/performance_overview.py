"""Source-backed player overviews sharing one scope across all five metrics."""

from __future__ import annotations

import calendar
import re
from collections import defaultdict
from datetime import date, timedelta

from app.agent.context_metrics import context_metrics, context_table
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
    original = question.split("\nClarification:", 1)[0]
    return (
        bool(
            re.search(
                r"\b(perform(?:ing|ed|ance)?|overview|stats|statistics)\b"
                r"|\bhow\b[^?\n]*\b(?:play(?:ed|ing)?|do(?:ing)?|done|fare[ds]?)\b",
                original,
                re.I,
            )
        )
        and not has_specific_metric(original)
        and not bool(
            re.search(
                r"\b(fantasy|rank|top|compare|versus|opponent|against|home|away|road)\b",
                original,
                re.I,
            )
        )
    )


def shift_months(day, n):
    year, month = divmod(day.year * 12 + day.month - 1 - n, 12)
    if year < 1 or year > 9999:
        raise SemanticError("invalid_scope", "Please choose a shorter month window.")
    month += 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def overview_scope(question, selected_season, today=None):
    from app.agent.question_intent import round_scope_message

    if message := round_scope_message(question):
        raise SemanticError("unsupported_scope", message)
    question = re.sub(
        r"\b(20\d{2})\s+(playoffs?|postseason)\b",
        lambda m: f"{int(m[1]) - 1}-{m[1][-2:]} {m[2]}",
        question,
        flags=re.I,
    )
    today = today or date.today()
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
        if re.search(
            r"\b(last|past|prior|previous|since|as.of|yesterday)\b", question, re.I
        ):
            raise SemanticError(
                "clarification_required",
                "Please specify a date range (from YYYY-MM-DD through YYYY-MM-DD) or a number of days, weeks, or months.",
            )
        named = re.findall(r"\b20\d{2}-\d{2}\b", question)
        if len(set(named)) > 1:
            raise SemanticError(
                "clarification_required",
                "For a combined overview, please specify the start and end dates.",
            )
        start, end = season_bounds(named[0] if named else selected_season)
        end = min(end, today)
    if end < start or end > today:
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
    previous_phases = list(phases)
    if not trailing and not explicit:
        if phases == ["Playoffs"]:
            # A full playoff overview compares against this season's regular season.
            previous_start, previous_end = start, end
            previous_phases = ["Regular Season"]
        else:
            previous_start = shift_months(start, 12)
            previous_end = shift_months(end, 12)
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
        start=start,
        end=end,
        previous_start=previous_start,
        previous_end=previous_end,
        phases=phases,
        previous_phases=previous_phases,
        comparison_label=comparison_label,
        seasons=seasons,
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
    metrics, table, charts = [], [], []
    grouped = defaultdict(list)
    for row in current:
        grouped[row["player_id"]].append(row)
    for key, label in METRICS:
        metric = contract.metric(key)
        value = aggregate(own, metric, "average")
        baseline = aggregate(prior, metric, "average")
        cohort = []
        for rows in grouped.values():
            stats = aggregate(rows, metric, "average")
            if (
                stats["valid_games"] >= contract.min_games
                and not stats["missing_component_games"]
            ):
                cohort.append(stats["value"])
        eligible = (
            value["valid_games"] >= contract.min_games
            and not value["missing_component_games"]
        )
        pct = (
            100 * sum(v < value["value"] for v in cohort) / (len(cohort) - 1)
            if eligible and len(cohort) > 1
            else None
        )
        delta = (
            value["value"] - baseline["value"]
            if baseline_covered
            and value["value"] is not None
            and baseline["value"] is not None
            and not value["missing_component_games"]
            and not baseline["missing_component_games"]
            else None
        )
        avg = f"{value['value']:.1f}" if value["value"] is not None else "Unavailable"
        table.append(
            [
                label,
                avg,
                f"{pct:.0f}" if pct is not None else "Unavailable",
                f"{delta:+.1f}" if delta is not None else "Unavailable",
                f"{value['valid_games']} / {len(own)}",
                str(len(cohort)),
            ]
        )
        buckets = defaultdict(list)
        for row in own:
            buckets[str(row["game_date"])[:7]].append(row)
        points = []
        for month, rows in sorted(buckets.items()):
            stats = aggregate(rows, metric, "average")
            if stats["value"] is not None:
                points.append(
                    {
                        "x": month,
                        "y": stats["value"],
                        "meta": f"{stats['valid_games']} / {len(rows)} games with {label.lower()} data",
                    }
                )
        # Do not draw a continuous trend across missing metric observations.
        if points and not value["missing_component_games"]:
            charts.append(
                {
                    "type": "line",
                    "title": f"{label} per game — monthly",
                    "x_label": "Month with appearances",
                    "y_label": label + " per game",
                    "series": [{"key": key, "label": label, "points": points}],
                }
            )
        metrics.append(
            {
                "key": key,
                "label": label,
                **value,
                "percentile": pct,
                "cohort_size": len(cohort),
                "previous": baseline,
                "change": delta,
                "monthly": points,
            }
        )
    phase = " + ".join(scope["phases"])
    extra = context_metrics(own, prior, baseline_covered=baseline_covered)
    insights = game_insights(own)
    paragraphs = summary_paragraphs(
        player["player_name"], metrics + extra, scope, len(own), insights
    )
    answer = "\n\n".join(paragraphs)
    return {
        **empty,
        "status": "ok" if own else "no_observations",
        "answer": answer,
        "clarification_options": [],
        "player_profile": identity_profile(player, own),
        "tables": [
            {
                "title": "Performance overview — per-game averages",
                "columns": [
                    {"key": k, "label": v}
                    for k, v in (
                        ("metric", "Stat"),
                        ("average", "Per game"),
                        ("percentile", "League percentile"),
                        ("change", "Change vs comparison dates"),
                        ("games", "Valid / played games"),
                        ("cohort", "Qualified players"),
                    )
                ],
                "rows": table,
            },
            context_table(extra),
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
            f"Percentiles compare per-game averages in the same period and phases, with at least {contract.min_games} games and complete data for that metric. Ties share the percentage of other eligible players strictly below them.",
            f"Source: {evidence.source}; data through {max(evidence.data_through.values())}. No games or missing values are fabricated.",
            "Charts show monthly per-game averages for months with appearances; gaps without appearances are omitted.",
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
            "summary_paragraphs": paragraphs,
            "game_insights": insights,
            "scope": {
                k: str(v) if isinstance(v, date) else v for k, v in scope.items()
            },
            "metrics": metrics + extra,
            "player_id": player["player_id"],
            "source": evidence.source,
            "data_through": str(max(evidence.data_through.values())),
            "min_games": contract.min_games,
        },
    }
