from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.claude_client import (
    ClaudeResponsesClient,
    _AdapterResponse,
    _convert_input,
    _convert_text_format,
    _convert_tools,
)
from app.agent.service import AgentDisabledError, StatsAgent
from tests.test_agent_service import AgentServiceFakeRepository, _settings

ANSWER_JSON = json.dumps(
    {
        "answer": "Answered from Claude.",
        "assumptions": [],
        "tables": [],
        "charts": [],
        "metric_definitions": [],
        "followups": [],
    }
)


def _text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def _tool_use_block(name: str, args: dict, block_id: str) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", name=name, input=args, id=block_id)


class FakeMessageStream:
    def __init__(self, message: SimpleNamespace, chunk_size: int = 7) -> None:
        self._message = message
        self._chunk_size = chunk_size

    def __enter__(self) -> FakeMessageStream:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    @property
    def text_stream(self):
        for block in self._message.content:
            if getattr(block, "type", None) != "text":
                continue
            text = block.text
            for start in range(0, len(text), self._chunk_size):
                yield text[start : start + self._chunk_size]

    def get_final_message(self) -> SimpleNamespace:
        return self._message


class FakeAnthropicMessages:
    def __init__(self, blocks_sequence: list[list[SimpleNamespace]]) -> None:
        self.calls = 0
        self.kwargs: list[dict] = []
        self._blocks_sequence = blocks_sequence

    def stream(self, **kwargs):
        self.kwargs.append(kwargs)
        index = min(self.calls, len(self._blocks_sequence) - 1)
        self.calls += 1
        return FakeMessageStream(
            SimpleNamespace(
                content=self._blocks_sequence[index],
                usage=SimpleNamespace(input_tokens=12, output_tokens=7),
            )
        )


class FakeAnthropic:
    def __init__(self, blocks_sequence: list[list[SimpleNamespace]]) -> None:
        self.messages = FakeAnthropicMessages(blocks_sequence)

    def with_options(self, **_kwargs):
        return self


def test_convert_tools_maps_function_schemas() -> None:
    tools = _convert_tools(
        [
            {
                "type": "function",
                "name": "resolve_player",
                "description": "Resolve a player name.",
                "strict": True,
                "parameters": {"type": "object", "properties": {}},
            }
        ]
    )

    assert tools == [
        {
            "name": "resolve_player",
            "description": "Resolve a player name.",
            "input_schema": {"type": "object", "properties": {}},
        }
    ]


