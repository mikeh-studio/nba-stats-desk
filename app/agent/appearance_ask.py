"""Bounded appearance-count follow-ups with explicit identity and source attribution."""

import re

from app.agent.availability_ask import _identities
from app.agent.followup import analysis_context, resolve_followup
from app.agent.research_ask import refusal
from app.agent.semantic_serving import requested_seasons, source_players
from app.agent.semantics import Query, SemanticError, run_query
from app.availability import load_availability


def wants_appearances(question):
    return bool(
        re.search(
            r"\bhow many (?:games|appearances)\b|^(?:show|tell me|what (?:is|are))\s+(?:the\s+)?(?:number of games|games played|appearance counts?)\b",
            question,
            re.I,
        )
    )


def answer_appearances(agent, question, context, conversation_id, trace=None):
    if trace:
        trace.route = "appearances"
    try:
        resolved, inherited = resolve_followup(question, context)
        prior_season = (
            context.get("scope", {}).get("season")
            or context.get("availability_scope", {}).get("season")
            or agent.settings.season
        )
        # Explicit relative periods refer to the selected season, not an old chat.
        default_season = (
            agent.settings.season
            if re.search(
                r"\b(?:this|current|last|previous|prior) season\b", question, re.I
            )
            else prior_season
        )
        seasons = inherited.get("seasons") or requested_seasons(
            resolved, default_season
        )
        if len(seasons) != 1:
            raise SemanticError(
                "unsupported_scope", "Choose one season for games played."
            )
        # Use the same full player-game source as the availability analysis, not
        # its filtered groups. Ordinary chats use the governed warehouse source.
        if context.get("availability_scope"):
            _, evidence, *_ = load_availability(
                agent.settings.research_availability_path
            )
        else:
            _, evidence = agent.semantic_agent.warehouse.load(seasons)
        evidence.validate()
        players = source_players(evidence)
        for prior in context.get("players", []):
            if prior["player_name"].casefold() not in resolved.casefold():
                continue
            if not any(
                p["player_id"] == prior["player_id"]
                and p["player_name"] == prior["player_name"]
                for p in players
            ):
                raise SemanticError(
                    "invalid_context",
                    "Saved player identities do not match the selected source.",
                )
        if re.search(r"\bplayer\d+\b", resolved, re.I):
            raise SemanticError(
                "invalid_context", "Use player names rather than internal tokens."
            )
        text, named = _identities(resolved, players)
        identity_order = {pid: text.find(f"player{pid}") for pid in named}
        if not named or len(named) > 2:
            raise SemanticError(
                "clarification_required",
                "Which one or two players should I count appearances for?",
            )
        regular = bool(re.search(r"regular[ -]season", text))
        playoffs = bool(re.search(r"playoffs?|postseason", text))
        phase = (
            "Both"
            if regular and playoffs
            else "Playoffs"
            if playoffs
            else "Regular Season"
        )
        start, end = None, None
        dates = re.search(
            r"from (\d{4}-\d{2}-\d{2}) (?:through|to) (\d{4}-\d{2}-\d{2})", text
        )
        if dates:
            start, end = dates.groups()
            text = text[: dates.start()] + " " + text[dates.end() :]
        text = re.sub(
            r"player\d+|20\d{2}[-–/]\d{2}|regular[ -]season|playoffs?|postseason",
            " ",
            text,
        )
        # Consume all words, so extra predicates cannot silently disappear.
        text = re.sub(
            r"\b(?:how|many|games?|appearances?|do|does|did|has|have|each|player|players|played|play|this|last|previous|prior|current|season|in|the|and|both|please|tell|me|show|total|number|of)\b",
            " ",
            text,
        )
        if text.strip(" .?!,():;"):
            raise SemanticError(
                "unsupported_scope",
                "I can count each player's appearances for a season, phase, or explicit date range. For an earlier comparison's games, specify which group to count.",
            )
        results = []
        identities = []
        for player in named.values():
            query = Query(
                metric="gp",
                season=seasons[0],
                aggregation="total",
                season_type=phase,
                player_id=player["player_id"],
                window="date_range" if start else "season_to_date",
                start_date=start,
                as_of=end,
            )
            result = run_query(evidence, query)
            results.append(result)
            identities.append({k: player[k] for k in ("player_id", "player_name")})
        # Explicit order in the resolved question, independent of source row order.
        pairs = sorted(
            zip(identities, results),
            key=lambda pair: identity_order[pair[0]["player_id"]],
        )
        identities, results = map(list, zip(*pairs))
        rows = []
        for player, result in zip(identities, results):
            row = next(
                (r for r in result["rows"] if r["player_id"] == player["player_id"]),
                None,
            )
            if row is None:
                raise SemanticError(
                    "unavailable",
                    f"No verified appearances for {player['player_name']} in that scope; no zero was assumed.",
                )
            rows.append([player["player_name"], row["observed_games"]])
        scope = results[0]["scope"]
        phase_label = (
            "regular season and playoffs" if phase == "Both" else phase.lower()
        )
        next_context = dict(
            version=1,
            analysis_type="appearances",
            players=identities,
            scope=scope,
            metrics=["gp"],
        )
        period = (
            f"from {start} through {scope['as_of']}"
            if start
            else f"through {scope['as_of']}"
        )
        explanation = (
            "These are each player's recorded appearances in this period, across teams."
        )
        if context.get("availability_scope"):
            explanation += " The prior teammate-availability filter is not applied."
        payload = dict(
            status="ok",
            answer="; ".join(f"{name}: {count} games played" for name, count in rows)
            + f". {seasons[0]} · {phase_label} · {period}. {explanation}",
            tables=[
                dict(
                    title="Games played",
                    columns=[
                        dict(key="player", label="Player"),
                        dict(key="gp", label="Games played"),
                    ],
                    rows=rows,
                )
            ],
            charts=[],
            assumptions=[
                "Counts use unique season/game/player appearances. Missing box-score components do not remove a recorded appearance.",
                f"Source: {evidence.source}; snapshot: {evidence.snapshot_id}.",
            ],
            metric_definitions=[
                dict(
                    key="gp",
                    label="Games played",
                    definition="Recorded player-game appearances within the stated season, phase and dates.",
                )
            ],
            followups=[],
            tool_calls=[],
            clarification_options=[],
            conversation_id=conversation_id,
            conversation_context=next_context,
            appearance_evidence=results,
        )
        if conversation_id and agent.conversation_store:
            agent.conversation_store.clear_pending_clarification(conversation_id)
            agent.conversation_store.append_turn(
                conversation_id,
                question=question,
                answer=payload["answer"],
                context=analysis_context(question, payload),
                max_turns=agent.settings.agent_conversation_max_turns,
            )
        if trace:
            trace.outcome = "answered"
        return payload
    except SemanticError as exc:
        return dict(
            refusal(str(exc), code=exc.code, trace=trace),
            conversation_id=conversation_id,
        )
    except (OSError, ValueError, KeyError, TypeError):
        return dict(
            refusal(
                "Games-played evidence could not be validated. No counts were substituted.",
                code="invalid_appearance_evidence",
                trace=trace,
            ),
            conversation_id=conversation_id,
        )
