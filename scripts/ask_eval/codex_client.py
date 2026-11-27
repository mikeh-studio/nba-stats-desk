"""Evaluation-only Responses adapter for the authenticated Codex CLI."""

import json
import os
import signal
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

_ENV_KEYS = (
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


def _prompt(instructions, messages, max_output_tokens):
    if isinstance(messages, str):
        messages = [{"role": "user", "content": messages}]
    elif isinstance(messages, dict):
        messages = [messages]
    sections = [instructions]
    if max_output_tokens is not None:
        sections.append(f"Keep the final response within {max_output_tokens} tokens.")
    sections.append(
        "Input messages (JSON):\n"
        + json.dumps(messages, ensure_ascii=False, sort_keys=True)
    )
    return "\n\n".join(sections)


def _usage(events):
    completed = []
    try:
        for line in events.splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            kind = event["type"]
            if kind.startswith("item.") and event["item"]["type"] not in {
                "agent_message",
                "reasoning",
            }:
                raise RuntimeError("Codex CLI emitted a tool or unexpected item")
            if kind == "turn.completed":
                completed.append(event["usage"])
            if kind == "turn.failed":
                raise RuntimeError("Codex CLI turn failed")
        (usage,) = completed
        input_tokens = usage["input_tokens"]
        output_tokens = usage["output_tokens"]
        total_tokens = usage.get("total_tokens", input_tokens + output_tokens)
        if any(
            type(count) is not int or count < 0
            for count in (input_tokens, output_tokens, total_tokens)
        ):
            raise ValueError("Invalid usage count")
    except (AttributeError, KeyError, TypeError, ValueError):
        raise RuntimeError("Invalid Codex CLI event stream or usage") from None
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
    )


class CodexCLIResponsesClient:
    def __init__(self, timeout=90.0):
        self.responses = self
        self.timeout = timeout

    def with_options(self, *, timeout=None, **kwargs):
        return type(self)(self.timeout if timeout is None else timeout)

    def create(
        self,
        *,
        model,
        instructions,
        input,
        text=None,
        max_output_tokens=None,
        timeout=None,
        **kwargs,
    ):
        response_format = (text or {}).get("format", {})
        schema = response_format.get("schema")
        if response_format.get("type") == "json_schema" and not isinstance(
            schema, dict
        ):
            raise ValueError("A JSON schema object is required for json_schema")
        if kwargs.get("tools"):
            raise ValueError("Codex CLI evaluation does not support tools")
        prompt = _prompt(instructions, input, max_output_tokens)
        env = {key: os.environ[key] for key in _ENV_KEYS if key in os.environ}
        with tempfile.TemporaryDirectory(prefix="nba-ask-codex-") as directory:
            output = Path(directory) / "last-message"
            command = [
                "codex",
                "exec",
                "--ignore-user-config",
                "--ephemeral",
                "--skip-git-repo-check",
                "-C",
                directory,
                "-s",
                "read-only",
                "-m",
                model,
                "-c",
                'model_reasoning_effort="low"',
                "-c",
                'web_search="disabled"',
                "-c",
                "features.shell_tool=false",
                "--json",
            ]
            if schema is not None:
                schema_path = Path(directory) / "schema.json"
                schema_path.write_text(json.dumps(schema), encoding="utf-8")
                command.extend(("--output-schema", str(schema_path)))
            command.extend(("--output-last-message", str(output), "-"))
            with subprocess.Popen(
                command,
                cwd=directory,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding="utf-8",
                start_new_session=True,
            ) as process:
                try:
                    events, _ = process.communicate(
                        prompt, timeout=self.timeout if timeout is None else timeout
                    )
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.communicate()
                    raise TimeoutError("Codex CLI response timed out") from None
                if process.returncode:
                    raise RuntimeError(
                        f"Codex CLI exited with status {process.returncode}"
                    )
            usage = _usage(events)
            output_text = (
                output.read_text(encoding="utf-8").strip() if output.is_file() else ""
            )
            if not output_text:
                raise RuntimeError("Codex CLI produced no final message")
        return SimpleNamespace(
            id="codex-cli",
            model=model,
            output=[],
            output_text=output_text,
            usage=usage,
        )
