"""Recover clarification hints from the pending request, never an older answer."""

import re

from app.agent.overview_clarification import OverviewClarification
from app.agent.overview_positions import BASELINE_CLAUSE, strip_peer_clause
from app.agent.performance_overview import overview_scope, resolve_overview_player
from app.agent.semantic_serving import has_time_scope, seasons_in_dates, source_players
from app.agent.semantics import SemanticError


def pending_overview_context(reply, pending, season, warehouse):
    needs_player = bool(re.search(r"\b(?:his|him|he|their|them|they)\b", reply, re.I))
    needs_dates = not has_time_scope(reply)
    needs_phase = not re.search(
        r"\b(?:regular[ -]season|playoffs?|postseason)\b", reply, re.I
    )
    if not (needs_player or needs_dates or needs_phase):
        return {}
    try:
        current = strip_peer_clause(BASELINE_CLAUSE.sub("", pending.question))
        scope = overview_scope(current, season, comparison={})
        if needs_dates and (scope["first_appearances"] or scope["recent_appearances"]):
            raise SemanticError(
                "clarification_required", "Restate the appearance window."
            )
        hints = {"phases": scope["phases"]} if needs_phase else {}
        if needs_dates:
            hints.update(start=scope["start"].isoformat(), end=scope["end"].isoformat())
        players = []
        if needs_player:
            _, evidence = warehouse.load(
                seasons_in_dates(scope["start"].isoformat(), scope["end"].isoformat())
            )
            evidence.validate()
            players = resolve_overview_player(
                pending.question, source_players(evidence), evidence.rows
            )
            if len(players) != 1:
                raise SemanticError("clarification_required", "Specify one player.")
            # The shared resolver uses singular pronouns for one verified player.
            if re.search(r"\b(?:their|them|they)\b", reply, re.I):
                raise SemanticError("clarification_required", "Specify one player.")
        return {"players": players, "scope": hints}
    except SemanticError as exc:
        raise OverviewClarification(
            pending.query_plan["overview_clarification"],
            "Please restate the question with one player's name and the current season or date range.",
        ) from exc
