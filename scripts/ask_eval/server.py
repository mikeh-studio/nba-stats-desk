"""Loopback-only evaluation app. Explicit frozen dependencies, no production writes."""

import json
import os
import sys
import tempfile
from dataclasses import replace
from datetime import date
from pathlib import Path
from unittest.mock import patch

# Select baseline/candidate app code before any app import. Runner code stays fixed.
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, os.environ.get("ASK_EVAL_CODE_ROOT", str(ROOT)))
if os.environ.get("ASK_EVAL_LIVE") == "1":
    from scripts.ask_eval.environment import load_provider_credentials

    load_provider_credentials(ROOT / ".env")

# This evaluation-only import scope prevents app.config's automatic .env load
# from bypassing the credential-only loader (including on older dotenv versions).
with patch("dotenv.load_dotenv", return_value=False):
    from app.config import Settings
    from app.main import app, get_agent_client, get_repository, get_settings

from fastapi import HTTPException, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from scripts.ask_eval.fixtures import (  # noqa: E402
    ControlledModel,
    FixtureAttachments,
    FixtureCapabilityError,
    frozen_repository,
    load_inputs,
)
from scripts.ask_eval.source_guard import failures as source_failures  # noqa: E402

source, document = load_inputs(
    ROOT,
    Path(os.environ["ASK_EVAL_INPUT_DIR"])
    if os.environ.get("ASK_EVAL_INPUT_DIR")
    else None,
)
lookup = {c["id"]: c for c in document["cases"]}
repositories = {}
_artifact_directory = tempfile.TemporaryDirectory(prefix="ask-eval-fixtures-")
_attachments = FixtureAttachments(source, _artifact_directory.name)
_capability_failures = {}


def settings(request: Request = None):
    # Settings() itself does not read the user's .env. No live source is used.
    base = Settings(
        project_id="fixture-project",
        ask_formula_cache_enabled=os.environ.get("ASK_EVAL_FORMULA_CACHE") == "1",
        ask_cohort_cache_enabled=os.environ.get("ASK_EVAL_COHORT_CACHE") == "1",
        ask_claim_enforcement=os.environ.get("ASK_EVAL_CLAIM_ENFORCEMENT", "log"),
        ask_deterministic_planning_enabled=os.environ.get(
            "ASK_EVAL_DETERMINISTIC_COMMON"
        )
        == "1",
        gold_dataset="fixture",
        metadata_dataset="fixture",
        freshness_threshold_hours=36,
        max_search_results=12,
        season=source.get("season", "2024-25"),
        openai_api_key="offline-fixture",
        agent_rate_limit_per_minute=0,
        agent_rate_limit_daily=0,
        agent_history_enabled=False,
        agent_trace_enabled=False,
        performance_cache_prewarm_enabled=False,
    )
    if os.environ.get("ASK_EVAL_LIVE") == "1":
        live = get_settings()
        base = replace(
            base,
            openai_api_key=live.openai_api_key,
            anthropic_api_key=live.anthropic_api_key,
            openrouter_api_key=live.openrouter_api_key,
            openai_agent_max_retries=0,
            anthropic_agent_max_retries=0,
            openrouter_agent_max_retries=0,
        )
    if os.environ.get("ASK_EVAL_CASSETTE_MODE"):
        base = replace(
            base,
            openai_agent_max_retries=0,
            anthropic_agent_max_retries=0,
            openrouter_agent_max_retries=0,
        )
    attachments = case_for(request).get("attachments", []) if request else []
    try:
        return _attachments.configure(base, attachments)
    except FixtureCapabilityError as exc:
        _capability_failures[case_for(request)["id"]] = exc.detail
        # This is a harness capability failure, not an application answer or pass.
        # Stop before executing Ask or falling back to live sources.
        raise HTTPException(status_code=501, detail=exc.detail) from exc


# A global budget bounds attempted model calls across both endpoint transports.
# Claude/OpenRouter SDKs are constructed with max_retries=0 in StatsAgent.
# OpenAI is disabled explicitly below; settings() disables the shared app retry loop.
# Provider adapters preserve those SDK settings when cloning their timeout.
from scripts.ask_eval.budget import BudgetClient, CallBudget  # noqa: E402

