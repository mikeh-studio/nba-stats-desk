"""Adapt Ask's bounded Responses contract to OpenRouter Chat Completions."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from jsonschema import validate

from app.config import OPENROUTER_AGENT_MODEL_VALUES


class OpenRouterResponsesClient:
    def __init__(self, client: Any) -> None:
        self.client = client
        self.responses = self

    def with_options(self, *, timeout: float) -> OpenRouterResponsesClient:
        return OpenRouterResponsesClient(self.client.with_options(timeout=timeout))

    def create(
        self,
        *,
        model: str,
        instructions: str,
        input: list[Any],
        text: dict,
        tools: list[dict] | None = None,
        **kwargs: Any,
    ) -> Any:
        if model not in OPENROUTER_AGENT_MODEL_VALUES:
            raise ValueError("Unsupported OpenRouter model")
        messages: list[dict] = [{"role": "system", "content": instructions}]
        for item in input:
            if isinstance(item, dict):
                if item.get("type") == "function_call_output":
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": item["call_id"],
                            "content": item["output"],
                        }
                    )
                else:
                    messages.append(
                        {
                            "role": "system"
                            if item.get("role") == "developer"
                            else item.get("role", "user"),
                            "content": item["content"],
                        }
                    )
            elif getattr(item, "type", None) == "openrouter_message":
                # Preserve reasoning details and tool-call IDs exactly on continuation.
                messages.append(item.message)
            elif getattr(item, "type", None) != "function_call":
                raise ValueError("Unsupported OpenRouter input item")
        fmt = text.get("format") or {}
        params: dict[str, Any] = dict(
            model=model,
            messages=messages,
            max_tokens=16000,
            extra_body={"provider": {"require_parameters": True}},
        )
        if fmt.get("type") == "json_schema":
            params["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    k: fmt[k] for k in ("name", "schema", "strict") if k in fmt
                },
            }
        elif fmt.get("type") not in (None, "text"):
            raise ValueError("Unsupported OpenRouter output format")
        if tools:
            if any(t.get("type") != "function" for t in tools):
                raise ValueError("Only allowlisted function tools are supported")
            params["tools"] = [
                {
                    "type": "function",
                    "function": {k: v for k, v in t.items() if k != "type"},
                }
                for t in tools
            ]
        completion = self.client.chat.completions.create(**params)
        choice = completion.choices[0]
        if choice.finish_reason not in ("stop", "tool_calls"):
            raise ValueError("OpenRouter response was incomplete or filtered")
        message = choice.message
        if getattr(message, "refusal", None):
            raise ValueError("OpenRouter declined this response")
        content = message.content or ""
        calls = message.tool_calls or []
        if not calls and fmt.get("type") == "json_schema":
            validate(json.loads(content), fmt["schema"])
        raw = message.model_dump(exclude_none=True)
        raw = {
            k: v
            for k, v in raw.items()
            if k in {"role", "content", "tool_calls", "reasoning_details"}
        }
        output = [SimpleNamespace(type="openrouter_message", message=raw)]
        output.extend(
            SimpleNamespace(
                type="function_call",
                name=c.function.name,
                arguments=c.function.arguments,
                call_id=c.id,
            )
            for c in calls
        )
        u = completion.usage
        usage = (
            SimpleNamespace(
                input_tokens=u.prompt_tokens,
                output_tokens=u.completion_tokens,
                total_tokens=u.total_tokens,
            )
            if u
            else None
        )
        return SimpleNamespace(output=output, output_text=content, usage=usage)
