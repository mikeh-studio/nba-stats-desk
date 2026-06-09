from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from time import monotonic
from typing import Any

LOGGER_NAME = "app.agent"
logger = logging.getLogger(LOGGER_NAME)


def truncate_value(value: Any, *, limit: int = 500) -> Any:
    if isinstance(value, str):
        return value if len(value) <= limit else f"{value[:limit]}..."
    if isinstance(value, dict):
        return {
            str(key): truncate_value(item, limit=limit)
            for key, item in list(value.items())[:20]
        }
    if isinstance(value, list):
        return [truncate_value(item, limit=limit) for item in value[:20]]
    return value


def summarize_tool_result(result: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {"status": result.get("status")}
    for key in (
        "message",
        "player_id",
        "player_name",
        "games_returned",
        "cohort_size",
        "percentile",
    ):
        if key in result:
            summary[key] = result.get(key)
    for key in ("matches", "rows", "trends", "similar_players", "metrics"):
        value = result.get(key)
        if isinstance(value, list):
            summary[f"{key}_count"] = len(value)
    return summary


@dataclass
class AgentTrace:
    request_id: str
    question: str
    model: str
    conversation_id: str | None = None
    route: str | None = None
    confidence: float | None = None
    tools: list[dict[str, Any]] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    outcome: str = "unknown"
    error_type: str | None = None
    start_time: float = field(default_factory=monotonic)

    def set_plan(self, *, route: str, confidence: float) -> None:
        self.route = route
        self.confidence = confidence

    def add_tool(
        self,
        *,
        name: str,
        args: dict[str, Any],
        status: str,
        duration_ms: int,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        record = {
            "name": name,
            "args": truncate_value(args, limit=300),
            "status": status,
            "duration_ms": duration_ms,
            "result": truncate_value(summarize_tool_result(result), limit=300),
        }
        self.tools.append(record)
        return record

    def add_usage(self, usage: Any) -> None:
        if usage is None:
            return
        prompt = (
            getattr(usage, "input_tokens", None)
            or getattr(usage, "prompt_tokens", None)
            or 0
        )
        completion = (
            getattr(usage, "output_tokens", None)
            or getattr(usage, "completion_tokens", None)
            or 0
        )
        total = getattr(usage, "total_tokens", None) or int(prompt) + int(completion)
        self.prompt_tokens += int(prompt)
        self.completion_tokens += int(completion)
        self.total_tokens += int(total)

    def to_log_dict(self) -> dict[str, Any]:
        return {
            "event_name": "agent_request_summary",
            "request_id": self.request_id,
            "conversation_id": self.conversation_id,
            "question": truncate_value(self.question, limit=500),
            "route": self.route,
            "confidence": self.confidence,
            "model": self.model,
            "tools": self.tools,
            "total_tool_calls": len(self.tools),
            "latency_ms": int((monotonic() - self.start_time) * 1000),
            "tokens": {
                "prompt": self.prompt_tokens,
                "completion": self.completion_tokens,
                "total": self.total_tokens,
            },
            "outcome": self.outcome,
            "error_type": self.error_type,
        }

    def emit(self) -> None:
        logger.info(json.dumps(self.to_log_dict(), sort_keys=True, default=str))
