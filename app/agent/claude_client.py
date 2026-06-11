"""Claude (Anthropic) backend for the stats agent.

The agent loop and planner speak the OpenAI Responses surface:
``client.responses.create(model, instructions, input, text, tools)`` returning
an object with ``output`` items, ``output_text``, and ``usage``. This module
adapts that surface onto ``anthropic.Anthropic().messages.create`` so the rest
of the agent code runs unchanged regardless of provider.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

_MAX_OUTPUT_TOKENS = 16000

# JSON Schema keywords the Anthropic structured-output validator rejects
# (numerical/string/array bounds). They are safe to drop because the agent
# re-validates parsed payloads with pydantic models that carry the same
# constraints.
_UNSUPPORTED_SCHEMA_KEYS = frozenset(
    {
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minProperties",
        "maxProperties",
    }
)


def _sanitize_schema(schema: Any) -> Any:
    if isinstance(schema, list):
        return [_sanitize_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    cleaned: dict[str, Any] = {}
    for key, value in schema.items():
        if key in _UNSUPPORTED_SCHEMA_KEYS:
            continue
        if key in {"enum", "const"}:
            cleaned[key] = value
        elif key in {"properties", "$defs", "definitions"} and isinstance(value, dict):
            # Values here are subschemas, but the keys are property names and
            # must survive even when they collide with constraint keywords.
            cleaned[key] = {name: _sanitize_schema(sub) for name, sub in value.items()}
        else:
            cleaned[key] = _sanitize_schema(value)
    return cleaned


def _convert_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    converted = []
    for tool in tools or []:
        if tool.get("type") != "function":
            continue
        converted.append(
            {
                "name": str(tool.get("name") or ""),
                "description": str(tool.get("description") or ""),
                "input_schema": tool.get("parameters")
                or {"type": "object", "properties": {}},
            }
        )
    return converted


def _convert_text_format(text: dict[str, Any] | None) -> dict[str, Any] | None:
    fmt = (text or {}).get("format") or {}
    if fmt.get("type") == "json_schema" and fmt.get("schema"):
        return {
            "format": {
                "type": "json_schema",
                "schema": _sanitize_schema(fmt["schema"]),
            }
        }
    return None


def _append_block(
    messages: list[dict[str, Any]], role: str, block: dict[str, Any]
) -> None:
    if messages and messages[-1]["role"] == role:
        messages[-1]["content"].append(block)
    else:
        messages.append({"role": role, "content": [block]})


def _convert_input(input_messages: list[Any]) -> list[dict[str, Any]]:
    """Translate Responses-style input into Anthropic messages.

    The agent echoes our own output items back (assistant text and
    function_call items) followed by function_call_output dicts, so tool_use
    blocks land in one assistant message and their tool_result blocks in the
    immediately following user message, as the Messages API requires.
    """
    messages: list[dict[str, Any]] = []
    for item in input_messages:
        if isinstance(item, dict):
            if item.get("type") == "function_call_output":
                _append_block(
                    messages,
                    "user",
                    {
                        "type": "tool_result",
                        "tool_use_id": str(item.get("call_id") or ""),
                        "content": str(item.get("output") or ""),
                    },
                )
                continue
            role = str(item.get("role") or "user")
            if role != "assistant":
                # "developer"/"system" instructions become user text; the
                # Messages API has no per-message system role on this path.
                role = "user"
            content = item.get("content")
            text = content if isinstance(content, str) else json.dumps(content)
            if text:
                _append_block(messages, role, {"type": "text", "text": text})
            continue
        item_type = getattr(item, "type", None)
        if item_type == "function_call":
            try:
                args = json.loads(getattr(item, "arguments", "") or "{}")
            except json.JSONDecodeError:
                args = {}
            _append_block(
                messages,
                "assistant",
                {
                    "type": "tool_use",
                    "id": str(getattr(item, "call_id", "") or ""),
                    "name": str(getattr(item, "name", "") or ""),
                    "input": args if isinstance(args, dict) else {},
                },
            )
        elif item_type == "output_text":
            text = str(getattr(item, "text", "") or "")
            if text:
                _append_block(messages, "assistant", {"type": "text", "text": text})
    return messages


class _AdapterResponse:
    def __init__(self, message: Any) -> None:
        self.output: list[Any] = []
        texts: list[str] = []
        for block in getattr(message, "content", None) or []:
            block_type = getattr(block, "type", None)
            if block_type == "text":
                text = str(getattr(block, "text", "") or "")
                texts.append(text)
                self.output.append(SimpleNamespace(type="output_text", text=text))
            elif block_type == "tool_use":
                self.output.append(
                    SimpleNamespace(
                        type="function_call",
                        name=str(getattr(block, "name", "") or ""),
                        arguments=json.dumps(
                            getattr(block, "input", None) or {}, default=str
                        ),
                        call_id=str(getattr(block, "id", "") or ""),
                    )
                )
        self.output_text = "".join(texts)
        self.usage = getattr(message, "usage", None)


class _AdapterResponses:
    def __init__(self, parent: ClaudeResponsesClient) -> None:
        self._parent = parent

    def create(self, **kwargs: Any) -> _AdapterResponse:
        return self._parent._create(**kwargs)


class ClaudeResponsesClient:
    def __init__(
        self,
        *,
        client: Any,
        model: str,
        timeout: float | None = None,
    ) -> None:
        self._client = client
        self._model = model
        self._timeout = timeout
        self.responses = _AdapterResponses(self)

    def with_options(self, *, timeout: float | None = None) -> ClaudeResponsesClient:
        return ClaudeResponsesClient(
            client=self._client,
            model=self._model,
            timeout=timeout if timeout is not None else self._timeout,
        )

    def _create(self, **kwargs: Any) -> _AdapterResponse:
        requested_model = str(kwargs.get("model") or "")
        model = (
            requested_model if requested_model.startswith("claude-") else self._model
        )
        messages = _convert_input(kwargs.get("input") or [])
        if messages:
            # Multi-turn breakpoint: each agent-loop iteration re-sends the
            # whole transcript, so marking the newest block lets the next
            # iteration read everything before it from cache.
            messages[-1]["content"][-1]["cache_control"] = {"type": "ephemeral"}
        request: dict[str, Any] = {
            "model": model,
            "max_tokens": _MAX_OUTPUT_TOKENS,
            "messages": messages,
        }
        instructions = str(kwargs.get("instructions") or "")
        if instructions:
            # A breakpoint on the system block caches tools + system together
            # (tools render first). Callers must keep instructions
            # byte-stable across requests for this to hit.
            request["system"] = [
                {
                    "type": "text",
                    "text": instructions,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        tools = _convert_tools(kwargs.get("tools"))
        if tools:
            request["tools"] = tools
        output_config = _convert_text_format(kwargs.get("text"))
        if output_config is not None:
            request["output_config"] = output_config
        client = self._client
        if self._timeout is not None and hasattr(client, "with_options"):
            client = client.with_options(timeout=self._timeout)
        # Stream and accumulate instead of a plain create(): structured
        # answers at this max_tokens routinely outlast a non-streaming read
        # timeout, while a live stream keeps the connection alive.
        on_text_delta = kwargs.get("on_output_text_delta")
        with client.messages.stream(**request) as stream:
            if on_text_delta is not None:
                for delta in stream.text_stream:
                    on_text_delta(delta)
            message = stream.get_final_message()
        return _AdapterResponse(message)
