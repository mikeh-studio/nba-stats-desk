"""Isolated, metered Codex CLI execution for offline evaluation."""

import json
import os
import signal
import subprocess
import tempfile
from datetime import datetime, timezone

from app.agent.reporting_evidence import digest
from scripts.reporting_artifacts import read, save


def codex_call(run_dir, stage, model, effort, prompt, schema):
    """One batch per stage amortizes CLI overhead; no automatic paid retries."""
    schema_path = run_dir / f"{stage}-schema.json"
    prompt_path = run_dir / f"{stage}-prompt.txt"
    output_path = run_dir / f"{stage}.json"
    if any(
        (run_dir / f"{stage}{suffix}").exists()
        for suffix in (".json", "-events.jsonl", "-prompt.txt")
    ):
        raise ValueError(
            "Stage already attempted; create a new run to preserve its evidence"
        )
    save(schema_path, schema)
    prompt_path.write_text(prompt)
    with tempfile.TemporaryDirectory(prefix="nba-evidence-codex-") as workspace:
        command = [
            "codex",
            "exec",
            "--ignore-user-config",
            "--ephemeral",
            "--skip-git-repo-check",
            "-C",
            workspace,
            "-s",
            "read-only",
            "-m",
            model,
            "-c",
            f'model_reasoning_effort="{effort}"',
            "-c",
            'web_search="disabled"',
            "-c",
            "features.shell_tool=false",
            "--json",
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(output_path),
            "-",
        ]
        # Do not pass application/provider credentials to evaluation subprocesses.
        env = {
            k: v
            for k, v in os.environ.items()
            if k
            in {
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
            }
        }
        with (
            (run_dir / f"{stage}-events.jsonl").open("x") as events,
            (run_dir / f"{stage}-stderr.log").open("x") as stderr,
        ):
            started = datetime.now(timezone.utc)
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=events,
                stderr=stderr,
                text=True,
                env=env,
                start_new_session=True,
            )
            try:
                process.communicate(prompt, timeout=600)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=10)
                raise RuntimeError(
                    "Codex timed out; partial artifacts preserved"
                ) from None
    records = [
        json.loads(line)
        for line in (run_dir / f"{stage}-events.jsonl").read_text().splitlines()
        if line.strip()
    ]
    usage = [r["usage"] for r in records if r.get("type") == "turn.completed"]
    tool_items = [
        r
        for r in records
        if r.get("type") == "item.completed"
        and r.get("item", {}).get("type") not in {"agent_message", "reasoning"}
    ]
    metadata = {
        "requested_model": model,
        "reasoning_effort": effort,
        "started_at": started.isoformat(),
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "exit_code": process.returncode,
        "usage": usage,
        "prompt_sha256": digest(prompt),
        "tool_items": tool_items,
        "command": command,
        "batch_mode": True,
        "model_verification": "Explicit CLI model flag; no model substitution. Event stream may not echo resolved model.",
    }
    save(run_dir / f"{stage}-metadata.json", metadata)
    if process.returncode or not usage or tool_items or not output_path.exists():
        raise RuntimeError(f"Invalid {stage} run; inspect saved CLI logs")
    return read(output_path)
