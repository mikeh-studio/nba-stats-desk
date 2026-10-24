"""The single optional import boundary for the frozen legacy compatibility tree.

The governed app never calls these functions. Missing legacy code is unavailable,
not a reason to run a governed query against a non-governed repository.
"""

from __future__ import annotations

from importlib import import_module
from importlib.util import find_spec


def legacy_available() -> bool:
    return all(
        find_spec("app.agent." + name) is not None for name in ("planner", "router")
    )


def build_agent_plan(*args, **kwargs):
    return import_module("app.agent.router").build_agent_plan(*args, **kwargs)


def build_query_plan(*args, **kwargs):
    return import_module("app.agent.planner").build_query_plan(*args, **kwargs)


def deterministic_query_plan(*args, **kwargs):
    return import_module("app.agent.planner").deterministic_query_plan(*args, **kwargs)


def detect_opponent_breakdown(*args, **kwargs):
    return import_module("app.agent.planner").detect_opponent_breakdown(*args, **kwargs)


def legacy_prompt(kind: str) -> str:
    prompts = import_module("app.agent.planner")
    return prompts.SYSTEM_PROMPT if kind == "system" else prompts.EVIDENCE_PROMPT
