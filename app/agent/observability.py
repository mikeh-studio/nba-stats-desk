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
    # Payload ask_route: route label, source and reason code; never question text.
    ask_route: dict[str, str | None] | None = None
    tools: list[dict[str, Any]] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_input_tokens: int | None = None
    reasoning_output_tokens: int | None = None
    token_detail_calls: dict[str, int] = field(default_factory=dict)
    data_reads_dropped: int = 0
    data_reads: list[dict[str, Any]] = field(default_factory=list)
    usage_reported_calls: int = 0
    model_calls: int = 0
    provider_failures: int = 0
    stage_latency_ms: dict[str, float] = field(default_factory=dict)
    stage_exclusive_latency_ms: dict[str, float] = field(default_factory=dict)
    capacity: dict[str, int | bool] = field(default_factory=dict)
    outcome: str = "unknown"
    error_type: str | None = None
    overview_tracking: list[dict[str, Any]] = field(default_factory=list)
    private_capture: Any = field(default=None, repr=False)
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
        self.usage_reported_calls += 1
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
        for detail_names, field_name, attr in (
            (
                ("input_tokens_details", "prompt_tokens_details"),
                "cached_tokens",
                "cached_input_tokens",
            ),
            (
                ("output_tokens_details", "completion_tokens_details"),
                "reasoning_tokens",
                "reasoning_output_tokens",
            ),
        ):
            details = next(
                (
                    getattr(usage, key, None)
                    for key in detail_names
                    if getattr(usage, key, None) is not None
                ),
                None,
            )
            value = (
                details.get(field_name)
                if isinstance(details, dict)
                else getattr(details, field_name, None)
            )
            if type(value) is int and value >= 0:
                setattr(self, attr, (getattr(self, attr) or 0) + value)
                self.token_detail_calls[attr] = self.token_detail_calls.get(attr, 0) + 1

    def to_log_dict(self) -> dict[str, Any]:
        # Rich tool records belong to the response/local history, not service logs.
        return {
            "event_name": "agent_request_summary",
            "request_id": self.request_id,
            "route": self.route,
            "ask_route": self.ask_route,
            "confidence": self.confidence,
            "model": self.model,
            "total_tool_calls": len(self.tools),
            "model_calls": self.model_calls,
            "provider_failures": self.provider_failures,
            "stage_latency_ms": {
                key: round(value, 3) for key, value in self.stage_latency_ms.items()
            },
            "stage_exclusive_latency_ms": {
                key: round(value, 3)
                for key, value in self.stage_exclusive_latency_ms.items()
            },
            "data_read_coverage": "semantic_fact_source_only",
            "data_reads": list(self.data_reads),
            "usage_reported_calls": self.usage_reported_calls,
            "data_reads_dropped": self.data_reads_dropped,
            "token_detail_calls": dict(self.token_detail_calls),
            "capacity": dict(self.capacity),
            "tool_latency_ms": sum(tool["duration_ms"] for tool in self.tools),
            "latency_ms": int((monotonic() - self.start_time) * 1000),
            "tokens": {
                "prompt": self.prompt_tokens,
                "completion": self.completion_tokens,
                "total": self.total_tokens,
                "cached_input": self.cached_input_tokens,
                "reasoning_output": self.reasoning_output_tokens,
            },
            "outcome": self.outcome,
            "error_type": self.error_type,
        }

    def emit(self) -> None:
        from app.agent.overview_tracking import emit_tracking

        logger.info(json.dumps(self.to_log_dict(), sort_keys=True, default=str))
        emit_tracking(self.request_id, self.overview_tracking, self.outcome)
