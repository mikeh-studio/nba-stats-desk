"""Compatibility exports for callers migrating to the public intelligence host.

Production planning and prompts live in the optional private package. Execution
has exactly one owner, app.agent.plan_execution. New app code imports that owner.
"""

from app.agent.plan_execution import execute_plan as execute_plan
from app.agent.plan_execution import leader_game_floor as leader_game_floor
from app.agent.plan_execution import (
    normalize_duplicate_rank_plan as normalize_duplicate_rank_plan,
)
from app.agent.plan_execution import plan_schema as plan_schema
from app.agent.plan_execution import response_schema as response_schema
from app.agent.plan_execution import single_period_rank_plan as single_period_rank_plan
from app.agent.reference_planner import league_leader_plan as league_leader_plan
from app.agent.scope_policy import explicit_scope as explicit_scope


def plan_question(client, *, settings=None, **kwargs):
    from app.agent.intelligence import plan_question as propose
    from app.config import get_settings

    return propose(client, settings=settings or get_settings(), **kwargs)