def test_convert_text_format_strips_unsupported_schema_constraints() -> None:
    # The Anthropic structured-output validator 400s on numerical and string
    # bounds; pydantic enforces them client-side, so the adapter drops them.
    output_config = _convert_text_format(
        {
            "format": {
                "type": "json_schema",
                "name": "plan",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "min_games": {
                            "type": ["integer", "null"],
                            "minimum": 1,
                            "maximum": 200,
                        },
                        "route": {"type": "string", "enum": ["overview"]},
                        "names": {
                            "type": "array",
                            "items": {"type": "string", "maxLength": 80},
                            "maxItems": 5,
                        },
                    },
                    "required": ["min_games", "route", "names"],
                    "additionalProperties": False,
                },
            }
        }
    )

    assert output_config == {
        "format": {
            "type": "json_schema",
            "schema": {
                "type": "object",
                "properties": {
                    "min_games": {"type": ["integer", "null"]},
                    "route": {"type": "string", "enum": ["overview"]},
                    "names": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["min_games", "route", "names"],
                "additionalProperties": False,
            },
        }
    }


def test_convert_input_groups_tool_blocks_into_message_pairs() -> None:
    function_call = SimpleNamespace(
        type="function_call",
        name="resolve_player",
        arguments=json.dumps({"name": "Tyrese Maxey", "limit": 5}),
        call_id="toolu_1",
    )
    messages = _convert_input(
        [
            {"role": "user", "content": "How is Tyrese Maxey trending?"},
            {"role": "developer", "content": "Evidence bundle here."},
            SimpleNamespace(type="output_text", text="Checking the player."),
            function_call,
            {
                "type": "function_call_output",
                "call_id": "toolu_1",
                "output": '{"status": "ok"}',
            },
        ]
    )

    assert [message["role"] for message in messages] == ["user", "assistant", "user"]
    # Developer instructions fold into the preceding user turn.
    assert [block["text"] for block in messages[0]["content"]] == [
        "How is Tyrese Maxey trending?",
        "Evidence bundle here.",
    ]
    assistant_blocks = messages[1]["content"]
    assert assistant_blocks[0] == {"type": "text", "text": "Checking the player."}
    assert assistant_blocks[1]["type"] == "tool_use"
    assert assistant_blocks[1]["id"] == "toolu_1"
    assert assistant_blocks[1]["input"] == {"name": "Tyrese Maxey", "limit": 5}
    assert messages[2]["content"][0] == {
        "type": "tool_result",
        "tool_use_id": "toolu_1",
        "content": '{"status": "ok"}',
    }


def test_adapter_response_exposes_responses_surface() -> None:
    message = SimpleNamespace(
        content=[
            _text_block("Working on it."),
            _tool_use_block("get_player_trends", {"player_id": 7}, "toolu_9"),
        ],
        usage=SimpleNamespace(input_tokens=3, output_tokens=2),
    )

    response = _AdapterResponse(message)

    assert response.output_text == "Working on it."
    function_calls = [item for item in response.output if item.type == "function_call"]
    assert function_calls[0].name == "get_player_trends"
    assert json.loads(function_calls[0].arguments) == {"player_id": 7}
    assert function_calls[0].call_id == "toolu_9"
    assert response.usage.input_tokens == 3


def test_adapter_uses_claude_model_and_output_config() -> None:
    fake = FakeAnthropic([[_text_block(ANSWER_JSON)]])
    adapter = ClaudeResponsesClient(client=fake, model="claude-opus-4-8")

    response = adapter.with_options(timeout=5.0).responses.create(
        model="gpt-5.4-mini",
        instructions="System prompt.",
        input=[{"role": "user", "content": "Question?"}],
        text={
            "format": {
                "type": "json_schema",
                "name": "answer",
                "schema": {"type": "object"},
                "strict": True,
            }
        },
        tools=[
            {
                "type": "function",
                "name": "resolve_player",
                "description": "Resolve.",
                "parameters": {"type": "object", "properties": {}},
            }
        ],
    )

    kwargs = fake.messages.kwargs[-1]
    assert kwargs["model"] == "claude-opus-4-8"
    assert kwargs["system"] == [
        {
            "type": "text",
            "text": "System prompt.",
            "cache_control": {"type": "ephemeral"},
        }
    ]
    last_block = kwargs["messages"][-1]["content"][-1]
    assert last_block["cache_control"] == {"type": "ephemeral"}
    assert kwargs["output_config"] == {
        "format": {"type": "json_schema", "schema": {"type": "object"}}
    }
    assert kwargs["tools"][0]["name"] == "resolve_player"
    assert response.output_text == ANSWER_JSON


def test_adapter_allows_claude_model_override() -> None:
    fake = FakeAnthropic([[_text_block(ANSWER_JSON)]])
    adapter = ClaudeResponsesClient(client=fake, model="claude-opus-4-8")

    adapter.responses.create(
        model="claude-fable-5",
        instructions="System prompt.",
        input=[{"role": "user", "content": "Question?"}],
        text=None,
    )

    assert fake.messages.kwargs[-1]["model"] == "claude-fable-5"


def test_adapter_streams_output_text_deltas() -> None:
    fake = FakeAnthropic([[_text_block(ANSWER_JSON)]])
    adapter = ClaudeResponsesClient(client=fake, model="claude-opus-4-8")
    deltas: list[str] = []

    response = adapter.responses.create(
        model="claude-opus-4-8",
        instructions="System prompt.",
        input=[{"role": "user", "content": "Question?"}],
        text=None,
        on_output_text_delta=deltas.append,
    )

    assert "".join(deltas) == ANSWER_JSON
    assert len(deltas) > 1
    assert response.output_text == ANSWER_JSON


def test_agent_streams_answer_deltas_via_claude_provider() -> None:
    fake = FakeAnthropic([[_text_block(ANSWER_JSON)]])
    adapter = ClaudeResponsesClient(client=fake, model="claude-opus-4-8")
    agent = StatsAgent(
        _settings(openai_api_key=None),
        AgentServiceFakeRepository(),
        claude_client=adapter,
    )
    events: list[dict] = []

    payload = agent.answer(
        "How is Tyrese Maxey trending?",
        provider="claude",
        progress_callback=events.append,
    )

    deltas = [e["delta"] for e in events if e["type"] == "answer_delta"]
    assert "".join(deltas) == "Answered from Claude."
    assert payload["answer_streamed"] is True
    assert payload["answer"] == "Answered from Claude."


def test_agent_answers_via_claude_provider() -> None:
    fake = FakeAnthropic([[_text_block(ANSWER_JSON)]])
    adapter = ClaudeResponsesClient(client=fake, model="claude-opus-4-8")
    agent = StatsAgent(
        _settings(openai_api_key=None),
        AgentServiceFakeRepository(),
        claude_client=adapter,
    )

    payload = agent.answer("How is Tyrese Maxey trending?", provider="claude")

    assert payload["answer"] == "Answered from Claude."
    assert fake.messages.calls > 0
    assert all(kwargs["model"] == "claude-opus-4-8" for kwargs in fake.messages.kwargs)
    # The OpenAI client was never needed for the Claude path.
    assert agent.client is None


def test_agent_answers_via_selected_claude_model() -> None:
    fake = FakeAnthropic([[_text_block(ANSWER_JSON)]])
    adapter = ClaudeResponsesClient(client=fake, model="claude-opus-4-8")
    agent = StatsAgent(
        _settings(openai_api_key=None),
        AgentServiceFakeRepository(),
        claude_client=adapter,
    )

    payload = agent.answer(
        "How is Tyrese Maxey trending?",
        provider="claude",
        model="claude-fable-5",
    )

    assert payload["answer"] == "Answered from Claude."
    assert fake.messages.calls > 0
    assert all(kwargs["model"] == "claude-fable-5" for kwargs in fake.messages.kwargs)


def test_openai_provider_still_requires_openai_key() -> None:
    agent = StatsAgent(
        _settings(openai_api_key=None),
        AgentServiceFakeRepository(),
        claude_client=ClaudeResponsesClient(
            client=FakeAnthropic([[_text_block(ANSWER_JSON)]]),
            model="claude-opus-4-8",
        ),
    )

    try:
        agent.answer("How is Tyrese Maxey trending?", provider="openai")
    except AgentDisabledError as exc:
        assert "OPENAI_API_KEY" in str(exc)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("Expected the OpenAI path to require its key")


def test_claude_provider_requires_anthropic_key() -> None:
    agent = StatsAgent(_settings(), AgentServiceFakeRepository())

    try:
        agent.answer("How is Tyrese Maxey trending?", provider="claude")
    except AgentDisabledError as exc:
        assert "ANTHROPIC_API_KEY" in str(exc)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("Expected the Claude path to require its key")


def test_unknown_provider_is_rejected() -> None:
    agent = StatsAgent(_settings(), AgentServiceFakeRepository())

    try:
        agent.answer("How is Tyrese Maxey trending?", provider="gemini")
    except ValueError as exc:
        assert "provider" in str(exc)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("Expected an unknown provider to be rejected")
