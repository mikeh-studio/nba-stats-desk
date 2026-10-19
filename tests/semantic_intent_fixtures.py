"""Explicit migration of pre-v2 controlled semantic test plans (never production)."""

import copy
import json
import re

from app.agent.planner import explicit_metric_mentions
from app.agent.question_intent import explicit_aggregation
from app.agent.semantic_serving import has_time_scope
from app.agent.semantics import load_contract


def migrate_legacy_plan(plan, request):
    """Annotate authored values, without correcting or fabricating aggregations."""
    plan = copy.deepcopy(plan)
    content = json.loads(request["input"][0]["content"])
    question = content.get("current_question", content["question"])
    context = content.get("conversation_context", {})
    prior_scope = context.get("scope") or {}
    prior = prior_scope.get("aggregation") or context.get("aggregation")
    prior_metric = prior_scope.get("metric") or next(
        iter(context.get("metrics", [])), None
    )
    if prior_metric == "gp" or prior_scope.get("operation") == "game_log":
        prior = None
    complete = bool(
        re.search(r"\bresolved_player_\d+\b", question)
        and has_time_scope(question)
        and explicit_metric_mentions(question)
    )
    cue = explicit_aggregation(question)
    for query in plan.get("queries", []):
        metric = load_contract().metrics.get(query.get("metric", ""))
        fixed = bool(
            query.get("metric") == "gp"
            or query.get("operation") == "game_log"
            or (metric and metric.denominator)
        )
        convention_cue = fixed and cue and query.get("aggregation") != cue
        query.setdefault(
            "aggregation_source",
            "explicit"
            if cue and not convention_cue
            else "inherited"
            if prior and not complete and not fixed
            else "planner_selected",
        )
    return plan
