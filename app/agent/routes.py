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
from app.agent.performance_overview import wants_overview
from app.agent.player_comparison import comparison_sides
from app.agent.question_intent import round_scope_message, split_kind
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
        "similarity_reference/1",
        "Published season neighbors only",
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


def select_route(context: RouteContext) -> Route:
    return next(route for route in ROUTES if route.matches(context))


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
