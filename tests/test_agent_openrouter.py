"""Offline OpenRouter schema, transport, continuation and credential boundaries."""

from unittest.mock import Mock

import pytest
from app.agent.openrouter_client import OpenRouterResponsesClient
from app.agent.service import AgentDisabledError, StatsAgent
from jsonschema import ValidationError
from openai.types.chat import ChatCompletion
from tests.test_agent_service import AgentServiceFakeRepository, _settings
from tests.test_api import FakeOpenAIClient, _test_settings, build_client

MODEL = "deepseek/deepseek-v4.1-flash"
FORMAT = {
    "format": {
        "type": "json_schema",
        "name": "choice",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {"choice": {"type": "string", "enum": ["pts"]}},
            "required": ["choice"],
            "additionalProperties": False,
        },
    }
}


@pytest.fixture(autouse=True)
def isolated_rate_limit_store(monkeypatch):
    from app import rate_limit

    monkeypatch.setattr(
        rate_limit, "_memory_store", rate_limit.InMemoryRateLimitStore()
    )


def completion(content='{"choice":"pts"}', calls=None, finish="stop"):
    return ChatCompletion.model_validate(
        dict(
            id="test",
            created=1,
            model=MODEL,
            object="chat.completion",
            choices=[
                dict(
                    index=0,
                    finish_reason=finish,
                    message=dict(
                        role="assistant",
                        content=content,
                        tool_calls=calls,
                        reasoning_details=[
                            {"type": "reasoning.text", "text": "private reasoning"}
                        ],
                    ),
                )
            ],
            usage=dict(prompt_tokens=100, completion_tokens=20, total_tokens=120),
        )
    )


def test_schema_provider_routing_and_usage():
    sdk = Mock()
    sdk.chat.completions.create.return_value = completion()
    client = OpenRouterResponsesClient(sdk)
    result = client.create(
        model=MODEL,
        instructions="Rules",
        input=[
            {"role": "developer", "content": "Scope"},
            {"role": "user", "content": "Question"},
        ],
        text=FORMAT,
    )
    args = sdk.chat.completions.create.call_args.kwargs
    assert args["model"] == MODEL
    assert args["extra_body"] == {"provider": {"require_parameters": True}}
    assert args["response_format"]["json_schema"] == {
        k: v for k, v in FORMAT["format"].items() if k != "type"
    }
    assert args["messages"][1] == {"role": "system", "content": "Scope"}
    assert result.output_text == '{"choice":"pts"}'
    assert result.usage.input_tokens == 100
    assert result.usage.output_tokens == 20
    client.with_options(timeout=9)
    sdk.with_options.assert_called_once_with(timeout=9)


def test_multiple_tool_calls_and_reasoning_survive_roundtrip():
    calls = [
        dict(
            id=str(i), type="function", function=dict(name="get_stats", arguments="{}")
        )
        for i in (1, 2)
    ]
    sdk = Mock()
    sdk.chat.completions.create.side_effect = [
        completion(None, calls, "tool_calls"),
        completion(),
    ]
    client = OpenRouterResponsesClient(sdk)
    tool = {
        "type": "function",
        "name": "get_stats",
        "description": "Read statistics",
        "parameters": {"type": "object", "properties": {}},
    }
    first = client.create(
        model=MODEL, instructions="Rules", input=[], text=FORMAT, tools=[tool]
    )
    results = [
        {"type": "function_call_output", "call_id": str(i), "output": "{}"}
        for i in (1, 2)
    ]
    client.create(
        model=MODEL, instructions="Rules", input=[*first.output, *results], text=FORMAT
    )
    messages = sdk.chat.completions.create.call_args.kwargs["messages"]
    assert messages[1]["tool_calls"] == calls
    assert messages[1]["reasoning_details"][0]["text"] == "private reasoning"
    assert [m["tool_call_id"] for m in messages[2:]] == ["1", "2"]
    assert sdk.chat.completions.create.call_args_list[0].kwargs["tools"] == [
        {"type": "function", "function": {k: v for k, v in tool.items() if k != "type"}}
    ]


@pytest.mark.parametrize(
    "content,finish,error",
    [
        ("not JSON", "stop", ValueError),
        ('{"choice":"invented"}', "stop", ValidationError),
        ('{"choice":"pts"}', "length", ValueError),
    ],
)
def test_invalid_or_truncated_outputs_fail_closed(content, finish, error):
    sdk = Mock()
    sdk.chat.completions.create.return_value = completion(content, finish=finish)
    with pytest.raises(error):
        OpenRouterResponsesClient(sdk).create(
            model=MODEL, instructions="Rules", input=[], text=FORMAT
        )


def test_missing_key_does_not_use_openai_client():
    agent = StatsAgent(_settings(), AgentServiceFakeRepository(), client=Mock())
    with pytest.raises(AgentDisabledError, match="OPENROUTER_API_KEY"):
        agent.answer("How is LeBron doing?", provider="openrouter", model=MODEL)
    agent.client.responses.create.assert_not_called()


@pytest.mark.parametrize("model", [MODEL, "qwen/qwen3-235b-a22b-2507"])
@pytest.mark.parametrize("endpoint", ["/api/agent/ask", "/api/agent/ask/stream"])
def test_http_routes_keep_selected_openrouter_model(monkeypatch, model, endpoint):
    fake = FakeOpenAIClient()
    monkeypatch.setattr(StatsAgent, "_get_openrouter_client", lambda self: fake)
    client = build_client(settings=_test_settings())
    response = client.post(
        endpoint,
        json={
            "question": "How is Tyrese Maxey trending?",
            "provider": "openrouter",
            "model": model,
        },
    )
    assert response.status_code == 200
    assert fake.responses.calls >= 1
    assert all(c["model"] == model for c in fake.responses.kwargs)
    if endpoint.endswith("stream"):
        assert "event: error" not in response.text


def test_selector_and_wrong_provider_model_rejection():
    client = build_client(settings=_test_settings())
    page = client.get("/ask")
    assert 'value="openrouter"' in page.text
    assert MODEL in page.text and "qwen/qwen3-235b-a22b-2507" in page.text
    bad = client.post(
        "/api/agent/ask",
        json={"question": "Stats?", "provider": "openrouter", "model": "gpt-5.6-luna"},
    )
    assert bad.status_code == 400


def test_client_uses_only_openrouter_key_and_endpoint():
    from dataclasses import replace
    from unittest.mock import patch

    settings = replace(_settings(), openrouter_api_key="router-test-key")
    with patch("openai.OpenAI") as sdk:
        agent = StatsAgent(settings, AgentServiceFakeRepository())
        adapter = agent._get_client("openrouter")
        sdk.assert_called_once_with(
            api_key="router-test-key",
            base_url="https://openrouter.ai/api/v1",
            max_retries=0,
        )
        assert adapter is agent._get_client("openrouter")


def test_api_missing_key_is_clear_and_env_defaults(monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("OPENROUTER_API_KEY", "fixture-key")
    monkeypatch.setenv("OPENROUTER_AGENT_MODEL", "qwen/qwen3-235b-a22b-2507")
    settings = get_settings()
    assert settings.openrouter_api_key == "fixture-key"
    assert settings.openrouter_agent_model == "qwen/qwen3-235b-a22b-2507"
    client = build_client(settings=_test_settings(openrouter_api_key=None))
    response = client.post(
        "/api/agent/ask",
        json={"question": "LeBron stats?", "provider": "openrouter", "model": MODEL},
    )
    assert response.status_code == 503
    assert "OPENROUTER_API_KEY" in response.text
