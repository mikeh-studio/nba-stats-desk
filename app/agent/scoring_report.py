"""Bounded player scoring and game-log requests over ordinary semantic queries."""

import re

from app.agent.game_filters import filter_scope
from app.agent.scope_policy import explicit_scope
from app.agent.semantics import load_contract

_NAME = r"(?P<name>(?!(?:his|her|their|he|she|they|the|same)\b)[^\W\d_][\w'.’\-]*(?:\s+[^\W\d_][\w'.’\-]*){0,5}?)"
_PHASE = (
    r"(?:both )?regular[ -]season and playoffs|regular[ -]season|playoffs?|postseason"
)
_DATE = r"\d{4}-\d{2}-\d{2}"
_POSSESSIVE = r"['’]s?"
_PERIOD = (
    rf"(?:\s+(?:(?:in|during|for) (?:the )?)?(?:20\d{{2}}[-–]\d{{2}}(?: (?:{_PHASE}))?|(?:this|current) season))?"
    rf"(?:\s+(?:(?:from )?{_DATE} (?:through|to) {_DATE}|between {_DATE} and {_DATE}))?"
    rf"(?:\s+(?:\((?:{_PHASE})\)|(?:{_PHASE})))?"
)
_SCORING = (
    rf"where did {_NAME}{_POSSESSIVE} points come from",
    rf"where did {_NAME} get (?:his|her|their) points",
    rf"(?:show (?:me )?|give me |explain )?{_NAME}{_POSSESSIVE} (?:scoring|points) (?:breakdown|composition|mix)",
    rf"(?:break down|split) {_NAME}(?:{_POSSESSIVE})? (?:scoring|points)(?: by (?:scoring )?source)?",
)
_LOG = (
    rf"{_NAME} game[ -]log (?P<metric>[a-z% ]+?)",
    rf"(?:show (?:me )?)?{_NAME}{_POSSESSIVE} (?P<metric>[a-z% ]+?) game[ -]log",
    rf"(?:show (?:me )?|give me )?{_NAME}(?:{_POSSESSIVE})? (?P<metric>[a-z% ]+?) game[ -]by[ -]game",
)
_LOG_FOLLOWUP = re.compile(
    r"(?:show (?:me )?|give me |(?:what|how) about )?(?:(?:his|her|their|the|that|same) )?"
    r"(?:(?P<metric>[a-z% ]+?) )?game[ -](?:log|by[ -]game)[?.!]*",
    re.I,
)
_SCOPE_FIELDS = (
    "season",
    "season_type",
    "as_of",
    "window",
    "n",
    "start_date",
    "seasons",
)
_SOURCES = (("Two-pointers", 2), ("Three-pointers", 3), ("Free throws", 1))


def _request(patterns, text):
    return next(
        (
            match
            for pattern in patterns
            if (match := re.fullmatch(pattern + _PERIOD, text, re.I))
        ),
        None,
    )


def is_scoring_question(question):
    return (
        _request(_SCORING, " ".join(question.strip().rstrip("?.!").split())) is not None
    )


def _game_log_plan(name, metric, scope, *, scoring=False):
    """Keep a full NBA season in the bounded chronological table when possible."""
    definition = load_contract().metric(metric)
    return {
        "status": "query",
        "message": "Scoring sources and game log" if scoring else "Player game log",
        "model_calls": 0,
        "include_scoring_sources": scoring,
        "queries": [
            dict(
                scope,
                player_name=name,
                metric=metric,
                aggregation="ratio" if definition.denominator else "total",
                operation="game_log",
                limit=100,
            )
        ],
    }


def plan_game_performance(question, season, selected_player=None):
    from app.agent.game_performance import parse_game_question
    from app.agent.game_rebounding import resolve_opponent_abbr

    request = parse_game_question(question)
    if request is None:
        return None
    plan = _game_log_plan(
        selected_player or request.player,
        "pts",
        dict(
            request.dates,
            season=season,
            season_type=request.phase,
            **(
                {"opponent_abbr": resolve_opponent_abbr(request.opponent)}
                if request.opponent
                else {}
            ),
        ),
    )
    plan["include_game_performance"] = True
    plan["message"] = "Single-game player performance"
    return plan


def plan_rebounding_game(question, season, selected_player=None):
    from app.agent.game_rebounding import parse_rebounding_game

    request = parse_rebounding_game(question)
    if request is None:
        return None
    plan = _game_log_plan(
        selected_player or request.player,
        "reb",
        dict(
            request.dates,
            season=season,
            season_type=request.phase,
            opponent_abbr=request.opponent_abbr(),
        ),
    )
    plan["include_rebounding_context"] = True
    plan["message"] = "Single-game rebounding"
    if request.stated is not None:
        plan["stated_rebounds"] = request.stated
    return plan


