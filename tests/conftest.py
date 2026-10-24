"""Explicit provider fixture for pre-hook public orchestration regressions.

Only modules marked scripted_intelligence opt in. Reference and conformance tests
exercise the real loader. Old canned handles are resolved from their source roster;
no private package, production prompts or planner heuristics are used.
"""

import pytest


@pytest.fixture
def scripted_intelligence(monkeypatch):
    from app.agent import intelligence as host
    from scripts.ask_eval.intelligence_fixture import ControlledIntelligence

    catalogs = {}
    provider = ControlledIntelligence(catalogs)
    build = host.planning_context

    def capture(question, selected_season, players, teams, conversation_context=None):
        result = build(question, selected_season, players, teams, conversation_context)
        catalogs[result.request_id] = players
        return result

    monkeypatch.setattr(host, "planning_context", capture)
    monkeypatch.setattr(host, "get_provider", lambda settings: provider)

    return provider
