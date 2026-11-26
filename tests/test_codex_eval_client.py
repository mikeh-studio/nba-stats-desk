import json
import signal
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, Mock

import pytest
from scripts.ask_eval import codex_client
from scripts.ask_eval.budget import BudgetClient, CallBudget
from scripts.ask_eval.cassettes import (
    CassetteClient,
    CassetteStore,
    call_context,
    response_data,
)
from scripts.ask_eval.codex_client import CodexCLIResponsesClient

SCHEMA = {
    "type": "object",
    "properties": {"scope": {"type": ["string", "null"]}},
    "required": ["scope"],
    "additionalProperties": False,
}
REQUEST = {
    "model": "gpt-6-luna",
    "instructions": "Return the supplied scope.",
    "input": [
        {"role": "developer", "content": {"scope": "regular season"}},
        {"role": "user", "content": [{"type": "input_text", "text": "Which scope?"}]},
    ],
    "text": {"format": {"type": "json_schema", "name": "plan", "schema": SCHEMA}},
    "max_output_tokens": 100,
    "store": False,
}
OUTPUT = '{"scope":"regular season"}'
EVENTS = [
    {"type": "thread.started", "thread_id": "test"},
    {"type": "item.completed", "item": {"type": "reasoning"}},
    {"type": "item.completed", "item": {"type": "agent_message"}},
    {"type": "turn.completed", "usage": {"input_tokens": 21, "output_tokens": 6}},
]


@pytest.fixture
def cli(monkeypatch):
    def install(*, events=EVENTS, output=OUTPUT, exit_code=0):
        process = MagicMock(pid=123, returncode=exit_code)
        process.__enter__.return_value = process

        def launch(command, **options):
            if "--output-schema" in command:
                schema_path = Path(command[command.index("--output-schema") + 1])
                assert json.loads(schema_path.read_text()) == SCHEMA
                assert schema_path.parent == Path(options["cwd"])
            if output is not None:
                output_path = Path(command[command.index("--output-last-message") + 1])
                output_path.write_text(output + "\n")
            return process

        process.communicate.return_value = (
            "\n".join(json.dumps(event) for event in events),
            "provider diagnostics must stay private",
        )
        popen = Mock(side_effect=launch)
        monkeypatch.setattr(codex_client.subprocess, "Popen", popen)
        return popen, process

    return install


def test_isolated_subprocess_exact_schema_and_usage(cli, monkeypatch):
    allowed = {
        key: f"test-{key}"
        for key in (
            "PATH",
            "HOME",
            "USER",
            "LOGNAME",
            "SHELL",
            "TMPDIR",
            "LANG",
            "LC_ALL",
            "CODEX_HOME",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
        )
    }
    for key, value in allowed.items():
        monkeypatch.setenv(key, value)
    for key in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "AWS_SECRET_ACCESS_KEY",
    ):
        monkeypatch.setenv(key, "must-not-reach-cli")

    popen, process = cli()
    client = CodexCLIResponsesClient(timeout=90)
    clone = client.with_options(timeout=60, max_retries=0)
    assert clone is not client and client.timeout == 90
    assert clone.responses is clone
    result = clone.responses.create(**REQUEST)
    (command,), options = popen.call_args
    workspace = Path(options["cwd"])
    assert command == [
        "codex",
        "exec",
        "--ignore-user-config",
        "--ephemeral",
        "--skip-git-repo-check",
        "-C",
        str(workspace),
        "-s",
        "read-only",
        "-m",
        "gpt-6-luna",
        "-c",
        'model_reasoning_effort="low"',
        "-c",
        'web_search="disabled"',
        "-c",
        "features.shell_tool=false",
        "--json",
        "--output-schema",
        str(workspace / "schema.json"),
        "--output-last-message",
        str(workspace / "last-message"),
        "-",
    ]
    assert options["start_new_session"] is True
    assert options["env"] == allowed
    assert not workspace.exists()
    (prompt,), options = process.communicate.call_args
    assert options == {"timeout": 60}
    instructions, messages = prompt.split("Input messages (JSON):\n")
    assert instructions.startswith(REQUEST["instructions"])
    assert "100 tokens" in instructions
    assert json.loads(messages) == REQUEST["input"]
    assert response_data(result) == {
        "id": "codex-cli",
        "model": "gpt-6-luna",
        "output": [],
        "output_text": OUTPUT,
        "usage": {"input_tokens": 21, "output_tokens": 6, "total_tokens": 27},
    }