def plan_report(question, selected_season, context, original, selected=None):
    """Match complete, explicable requests; leave other prompts to normal planning."""
    from app.agent.reference_planner import followup_metric_key

    def metric_key(value):
        text = value.casefold()
        return "pts" if text == "scoring" else followup_metric_key(text)

    if selected:
        question = question.partition("\nSelected player: ")[0]
    question = " ".join(question.strip().rstrip("?.!").split())
    text = " ".join((original or question).strip().split())
    prior_match = _LOG_FOLLOWUP.fullmatch(text)
    if (
        prior_match
        and context.get("analysis_type") in {"game_log", "game_performance"}
        and len(context.get("players", [])) == 1
    ):
        requested = prior_match["metric"]
        metric = (
            metric_key(requested)
            if requested
            else next(iter(context.get("metrics", [])), None)
        )
        prior = context.get("scope") or {}
        scope = {key: prior[key] for key in _SCOPE_FIELDS if prior.get(key) is not None}
        if metric and "season" in scope and "season_type" in scope:
            if context.get("analysis_type") == "game_performance":
                # The series uses all opponents through the analyzed game date,
                # retaining the phase and release and excluding later games.
                scope.update(window="season_to_date")
                scope.pop("start_date", None)
                scope.pop("n", None)
                scope.pop("seasons", None)
                plan = _game_log_plan(
                    context["players"][0]["player_name"], metric, scope
                )
                plan["explicit_period_operands"] = True
                plan["all_opponents_through_game"] = True
                plan["message"] = (
                    "Player game log through the analyzed game date, all opponents"
                )
                return plan
            scope.update(filter_scope(prior))
            return _game_log_plan(context["players"][0]["player_name"], metric, scope)

    for patterns, scoring in ((_SCORING, True), (_LOG, False)):
        if match := _request(patterns, question):
            metric = "pts" if scoring else metric_key(match["metric"])
            if metric is None:
                continue
            phase = "Regular Season"
            if re.search(r"\bregular[ -]season and playoffs\b", question, re.I):
                phase = "Both"
            elif re.search(r"\b(?:postseason|playoffs?)\b", question, re.I):
                phase = "Playoffs"
            scope = {
                "season": selected_season,
                "season_type": phase,
                **explicit_scope(question),
            }
            name = re.sub(r"['’]s?$", "", match["name"], flags=re.I)
            return _game_log_plan(selected or name, metric, scope, scoring=scoring)

    return None


def scoring_sources(rows):
    """Reconcile each complete appearance before aggregating its box-score sources."""
    makes = [0, 0, 0]
    for row in rows:
        counts = [row.get(field) for field in ("pts", "fgm", "fg3m", "ftm")]
        if any(
            value is None or not value >= 0 or int(value) != value for value in counts
        ):
            return {"status": "unavailable"}
        points, field_goals, threes, free_throws = map(int, counts)
        twos = field_goals - threes
        if twos < 0 or 2 * twos + 3 * threes + free_throws != points:
            return {"status": "unavailable"}
        makes[0] += twos
        makes[1] += threes
        makes[2] += free_throws
    if not rows:
        return {"status": "unavailable"}
    sources = [
        {"label": label, "makes": made, "points": made * value}
        for (label, value), made in zip(_SOURCES, makes, strict=True)
    ]
    total = sum(source["points"] for source in sources)
    for source in sources:
        source["per_game"] = source["points"] / len(rows)
        source["share"] = source["points"] / total if total else None
    return {
        "status": "ok",
        "metric": {"key": "pts", "unit": "points"},
        "per_game_metric": {"key": "pts", "unit": "points per game"},
        "share_metric": {"key": "scoring_share", "unit": "ratio"},
        "makes_metric": {"key": "scoring_makes", "unit": "makes"},
        "observed_games": len(rows),
        "total": total,
        "rows": sorted(sources, key=lambda source: -source["points"]),
    }


def game_log_context(scope):
    """Human-readable, source-bound scope for the raw response and table."""
    season = ", ".join(scope.get("seasons") or [scope["season"]])
    fields, template = game_log_context_contract(scope)
    return template.format(
        season, scope["season_type"], *(scope[key] for key in fields)
    )


def game_log_context_contract(scope):
    start, end = scope.get("window_start"), scope.get("window_end")
    if scope["window"] == "date_range" and start and end:
        if start == end:
            return ("window_start",), "{0} {1} · {2}"
        return ("window_start", "window_end"), "{0} {1} · {2} through {3}"
    return ("as_of",), "{0} {1} · through {2}"


