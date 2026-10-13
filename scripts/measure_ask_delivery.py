#!/usr/bin/env python3
"""Measure incremental SSE delivery against public frozen Ask inputs, offline."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from collections import deque
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def code_digest() -> str:
    return hashlib.sha256(
        b"".join(
            str(path.relative_to(ROOT)).encode() + path.read_bytes()
            for directory in (ROOT / "app", ROOT / "scripts/ask_eval")
            for path in sorted(directory.rglob("*.py"))
        )
    ).hexdigest()


def frames(text: str) -> list[dict]:
    """Retain size metadata only; final payloads and questions are not exported."""
    result = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        if not block:
            continue
        lines = block.splitlines()
        data = [line[6:] for line in lines if line.startswith("data: ")]
        if not data:
            continue
        event = json.loads("\n".join(data))
        event_type = next(
            (line[7:] for line in lines if line.startswith("event: ")), "progress"
        )
        wire = block + "\n\n"
        result.append(
            {
                "type": event_type,
                "wire_bytes": len(wire.encode("utf-8")),
                "string_heap_bytes": sys.getsizeof(wire),
                "bytes_heap_bytes": sys.getsizeof(wire.encode("utf-8")),
                "delta_chars": len(event.get("delta", "")),
            }
        )
    return result


def measure(url: str, cases: list[dict]) -> list[dict]:
    records = []
    with httpx.Client(base_url=url, timeout=60) as client:
        for case in cases:
            conversation = "capacity-" + uuid.uuid4().hex
            for index, turn in enumerate(case["turns"]):
                if turn.get("reset_context"):
                    client.post("/__eval/reset-conversations").raise_for_status()
                body = {
                    "question": turn["question"],
                    "conversation_id": conversation,
                    "provider": "openai",
                }
                if turn.get("client_context"):
                    body["previous_context"] = turn["client_context"]
                response = client.post(
                    "/api/agent/ask/stream?season=2024-25",
                    json=body,
                    headers={
                        "X-Evaluation-Case": case["id"],
                        "X-Evaluation-Turn": str(index),
                    },
                )
                response.raise_for_status()
                captured = frames(response.text)
                terminals = [f for f in captured if f["type"] in {"final", "error"}]
                if len(terminals) != 1 or terminals[0]["type"] != "final":
                    raise RuntimeError(
                        f"Missing successful final: {case['id']}/{index}"
                    )
                incremental = [
                    f for f in captured if f["type"] not in {"meta", "final", "error"}
                ]
                records.append(
                    {
                        "case_id": case["id"],
                        "turn": index,
                        "incremental_events": len(incremental),
                        "incremental_bytes": sum(f["wire_bytes"] for f in incremental),
                        "incremental_string_heap_bytes": sum(
                            f["string_heap_bytes"] for f in incremental
                        ),
                        "incremental_bytes_heap_bytes": sum(
                            f["bytes_heap_bytes"] for f in incremental
                        ),
                        "largest_event_bytes": max(
                            (f["wire_bytes"] for f in incremental), default=0
                        ),
                        "terminal_bytes": terminals[0]["wire_bytes"],
                        "frames": captured,
                    }
                )
    return records


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    input_dir = ROOT / "tests/fixtures/ask"
    cases_path = input_dir / "cases.json"
    cases = json.loads(cases_path.read_text())["cases"]
    initial_code_digest = code_digest()
    # Explicit public inputs and controlled model; inherited live flags and
    # private input paths must never change the scope of this command.
    env = {
        **os.environ,
        "ASK_EVAL_LIVE": "0",
        "ASK_EVAL_MAX_CALLS": "0",
        "ASK_EVAL_CODE_ROOT": str(ROOT),
        "ASK_EVAL_INPUT_DIR": str(input_dir),
    }
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    with (args.output / "server.log").open("x") as log:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "scripts.ask_eval.server:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--no-proxy-headers",
            ],
            cwd=ROOT,
            env=env,
            stdout=log,
            stderr=log,
        )
        try:
            startup_deadline = time.monotonic() + 30
            while time.monotonic() < startup_deadline:
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        break
                except OSError:
                    if proc.poll() is not None:
                        raise RuntimeError("Fixture server failed; see server.log")
                    time.sleep(0.1)
            else:
                raise RuntimeError("Fixture server startup timed out")
            records = measure(url, cases)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
    byte_counts = sorted(r["incremental_bytes"] for r in records)
    max_events = max(r["incremental_events"] for r in records)
    summary = {
        "execution_mode": "offline_public_fixtures",
        "cases": len(cases),
        "sse_requests": len(records),
        "python_version": sys.version.split()[0],
        "code_sha256_at_start": initial_code_digest,
        "code_unchanged_during_measurement": initial_code_digest == code_digest(),
        "cases_sha256": hashlib.sha256(cases_path.read_bytes()).hexdigest(),
        "source_sha256": hashlib.sha256(
            (input_dir / "source.json").read_bytes()
        ).hexdigest(),
        "incremental_bytes_max": max(byte_counts),
        "incremental_events_max": max_events,
        "largest_event_bytes": max(r["largest_event_bytes"] for r in records),
        "terminal_bytes_max": max(r["terminal_bytes"] for r in records),
        "incremental_string_heap_bytes_max": max(
            r["incremental_string_heap_bytes"] for r in records
        ),
        "incremental_bytes_heap_bytes_max": max(
            r["incremental_bytes_heap_bytes"] for r in records
        ),
        "deque_container_bytes_at_max_events": sys.getsizeof(deque([""] * max_events)),
        "incremental_bytes_percentiles": {
            str(p): byte_counts[round((len(byte_counts) - 1) * p / 100)]
            for p in (50, 90, 95, 99)
        },
        "limitations": [
            "Complete emitted interim frames are counted, not peak retained backlog.",
            "Overflow in a bounded server may suppress interim frames; compare a baseline capture when assessing defaults.",
            "Final payload and meta are excluded from the incremental quota.",
            "Heap sizes cover Python strings/bytes and deque only, not total request memory.",
            "Controlled fixture output is not production/provider output or latency.",
            "This is a delivery measurement, not a factual-answer evaluation.",
        ],
    }
    (args.output / "captures.json").write_text(json.dumps(records, indent=2) + "\n")
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