@pytest.mark.parametrize(
    "input", ["Which scope?", {"role": "user", "content": "Which scope?"}]
)
def test_plain_input_without_schema(cli, input):
    usage = {"input_tokens": 21, "output_tokens": 6, "total_tokens": 29}
    popen, process = cli(events=[{"type": "turn.completed", "usage": usage}])
    response = CodexCLIResponsesClient().create(
        model="another-cli-model", instructions="Answer briefly.", input=input
    )
    assert "--output-schema" not in popen.call_args.args[0]
    prompt = process.communicate.call_args.args[0]
    assert json.loads(prompt.split("Input messages (JSON):\n")[1]) == [
        {"role": "user", "content": "Which scope?"}
    ]
    assert vars(response.usage) == usage


def test_strict_format_requires_schema_before_starting(cli):
    popen, _ = cli()
    with pytest.raises(ValueError, match="JSON schema"):
        CodexCLIResponsesClient().create(
            **{**REQUEST, "text": {"format": {"type": "json_schema"}}}
        )
    popen.assert_not_called()


@pytest.mark.parametrize("kind", ["item.started", "item.completed"])
def test_rejects_tools(cli, kind):
    cli(events=[{"type": kind, "item": {"type": "command_execution"}}, *EVENTS])
    with pytest.raises(RuntimeError, match="tool or unexpected item"):
        CodexCLIResponsesClient().create(**REQUEST)


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"events": EVENTS[:-1]}, "event stream or usage"),
        ({"output": None}, "no final message"),
        ({"exit_code": 1}, "exited with status 1"),
    ],
)
def test_rejects_unsuccessful_turn(cli, options, message):
    cli(**options)
    with pytest.raises(RuntimeError, match=message):
        CodexCLIResponsesClient().create(**REQUEST)


def test_timeout_kills_group_and_reaps_process(cli, monkeypatch):
    popen, process = cli()
    process.communicate.side_effect = [subprocess.TimeoutExpired("codex", 1), ("", "")]
    killpg = Mock()
    monkeypatch.setattr(codex_client.os, "killpg", killpg)
    with pytest.raises(TimeoutError, match="timed out"):
        CodexCLIResponsesClient().with_options(timeout=60).create(**REQUEST, timeout=1)
    assert process.communicate.call_args_list[0].kwargs == {"timeout": 1}
    assert process.communicate.call_args_list[1].args == ()
    killpg.assert_called_once_with(process.pid, signal.SIGKILL)
    assert not Path(popen.call_args.kwargs["cwd"]).exists()


def test_cassette_round_trip_through_budget(cli, tmp_path):
    popen, _ = cli()
    binding = {
        "provider": "openai",
        "model": REQUEST["model"],
        "cases": {"plan": "hash"},
        "transport": "codex_cli",
    }
    path = tmp_path / "cassette"
    store = CassetteStore(path, "record", binding)
    budget = CallBudget(1)
    client = CassetteClient(
        store, "openai", BudgetClient(CodexCLIResponsesClient(), budget)
    )
    token = call_context.set(
        {"case": "plan", "turn": 0, "transport": "json", "settings_sha256": "settings"}
    )
    try:
        expected = client.with_options(timeout=30, max_retries=0).responses.create(
            **REQUEST
        )
        assert store.terminal()["failed_attempts"] == 0
        replay = CassetteStore(path, "replay", binding)
        actual = CassetteClient(replay, "openai").responses.create(**REQUEST)
    finally:
        call_context.reset(token)
    assert response_data(actual) == response_data(expected)
    assert budget.calls == 1
    assert budget.records[0]["usage"]["total_tokens"] == 27
    terminal = replay.terminal()
    assert terminal["consumed"] == terminal["calls"] == 1
    assert terminal["errors"] == []
    popen.assert_called_once()