def scoring_summary(scoring, name):
    if scoring["status"] != "ok":
        return "Scoring sources are unavailable because the required shooting counts are missing or do not reconcile with recorded points."
    total = scoring["total"]
    games = scoring["observed_games"]
    sample = f"{games} recorded {'appearance' if games == 1 else 'appearances'}"
    if total == 0:
        return f"{name} scored zero points across {sample}, so there is no scoring share to calculate."
    top, second, _ = scoring["rows"]
    leaders = [
        row["label"].lower()
        for row in scoring["rows"]
        if row["points"] == top["points"]
    ]
    if len(leaders) > 1:
        labels = (
            " and ".join(leaders)
            if len(leaders) == 2
            else ", ".join(leaders[:-1]) + ", and " + leaders[-1]
        )
        lead = (
            f"{labels.capitalize()} tied for {name}’s largest scoring source at "
            f"{top['points']:,} points each ({top['share']:.1%} apiece)."
        )
    else:
        lead = f"{top['label']} were {name}’s largest source of points ({top['share']:.1%})."
        if second["points"] > 0:
            runner = second["label"].lower()
            difference = top["points"] - second["points"]
            lead += f" They produced {difference:,} more {'point' if difference == 1 else 'points'} than {runner}"
            if top["makes"] < second["makes"]:
                gap = second["makes"] - top["makes"]
                lead += f" on {gap:,} fewer {'make' if gap == 1 else 'makes'}"
            lead += "."
    return f"{lead}\n\n{name} scored {total:,} {'point' if total == 1 else 'points'} across {sample} ({total / games:.1f} per game)."


def log_summary(section, name):
    scoring = section.get("scoring_sources")
    observed, shown = section["observed_games"], section["displayed_games"]
    unit = section["metric"]["label"]
    if not shown:
        return f"No {name} appearances matched the requested scope."
    if scoring:
        return scoring_summary(scoring, name)
    if context := section.get("rebounding_context"):
        game = section["rows"][0]
        if game["display_value"] is not None:
            total = game["display_value"]
            answer = f"{name} had {total:g} rebounds against {game['opponent_abbr']} on {game['game_date']}."
            if context.get("premise_differs"):
                answer = "The box score records a different total. " + answer
            if split := context["split"]:
                answer += f" The split was {split['offensive']} offensive and {split['defensive']} defensive."
            if previous := context["previous"]:
                gap = total - previous["average"]
                games = previous["appearances"]
                phase = (
                    "playoff" if game["season_type"] == "Playoffs" else "regular-season"
                )
                answer += (
                    f" He averaged {previous['average']:.1f} rebounds across his "
                    f"{games} earlier {phase} {'appearance' if games == 1 else 'appearances'} in this dataset."
                )
                if abs(gap) >= 0.05:
                    answer += f" This game was {abs(gap):.1f} rebounds {'above' if gap > 0 else 'below'} that average."
                if games >= 5:
                    answer += (
                        f" He had at least {total:g} rebounds in {previous['at_least_current']} "
                        f"of those {games} previous appearances."
                    )
            return answer
    sample = (
        f"{shown} of {observed} recorded appearances"
        if shown < observed
        else (
            "one recorded appearance" if shown == 1 else f"{shown} recorded appearances"
        )
    )
    lines = [f"{name} · {unit} across {sample}."]
    rows = [row for row in section["rows"] if row["display_value"] is not None]
    if rows:
        if shown > 1 and len(rows) == shown and section["metric"]["key"] == "pts":
            mean = sum(row["display_value"] for row in rows) / shown
            lines[:] = [f"{name} averaged {mean:.1f} points per game over {sample}."]
        high = max(row["display_value"] for row in rows)
        best = [row for row in rows if row["display_value"] == high]
        pct = "%" if section["metric"]["unit"] == "ratio" else ""
        value = f"{high:g}{pct}"
        if shown == 1 and section["metric"]["key"] == "pts":
            row = best[0]
            lines.append(f"{value} points on {row['game_date']}.")
        elif len(best) == 1:
            row = best[0]
            opponent = (
                f" against {row['opponent_abbr']}" if row["opponent_abbr"] else ""
            )
            qualifier = "Highest shown" if len(rows) < observed else "Game high"
            if section["metric"]["key"] == "pts":
                value += " points"
            lines.append(f"{qualifier}: {value}{opponent} on {row['game_date']}.")
        elif len(rows) > 1:
            lines.append(
                f"The highest shown value was {value}, reached in {len(best)} games."
            )
    return " ".join(lines)


def add_scoring_evidence(payload, sources):
    rows = sources["rows"]
    payload["tables"].append(
        {
            "title": "Scoring sources",
            "columns": [
                {"key": "source", "label": "Source"},
                {"key": "points", "label": "Points"},
                {"key": "per_game", "label": "Points per game"},
                {"key": "share", "label": "Share of points"},
                {"key": "makes", "label": "Makes"},
            ],
            "rows": [
                [
                    row["label"],
                    row["points"],
                    f"{row['per_game']:.1f}",
                    "Unavailable" if row["share"] is None else f"{row['share']:.1%}",
                    row["makes"],
                ]
                for row in rows
            ],
        }
    )
    payload["charts"].append(
        {
            "type": "bar",
            "title": "Points by scoring source",
            "y_label": "Total points",
            "series": [
                {
                    "key": "pts",
                    "points": [{"x": row["label"], "y": row["points"]} for row in rows],
                }
            ],
        }
    )
