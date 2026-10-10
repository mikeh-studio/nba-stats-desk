"""Loopback-only evaluation app. Explicit frozen dependencies, no production writes."""

import os
import sys
from dataclasses import replace
from pathlib import Path

# Select baseline/candidate app code before any app import. Runner code stays fixed.
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, os.environ.get("ASK_EVAL_CODE_ROOT", str(ROOT)))
if os.environ.get("ASK_EVAL_LIVE") == "1":
    from dotenv import load_dotenv

    # Archived app code may not discover the working checkout's local config.
    # Load here without copying credentials into the archive or run artifacts.
    load_dotenv(ROOT / ".env")

from app.config import Settings  # noqa: E402
from app.main import app, get_agent_client, get_repository, get_settings  # noqa: E402
from fastapi import Request  # noqa: E402
from scripts.ask_eval.fixtures import (  # noqa: E402
    ControlledModel,
    frozen_repository,
    load_inputs,
)

source, document = load_inputs(
    ROOT,
    Path(os.environ["ASK_EVAL_INPUT_DIR"])
    if os.environ.get("ASK_EVAL_INPUT_DIR")
    else None,
)
lookup = {c["id"]: c for c in document["cases"]}
repositories = {}


def settings():
    # Settings() itself does not read the user's .env. No live source is used.
    base = Settings(
        project_id="fixture-project",
        gold_dataset="fixture",
        metadata_dataset="fixture",
        freshness_threshold_hours=36,
        max_search_results=12,
        season=source.get("season", "2024-25"),
        openai_api_key="offline-fixture",
        agent_rate_limit_per_minute=0,
        agent_rate_limit_daily=0,
        agent_history_enabled=False,
        performance_cache_prewarm_enabled=False,
    )
    if os.environ.get("ASK_EVAL_LIVE") == "1":
        live = get_settings()
        return replace(
            base,
            openai_api_key=live.openai_api_key,
            anthropic_api_key=live.anthropic_api_key,
            openrouter_api_key=live.openrouter_api_key,
            openai_agent_max_retries=0,
        )
    return base


# A global budget bounds attempted model calls across both endpoint transports.
# SDK retries are disabled on the evaluation clients; runtime defaults are untouched.
from scripts.ask_eval.budget import BudgetClient, CallBudget  # noqa: E402

budget = CallBudget(
    int(os.environ.get("ASK_EVAL_MAX_CALLS", "0")), os.environ.get("ASK_EVAL_CALL_LOG")
)
if os.environ.get("ASK_EVAL_LIVE") == "1":
    from app.agent.service import StatsAgent

    _get_client = StatsAgent._get_client

    def budgeted_client(self, provider="openai"):
        client = _get_client(self, provider)
        if provider == "openai":
            client = client.with_options(max_retries=0, timeout=30)
        return BudgetClient(client, budget)

    StatsAgent._get_client = budgeted_client


@app.get("/__eval/metadata")
def evaluation_metadata():
    return {
        "model_calls": budget.calls,
        "source_kind": source.get("source_kind", "synthetic"),
        "model_usage": budget.records,
        "live": os.environ.get("ASK_EVAL_LIVE") == "1",
    }


@app.post("/__eval/reset-conversations")
def reset_conversations():
    # Evaluation-only process: simulate eviction/restart without changing the
    # browser's conversation identity or mocking its endpoint responses.
    from app.main import _season_conversation_store

    _season_conversation_store.cache_clear()
    return {"reset": True}


def case_for(request):
    cid = request.headers.get("X-Evaluation-Case", next(iter(lookup)))
    return lookup[cid]


def repository(request: Request):
    case = case_for(request)
    variant = case["source_variant"]
    if variant not in repositories:
        if source.get("source_kind") == "production_snapshot":
            from scripts.ask_eval.production import production_repository

            if variant != "complete":
                raise ValueError("Production evidence must not be mutated")
            repositories[variant] = production_repository(settings(), source)
        else:
            repositories[variant] = frozen_repository(settings(), source, variant)
    return repositories[variant]


def model(request: Request):
    if os.environ.get("ASK_EVAL_LIVE") == "1":
        return None
    case = case_for(request)
    index = int(request.headers.get("X-Evaluation-Turn", "0"))
    return ControlledModel(case["turns"][index])


app.dependency_overrides.update(
    {get_settings: settings, get_repository: repository, get_agent_client: model}
)