budget = CallBudget(
    int(os.environ.get("ASK_EVAL_MAX_CALLS", "0")), os.environ.get("ASK_EVAL_CALL_LOG")
)
cassette = None
if os.environ.get("ASK_EVAL_CASSETTE_MODE"):
    from scripts.ask_eval.cassettes import (
        CassetteClient,
        CassetteStore,
        call_context,
        install_network_guard,
        settings_binding,
    )

    cassette = CassetteStore(
        os.environ["ASK_EVAL_CASSETTE_DIR"],
        os.environ["ASK_EVAL_CASSETTE_MODE"],
        json.loads(os.environ["ASK_EVAL_BINDING"]),
    )
    if cassette.mode == "replay":
        install_network_guard(cassette.fail)


@app.middleware("http")
async def evaluation_context(request, call_next):
    if not request.url.path.startswith("/api/agent/ask"):
        return await call_next(request)
    clock_token = None
    context_token = None
    try:
        if os.environ.get("ASK_EVAL_CLOCK"):
            from app.agent.clock import semantic_date

            clock_token = semantic_date.set(
                date.fromisoformat(os.environ["ASK_EVAL_CLOCK"])
            )
        if cassette:
            case = case_for(request)
            try:
                turn = int(request.headers["X-Evaluation-Turn"])
                if not 0 <= turn < len(case["turns"]):
                    raise ValueError()
            except (KeyError, ValueError):
                raise HTTPException(
                    status_code=400, detail="Valid X-Evaluation-Turn is required"
                )
            context_token = call_context.set(
                {
                    "case": case["id"],
                    "turn": turn,
                    "transport": "sse"
                    if request.url.path.endswith("/stream")
                    else "json",
                    "settings_sha256": settings_binding(settings(request)),
                }
            )
        return await call_next(request)
    except HTTPException as exc:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    finally:
        if context_token is not None:
            call_context.reset(context_token)
        if clock_token is not None:
            semantic_date.reset(clock_token)


if os.environ.get("ASK_EVAL_LIVE") == "1" or cassette:
    from app.agent.service import StatsAgent

    _get_client = StatsAgent._get_client

    def budgeted_client(self, provider="openai"):
        if cassette and cassette.mode == "replay":
            return CassetteClient(cassette, provider)
        client = _get_client(self, provider)
        if provider == "openai":
            client = client.with_options(max_retries=0, timeout=30)
        client = BudgetClient(client, budget)
        return CassetteClient(cassette, provider, client) if cassette else client

    StatsAgent._get_client = budgeted_client
    if cassette and cassette.mode == "replay":
        StatsAgent._get_openrouter_client = lambda self: CassetteClient(
            cassette, "openrouter"
        )
        StatsAgent._get_claude_client = lambda self: CassetteClient(cassette, "claude")


@app.get("/__eval/metadata")
def evaluation_metadata():
    return {
        "cassette_errors": list(cassette.errors) if cassette else [],
        "fixture_capability_failures": dict(_capability_failures),
        "uncaptured_reads": source_failures(),
        "model_calls": budget.calls,
        "source_kind": source.get("source_kind", "synthetic"),
        "model_usage": budget.records,
        "live": os.environ.get("ASK_EVAL_LIVE") == "1",
    }


@app.post("/__eval/finalize")
def finalize():
    return cassette.terminal() if cassette else {"errors": []}


@app.post("/__eval/reset-conversations")
def reset_conversations():
    # Evaluation-only process: simulate eviction/restart without changing the
    # browser's conversation identity or mocking its endpoint responses.
    from app.main import _season_conversation_store

    _season_conversation_store.cache_clear()
    return {"reset": True}


def case_for(request):
    cid = request.headers.get(
        "X-Evaluation-Case", None if cassette else next(iter(lookup))
    )
    if cid not in lookup:
        raise HTTPException(
            status_code=400, detail="Valid X-Evaluation-Case is required"
        )
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
        if os.environ.get("ASK_EVAL_SERVING_FIXTURE") == "1":
            from scripts.ask_eval.fixtures import prepared_fixture_repository

            repositories[variant] = prepared_fixture_repository(
                repositories[variant], source, variant
            )
    return repositories[variant]


def model(request: Request):
    if os.environ.get("ASK_EVAL_LIVE") == "1" or cassette:
        return None
    case = case_for(request)
    index = int(request.headers.get("X-Evaluation-Turn", "0"))
    return ControlledModel(case["turns"][index])


app.dependency_overrides.update(
    {get_settings: settings, get_repository: repository, get_agent_client: model}
)
