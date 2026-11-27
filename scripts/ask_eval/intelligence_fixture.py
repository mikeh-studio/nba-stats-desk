"""Controlled fixture provider, never selected by the application loader.

The harness supplies canned model replies. This adapter contains no production
prompts or language heuristics; all scope and arithmetic remain host-owned.
"""

from __future__ import annotations

import json
import re
from dataclasses import fields

from app.agent import intelligence_contract as api

REQUIRED_QUERY_FIELDS = {
    "season",
    "season_type",
    "operation",
    "metric",
    "aggregation",
    "aggregation_source",
    "window",
}


class ControlledIntelligence:
    descriptor = api.ProviderDescriptor(
        "fixture", "1", api.API_VERSION, ("nba_semantics/0.1",), ("metrics",), True
    )

    def __init__(self, catalogs=None):
        self.catalogs = catalogs if catalogs is not None else {}
        self.next_result = None

    def prompts(self):
        return api.PromptBundle(
            "synthetic/1",
            "Controlled test transport. Return the authored fixture plan.",
        )

    def classify(self, context):
        return api.IntentSelection("metrics", context.prior is not None)

    async def plan(self, context, intent, *, prompts, budget, model):
        if self.next_result is not None:
            result, self.next_result = self.next_result, None
            return result
        players = self.catalogs.get(context.request_id, ())
        references = {
            f"resolved_player_{p['player_id']}": p["player_name"] for p in players
        }
        if not references:
            references = {name: name for name in context.player_names}
        references.update({name: name for name in context.player_names})
        question = context.question
        mentions = []
        for key, name in references.items():
            if not key.startswith("resolved_player_"):
                continue
            question, count = re.subn(
                r"(?<!\w)" + re.escape(name) + r"(?!\w)", key, question, flags=re.I
            )
            if count:
                mentions.append({"mention": key, "status": "ok"})
        prior = context.prior
        history = (
            {}
            if prior is None
            else {
                "metrics": prior.metric_keys,
                "players": [{"player_name": name} for name in prior.player_names],
            }
        )
        payload = {
            "question": question,
            "current_question": question,
            "selected_season": context.selected_season,
            "conversation_context": history,
            "recognized_entity_mentions": mentions,
            "available_team_abbreviations": context.team_abbreviations,
        }
        text = await model.generate_plan(
            system_prompt=prompts.planning_system, user_payload=json.dumps(payload)
        )
        try:
            raw = json.loads(text)
        except (TypeError, ValueError):
            return api.ProviderFailure("invalid_provider_output")
        if isinstance(raw, dict) and set(raw) == {"plan"}:
            raw = raw["plan"]
        if not isinstance(raw, dict) or set(raw) not in (
            {"status", "queries", "message"},
            {"status", "queries", "message", "reason"},
        ):
            return api.ProviderFailure("invalid_provider_output")
        if not isinstance(raw["queries"], list) or not isinstance(raw["message"], str):
            return api.ProviderFailure("invalid_provider_output")
        if raw["status"] in ("clarification_required", "unsupported"):
            if raw["queries"]:
                return api.ProviderFailure("invalid_provider_output")
            if raw.get("reason") is not None:
                return api.Withheld(raw["status"], raw["reason"])
            return api.Withheld(
                raw["status"],
                (
                    "missing_qualification"
                    if "attempt threshold" in raw.get("message", "")
                    else "ambiguous_scope"
                )
                if raw["status"] == "clarification_required"
                else "unsupported_operation",
            )
        queries = []
        scope_fields = {f.name for f in fields(api.Scope)}
        for original in raw["queries"]:
            if (
                not isinstance(original, dict)
                or not REQUIRED_QUERY_FIELDS <= original.keys()
                or any(
                    not isinstance(original[key], str) for key in REQUIRED_QUERY_FIELDS
                )
                or original["aggregation_source"]
                not in ("explicit", "inherited", "planner_selected", "omitted")
            ):
                return api.ProviderFailure("invalid_provider_output")
            q = dict(original)
            if q.get("player_name") is not None:
                if not isinstance(q["player_name"], str):
                    return api.ProviderFailure("invalid_provider_output")
                q["player_name"] = references.get(q["player_name"], q["player_name"])
            try:
                scope = api.Scope(**{k: q.pop(k) for k in scope_fields if k in q})
                queries.append(api.QueryProposal(scope=scope, **q))
            except TypeError:
                return api.ProviderFailure("invalid_provider_output")
        return api.QueryPlan("metrics", raw["status"], tuple(queries))


def install_controlled_provider():
    """Explicit evaluation-server wiring, never imported by the production app."""
    from app.agent import intelligence as host

    catalogs = {}
    provider = ControlledIntelligence(catalogs)
    original_context, original_loader = host.planning_context, host.get_provider

    def capture(question, selected_season, players, teams, conversation_context=None):
        context = original_context(
            question, selected_season, players, teams, conversation_context
        )
        catalogs[context.request_id] = players
        return context

    host.planning_context = capture
    host.get_provider = lambda settings: (
        provider
        if settings.ask_intelligence_provider == "fixture"
        else original_loader(settings)
    )
