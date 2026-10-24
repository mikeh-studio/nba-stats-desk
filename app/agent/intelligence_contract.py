"""Versioned intelligence boundary; the public host validates every returned value.

Frozen records describe data, not a sandbox. Discovery, validation and transport
live in intelligence.py; execution remains in plan_execution.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, TypeAlias

API_VERSION = "nba_intelligence/1"
ENTRY_POINT_GROUP = "nba_stats_desk.intelligence.v1"

Phase: TypeAlias = Literal["Regular Season", "Playoffs", "Both"]
Operation: TypeAlias = Literal["summary", "rank", "percentile", "game_log"]
Aggregation: TypeAlias = Literal["average", "total", "ratio", "unspecified"]
Window: TypeAlias = Literal[
    "season_to_date",
    "last_n_games",
    "prior_n_games",
    "last_n_days",
    "last_n_months",
    "last_week",
    "last_month",
    "date_range",
]
Reason: TypeAlias = Literal[
    "missing_identity",
    "ambiguous_identity",
    "missing_metric",
    "ambiguous_scope",
    "missing_qualification",
    "unsupported_scope",
    "unsupported_metric",
    "unsupported_operation",
]
# Providers may return only these; the host treats any other code as invalid output.
FailureCode: TypeAlias = Literal["provider_unavailable", "invalid_provider_output"]
# Assigned by the host from its deadline/accounting or gateway exceptions.
HostFailureCode: TypeAlias = Literal["provider_timeout", "budget_exhausted"]


@dataclass(frozen=True, slots=True)
class Scope:
    season: str
    season_type: Phase
    window: Window = "season_to_date"
    n: int | None = None
    start_date: str | None = None
    as_of: str | None = None
    team_abbr: str | None = None
    opponent_abbr: str | None = None
    home_away: Literal["HOME", "AWAY"] | None = None


@dataclass(frozen=True, slots=True)
class QueryProposal:
    """Scope proposal only; player_name must pass the public identity resolver."""

    metric: str
    scope: Scope
    operation: Operation
    aggregation: Aggregation
    player_name: str | None = None
    min_games: int | None = None
    min_attempts: int | None = None
    direction: Literal["higher", "lower"] | None = None
    limit: int = 10
    aggregation_source: Literal[
        "explicit", "inherited", "planner_selected", "omitted"
    ] = "omitted"


@dataclass(frozen=True, slots=True)
class MetricCapability:
    key: str
    aggregations: tuple[Aggregation, ...]
    operations: tuple[Operation, ...]
    requires_attempt_threshold: bool


@dataclass(frozen=True, slots=True)
class RouteCapability:
    route_id: str
    plan_kind: Literal["queries", "handler"]


@dataclass(frozen=True, slots=True)
class PriorAnalysis:
    """Host-selected successful scope only; never answer text or statistical proof."""

    route_id: str
    scopes: tuple[Scope, ...]
    player_names: tuple[str, ...]
    metric_keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PlanningContext:
    request_id: str
    question: str
    selected_season: str
    today: str
    semantic_contract_version: str
    supported_seasons: tuple[str, ...]
    metrics: tuple[MetricCapability, ...]
    routes: tuple[RouteCapability, ...]
    player_names: tuple[str, ...]
    team_abbreviations: tuple[str, ...]
    prior: PriorAnalysis | None = None


@dataclass(frozen=True, slots=True)
class IntentSelection:
    """Advisory choice from context.routes, not permission to dispatch a handler.

    The host rechecks eligibility; a handler route dispatches without plan().
    """

    route_id: str
    continuation: bool


@dataclass(frozen=True, slots=True)
class Withheld:
    status: Literal["clarification_required", "unsupported"]
    reason: Reason


@dataclass(frozen=True, slots=True)
class ProviderFailure:
    code: FailureCode


@dataclass(frozen=True, slots=True)
class QueryPlan:
    """Proposal: structural checks precede narrow public normalization.

    Final compare: same publicly resolved identity, metric and aggregation;
    current side first. Supported season-to-season scopes remain distinct.
    """

    route_id: str
    status: Literal["query", "compare"]
    queries: tuple[QueryProposal, ...]
    qualification: Literal["explicit_or_default", "league_leader"] = (
        "explicit_or_default"
    )


@dataclass(frozen=True, slots=True)
class HandlerPlan:
    """Select an existing public handler; never provide callable/tool arguments."""

    route_id: str


IntentResult: TypeAlias = IntentSelection | Withheld | ProviderFailure
PlanningResult: TypeAlias = QueryPlan | HandlerPlan | Withheld | ProviderFailure


@dataclass(frozen=True, slots=True)
class PromptBundle:
    revision: str
    planning_system: str | None
    answer_system: str | None


@dataclass(frozen=True, slots=True)
class ProviderDescriptor:
    provider_id: str
    provider_version: str
    api_version: str
    semantic_contract_versions: tuple[str, ...]
    route_ids: tuple[str, ...]
    requires_model: bool


@dataclass(frozen=True, slots=True)
class PlanningBudget:
    deadline_monotonic: float
    max_model_calls: int
    max_output_tokens: int


class GatewayError(Exception):
    """Raised by the host gateway only; providers let it propagate."""


class GatewayTimeout(GatewayError):
    pass


class GatewayBudgetExhausted(GatewayError):
    pass


class GatewayUnavailable(GatewayError):
    pass


class PlanningModelGateway(Protocol):
    """Host-owned transport; fixed plan schema, model selection and budget."""

    async def generate_plan(self, *, system_prompt: str, user_payload: str) -> str:
        """Return bounded JSON text or raise a GatewayError subclass.

        The host accounts for usage, timeout and retries.
        """
        ...


class IntelligenceProvider(Protocol):
    descriptor: ProviderDescriptor

    def prompts(self) -> PromptBundle:
        """Return inert text without I/O; reference returns two None values."""
        ...

    def classify(self, context: PlanningContext) -> IntentResult:
        """Bounded, deterministic, no I/O; identify intent and propose a route."""
        ...

    async def plan(
        self,
        context: PlanningContext,
        intent: IntentSelection,
        *,
        prompts: PromptBundle,
        budget: PlanningBudget,
        model: PlanningModelGateway | None,
    ) -> PlanningResult:
        """Propose a plan for a queries route; None model for reference mode."""
        ...


class ProviderFactory(Protocol):
    def __call__(self) -> IntelligenceProvider:
        """Construct without I/O; no credentials, repository or tools supplied."""
        ...
