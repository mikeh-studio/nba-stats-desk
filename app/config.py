from __future__ import annotations

import os
from dataclasses import dataclass

SUPPORTED_SEASON = "2025-26"


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "t", "yes", "y", "on"}


@dataclass(frozen=True)
class Settings:
    project_id: str
    gold_dataset: str
    metadata_dataset: str
    freshness_threshold_hours: int
    max_search_results: int
    agent_dataset: str = "nba_agent"
    openai_api_key: str | None = None
    openai_agent_model: str = "gpt-5.4-mini"
    openai_agent_enabled: bool = True
    openai_agent_timeout_seconds: float = 20.0
    openai_agent_max_retries: int = 2
    openai_agent_retry_base_delay_seconds: float = 0.5
    agent_planner_enabled: bool = True
    agent_planner_model: str | None = None
    agent_planner_timeout_seconds: float = 8.0
    agent_planner_min_confidence: float = 0.55
    agent_player_match_min_confidence: float = 0.78
    agent_max_tool_calls: int = 6
    agent_question_max_chars: int = 500
    agent_rate_limit_per_minute: int = 12
    agent_rate_limit_daily: int = 200
    agent_rate_limit_redis_url: str | None = None
    agent_conversation_max_turns: int = 6
    agent_cache_ttl_seconds: int = 300
    performance_cache_prewarm_enabled: bool = True


def get_settings() -> Settings:
    return Settings(
        project_id=os.getenv("BQ_PROJECT", os.getenv("GCP_PROJECT_ID", "")),
        gold_dataset=os.getenv("BQ_DATASET_GOLD", "nba_gold"),
        agent_dataset=os.getenv("BQ_DATASET_AGENT", "nba_agent"),
        metadata_dataset=os.getenv("BQ_METADATA_DATASET", "nba_metadata"),
        freshness_threshold_hours=int(os.getenv("API_FRESHNESS_THRESHOLD_HOURS", "36")),
        max_search_results=int(os.getenv("API_MAX_SEARCH_RESULTS", "12")),
        openai_api_key=os.getenv("OPENAI_API_KEY") or None,
        openai_agent_model=os.getenv("OPENAI_AGENT_MODEL", "gpt-5.4-mini"),
        openai_agent_enabled=_env_bool("OPENAI_AGENT_ENABLED", True),
        openai_agent_timeout_seconds=float(
            os.getenv("OPENAI_AGENT_TIMEOUT_SECONDS", "20")
        ),
        openai_agent_max_retries=int(os.getenv("OPENAI_AGENT_MAX_RETRIES", "2")),
        openai_agent_retry_base_delay_seconds=float(
            os.getenv("OPENAI_AGENT_RETRY_BASE_DELAY_SECONDS", "0.5")
        ),
        agent_planner_enabled=_env_bool("AGENT_PLANNER_ENABLED", True),
        agent_planner_model=os.getenv("AGENT_PLANNER_MODEL") or None,
        agent_planner_timeout_seconds=float(
            os.getenv("AGENT_PLANNER_TIMEOUT_SECONDS", "8")
        ),
        agent_planner_min_confidence=float(
            os.getenv("AGENT_PLANNER_MIN_CONFIDENCE", "0.55")
        ),
        agent_player_match_min_confidence=float(
            os.getenv("AGENT_PLAYER_MATCH_MIN_CONFIDENCE", "0.78")
        ),
        agent_max_tool_calls=int(os.getenv("AGENT_MAX_TOOL_CALLS", "6")),
        agent_question_max_chars=int(os.getenv("AGENT_QUESTION_MAX_CHARS", "500")),
        agent_rate_limit_per_minute=int(os.getenv("AGENT_RATE_LIMIT_PER_MINUTE", "12")),
        agent_rate_limit_daily=int(os.getenv("AGENT_RATE_LIMIT_DAILY", "200")),
        agent_rate_limit_redis_url=os.getenv("AGENT_RATE_LIMIT_REDIS_URL") or None,
        agent_conversation_max_turns=int(
            os.getenv("AGENT_CONVERSATION_MAX_TURNS", "6")
        ),
        agent_cache_ttl_seconds=int(os.getenv("AGENT_CACHE_TTL_SECONDS", "300")),
        performance_cache_prewarm_enabled=_env_bool(
            "PERFORMANCE_CACHE_PREWARM_ENABLED",
            True,
        ),
    )
