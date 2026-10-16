"""Ordered Ask routes. First match wins; registry metadata is test-enumerable.

Semantic subroutes use the existing evidence handlers. Compatibility is explicit
and is only selected for repositories without a governed warehouse adapter.
Reference questions require governed evidence even for compatibility repositories;
other legacy tool workflows remain available.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cached_property
from typing import Any, Callable

from app.agent.appearance_ask import wants_appearances
from app.agent.availability_ask import availability_followup, wants_availability
from app.agent.award_lookup import wants_award
from app.agent.execution import timed
from app.agent.performance_overview import wants_overview
from app.agent.player_comparison import comparison_sides
from app.agent.question_intent import (
    round_scope_message,
    split_kind,
    starts_new_question,
)
from app.agent.reference_ask import reference_followup, reference_kind
from app.agent.research_ask import (
    mentioned_pairs,
    wants_research,
    wants_research_followup,
)
from app.agent.teammate_ask import legacy_study_has_replacement, wants_study


@dataclass(frozen=True)
class RouteContext:
    question: str
    routing_question: str
    context: dict[str, Any]
    recent_context: dict[str, Any]
    settings: Any
    governed: bool
    reference_reply: str | None = None

    @property
    def research_followup(self):
        return bool(
            self.recent_context.get("research_scope")
            and wants_research_followup(self.question)
        )

    @property
    def study_followup(self):
        return bool(
            self.recent_context.get("teammate_study")
            and re.search(
                r"^(?:is|was|does|did|what|how).*(?:significan|caus|uncertain|confidence|p-value|sample)",
                self.question,
                re.I,
            )
        )

    @property
    def study_requested(self):
        return (
            wants_study(self.question, self.settings.agent_teammate_study_path)
            or self.study_followup
        )

    @property
    def preserve_study(self):
        return (
            bool(self.settings.agent_teammate_study_path)
            and self.study_requested
            and not self.research_followup
            and not legacy_study_has_replacement(
                self.settings.agent_teammate_study_path,
                self.settings.research_studies_path,
            )
        )

    @cached_property
    def reference_followup_question(self):
        return reference_followup(self.question, self.context)

    @cached_property
    def reference_question(self):
        if self.reference_reply is not None:
            return self.routing_question + " " + self.reference_reply
        return self.reference_followup_question or self.routing_question

    @cached_property
    def reference(self):
        return reference_kind(self.reference_question)


def availability(c):
    return bool(
        c.context.get("availability_scope") and availability_followup(c.question)
    ) or (
        wants_availability(c.question)
        and not (
            c.context.get("research_scope") and wants_research_followup(c.question)
        )
        and (
            bool(c.settings.research_availability_path)
            or (
                not mentioned_pairs(c.question)
                and not wants_study(c.question, c.settings.agent_teammate_study_path)
            )
        )
    )


@dataclass(frozen=True)
class Route:
    key: str
    handler: str
    matches: Callable[[RouteContext], bool]
    evidence: str
    capability: str


ROUTES = (
    Route(
        "unsupported_round",
        "scope_refusal",
        lambda c: not wants_award(c.question) and bool(round_scope_message(c.question)),
        "none",
        "Reject unavailable playoff round scope",
    ),
    Route(
        "appearances",
        "appearances",
        lambda c: bool(
            wants_appearances(c.question)
            and (c.governed or c.context.get("availability_scope"))
        ),
        "appearance evidence",
        "Recorded appearances and scoped games played",
    ),
    Route(
        "availability",
        "availability",
        availability,
        "availability/2",
        "Verified teammate participation comparisons",
    ),
    Route(
        "research",
        "research",
        lambda c: bool(
            (wants_research(c.question) or c.research_followup)
            and not c.preserve_study
            and not split_kind(c.question)
            and (not c.reference or wants_availability(c.question))
        ),
        "research study",
        "Published studies and descriptive breakdowns",
    ),
    Route(
        "teammate_study",
        "teammate_study",
        lambda c: bool(c.study_requested),
        "frozen teammate study",
        "Compatibility for unreplaced published studies",
    ),
    Route(
        "similarity",
        "reference",
        lambda c: c.reference == "similarity",
        "similarity_reference/1 or recent_statistical_similarity/1",
        "Published season profiles or complete recent statistical comparisons",
    ),
    Route(
        "league_baseline",
        "reference",
        lambda c: c.reference == "league_baseline",
        "league_baseline/2",
        "Pooled same-calendar-scope league reference",
    ),
    Route(
        "award",
        "semantic",
        lambda c: c.governed and wants_award(c.routing_question),
        "reviewed award catalog",
        "Award identity and scoped performance composition",
    ),
    Route(
        "player_split",
        "semantic",
        lambda c: c.governed and bool(split_kind(c.routing_question)),
        "player_splits/1",
        "Phase, venue, recent/prior and event splits",
    ),
    Route(
        "player_comparison",
        "semantic",
        lambda c: c.governed and bool(comparison_sides(c.routing_question)),
        "governed scorecard",
        "Two-player shared-scope scorecards",
    ),
    Route(
        "overview",
        "semantic",
        lambda c: c.governed and wants_overview(c.routing_question),
        "governed overview",
        "Broad player performance summaries",
    ),
    Route(
        "metrics",
        "semantic",
        lambda c: c.governed,
        "nba_semantics/0.1",
        "Validated metric plans and deterministic rendering",
    ),
    Route(
        "legacy_repository",
        "legacy_repository",
        lambda c: not c.governed,
        "legacy tool bundle",
        "Non-warehouse repository compatibility; never a failed governed-query fallback",
    ),
)


def pending_routing(
    question: str,
    pending_question: str | None,
    selected_player: Any = None,
    typed_overview: bool = False,
) -> tuple[str, str | None, bool]:
    """Return the routing question, reference reply and whether pending survives.

    A pending reference clarification survives only a selection or a short name
    reply. Any other untyped pending clarification is dropped by a self-contained
    new question, which routes on its own wording. Typed Overview clarifications
    keep their own restatement contract (``resume_overview``). Otherwise the
    pending question claims routing unless the new turn is a reference or award
    request itself.
    """
    if pending_question is None:
        return question, None, False
    reference_reply = None
    if reference_kind(pending_question):
        from app.agent.service import _clarify_reply_name

        reply = _clarify_reply_name(question)
        new_question = bool(
            reference_kind(question)
            or wants_award(question)
            or re.search(
                r"\b(?:how|what|who|which|show|compare|rank|when|without|out|sits|games|per)\b",
                question,
                re.I,
            )
        )
        if selected_player:
            reference_reply = str(selected_player.get("player_name", ""))
        elif reply and not new_question:
            reference_reply = reply
        else:
            return question, None, False
    elif not selected_player and not typed_overview and starts_new_question(question):
        return question, None, False
    if reference_kind(question) or wants_award(question):
        return question, reference_reply, True
    return pending_question, reference_reply, True


@timed("routing")
def select_route(context: RouteContext) -> Route:
    return next(route for route in ROUTES if route.matches(context))


def dispatch_source(context: RouteContext, route: Route) -> str:
    """Name what claimed the route: a pending clarification, a prior turn, or cues."""
    if context.reference_reply is not None or (
        context.routing_question != context.question
    ):
        return "pending_clarification"

    def reference() -> bool:
        return bool(context.reference_followup_question) and not reference_kind(
            context.question
        )

    sticky: dict[str, Callable[[], bool]] = {
        "research": lambda: (
            context.research_followup and not wants_research(context.question)
        ),
        "teammate_study": lambda: (
            context.study_followup
            and not wants_study(
                context.question, context.settings.agent_teammate_study_path
            )
        ),
        "availability": lambda: (
            bool(context.context.get("availability_scope"))
            and availability_followup(context.question)
            and not wants_availability(context.question)
        ),
        "similarity": reference,
        "league_baseline": reference,
    }
    claimed = sticky.get(route.key)
    return "prior_route" if claimed and claimed() else "deterministic"


def semantic_shape(question: str) -> str:
    """Use the same ordered predicates after conversational scope is resolved."""
    # Only these predicates need question/warehouse state; no new routing chain.
    from types import SimpleNamespace

    context = RouteContext(question, question, {}, {}, SimpleNamespace(), True)
    return next(
        route.key
        for route in ROUTES
        if route.key in {"player_split", "player_comparison", "overview", "metrics"}
        and route.matches(context)
    )
