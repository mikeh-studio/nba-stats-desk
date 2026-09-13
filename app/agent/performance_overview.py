"""Source-backed player overviews sharing one scope across all five metrics."""

from __future__ import annotations

import calendar
import re
from collections import defaultdict
from datetime import date, timedelta

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
    return bool(
        re.search(
            r"\b(performing|performed|performance|overview|stats|statistics)\b",
            original,
            re.I,
        )
    ) and not bool(
        re.search(
            r"\b(points?|rebounds?|assists?|steals?|blocks?|scoring|shooting|fantasy|rank|top|compare|versus|opponent|against)\b|%",
            original,
            re.I,
        )
    )


def shift_months(day, n):
    year, month = divmod(day.year * 12 + day.month - 1 - n, 12)
    if year < 1 or year > 9999:
        raise SemanticError("invalid_scope", "Please choose a shorter month window.")
    month += 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def overview_scope(question, selected_season, today=None):
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
    if re.search(r"\b(regular season|playoffs?|postseason)\b", question, re.I):
        regular = bool(re.search(r"regular season", question, re.I))
        playoffs = bool(re.search(r"playoffs?|postseason", question, re.I))
        phases = (["Regular Season"] if regular else []) + (
            ["Playoffs"] if playoffs else []
        )
    if re.search(
        r"\b(preseason|play.in|summer league|quarter|injur)\w*", question, re.I
    ):
        raise SemanticError(
            "unsupported_coverage",
            "This overview covers regular-season and playoff box scores only.",
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

    def rows_between(lo, hi):
        return [
            r
            for r in evidence.rows
            if r["season_type"] in scope["phases"]
            and lo <= date.fromisoformat(str(r["game_date"])) <= hi
        ]

    current = rows_between(start, end)
    previous = rows_between(scope["previous_start"], scope["previous_end"])
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
            if (
                bounds[0] <= scope["previous_end"]
                and bounds[1] >= scope["previous_start"]
                and (season, phase) not in evidence.covered_scopes
            ):
                baseline_covered = False
    metrics, table, charts, sentences = [], [], [], []
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
        sentences.append(
            f"{label}: {avg} per game"
            + (
                f" ({pct:.0f}th percentile)"
                if pct is not None
                else " (percentile unavailable)"
            )
            + (
                f", {delta:+.1f} versus {scope['previous_start']} through {scope['previous_end']}"
                if delta is not None
                else ""
            )
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
    missing = [m["label"] for m in metrics if m["missing_component_games"]]
    phase = " + ".join(scope["phases"])
    answer = (
        f"{player['player_name']} — {len(own)} recorded appearances from {start} through {end} ({phase}).\n\n"
        + "\n".join("- " + sentence for sentence in sentences)
    )
    if missing:
        answer += (
            "\n\nPartial data for "
            + ", ".join(missing)
            + ": averages use available values; percentiles, changes, and trend charts are withheld. Missing values are not zero."
        )
    available = [m for m in metrics if m["percentile"] is not None]
    if available:
        strongest = max(available, key=lambda m: m["percentile"])
        answer += f"\n\nStrongest relative category: {strongest['label'].lower()} ({strongest['percentile']:.0f}th percentile among qualified players)."
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
            }
        ],
        "charts": charts,
        "assumptions": [
            f"Requested dates: {start} through {end}; {phase}. All matching available seasons are combined.",
            f"Comparison dates: {scope['previous_start']} through {scope['previous_end']}."
            + (
                " Baseline extends outside available archives; changes are unavailable."
                if not baseline_covered
                else ""
            ),
            f"Percentiles compare per-game averages in the same period and phases, with at least {contract.min_games} games and complete data for that metric. Ties share the percentage of other eligible players strictly below them.",
            f"Source: {evidence.source}; data through {max(evidence.data_through.values())}. No games or missing values are fabricated.",
            "Charts show monthly per-game averages for months with appearances; gaps without appearances are omitted.",
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
        ],
        "semantic_evidence": {
            "scope": {
                k: str(v) if isinstance(v, date) else v for k, v in scope.items()
            },
            "metrics": metrics,
            "player_id": player["player_id"],
            "source": evidence.source,
            "data_through": str(max(evidence.data_through.values())),
            "min_games": contract.min_games,
        },
    }
