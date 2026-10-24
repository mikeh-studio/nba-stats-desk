"""Controlled fixture provider, never selected by the application loader.

The harness supplies canned model replies. This adapter contains no production
prompts or language heuristics; all scope and arithmetic remain host-owned.
"""

from __future__ import annotations

import json
from dataclasses import fields

from app.agent import intelligence_contract as api
from app.agent.scope_policy import explicit_scope
from app.agent.semantics import load_contract


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
        import re

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
        raw = json.loads(text)
        if isinstance(raw, dict) and set(raw) == {"plan"}:
            raw = raw["plan"]
        if not isinstance(raw, dict) or set(raw) not in (
            {"status", "queries", "message"},
            {"status", "queries", "message", "reason"},
        ):
            return api.ProviderFailure("invalid_provider_output")
        if raw["status"] in ("clarification_required", "unsupported"):
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
            q = dict(original)
            if q.get("player_name") is not None:
                q["player_name"] = references.get(q["player_name"], q["player_name"])
            scope_hint = explicit_scope(context.question)
            phase = scope_hint.get("season_type") or (
                "Playoffs"
                if re.search(r"\bplayoffs?\b", context.question, re.I)
                else "Regular Season"
            )
            if (
                "season_type" not in q
                and context.prior
                and not mentions
                and not re.search(
                    r"\b(?:playoffs?|regular season)\b", context.question, re.I
                )
            ):
                phase = context.prior.scopes[0].season_type
            q.setdefault("season_type", phase)
            for key, value in scope_hint.items():
                if key in {"window", "start_date", "as_of", "n"}:
                    q.setdefault(key, value)
            if q.get("metric") in ("Fantasy Score", "fantasy", "fantasy_points"):
                q["metric"] = load_contract().default_fantasy_metric
            q.setdefault("operation", "summary")
            if q.get("aggregation_source") not in (
                "explicit",
                "inherited",
                "planner_selected",
                "omitted",
            ):
                q["aggregation_source"] = "omitted"
            scope = api.Scope(**{k: q.pop(k) for k in scope_fields if k in q})
            queries.append(api.QueryProposal(scope=scope, **q))
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
