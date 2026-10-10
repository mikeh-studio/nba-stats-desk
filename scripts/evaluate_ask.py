#!/usr/bin/env python3
"""Run frozen Ask cases through real loopback HTTP endpoints; save immutable runs."""

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
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.ask_eval.fixtures import load_inputs  # noqa: E402
from scripts.ask_eval.grading import canonical, grade  # noqa: E402


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def evaluate(
    url,
    document,
    provider="openai",
    model=None,
    *,
    live=False,
    progress=None,
    season="2024-25",
):
    results = []
    previous_calls = 0
    with httpx.Client(base_url=url, timeout=60) as client:
        for case in document["cases"]:
            modes = {}
            for mode in ("json", "sse"):
                conversation = "eval-" + uuid.uuid4().hex
                turns = []
                for index, turn in enumerate(case["turns"]):
                    started = time.monotonic()
                    body = {
                        "question": turn["question"],
                        "conversation_id": conversation,
                        "provider": provider,
                    }
                    if model:
                        body["model"] = model
                    headers = {
                        "X-Evaluation-Case": case["id"],
                        "X-Evaluation-Turn": str(index),
                    }
                    endpoint = "/api/agent/ask" + ("/stream" if mode == "sse" else "")
                    events = []
                    status = 0
                    try:
                        response = client.post(
                            endpoint + "?season=" + season, json=body, headers=headers
                        )
                        status = response.status_code
                        if mode == "json":
                            payload = response.json()
                        else:
                            for block in response.text.replace("\r\n", "\n").split(
                                "\n\n"
                            ):
                                data = [
                                    line[6:]
                                    for line in block.splitlines()
                                    if line.startswith("data: ")
                                ]
                                if data:
                                    events.append(json.loads("\n".join(data)))
                            payload = next(
                                (
                                    e["payload"]
                                    for e in events
                                    if e.get("type") == "final"
                                ),
                                {"stream_error": events},
                            )
                    except (httpx.HTTPError, ValueError) as exc:
                        payload = {"evaluation_error": type(exc).__name__}
                    usage = []
                    if live:
                        metadata = client.get("/__eval/metadata").json()
                        usage = metadata.get("model_usage", [])[previous_calls:]
                        previous_calls = len(metadata.get("model_usage", []))
                    assertions = grade(payload, turn["checks"])
                    if status != 200 or (
                        mode == "sse"
                        and not any(e.get("type") == "final" for e in events)
                    ):
                        assertions.append(
                            {"path": "HTTP/terminal", "passed": False, "actual": status}
                        )
                    turns.append(
                        {
                            "question": turn["question"],
                            "payload": payload,
                            "checks": assertions,
                            "passed": all(c["passed"] for c in assertions),
                            "latency_ms": round((time.monotonic() - started) * 1000),
                            "http_status": status,
                            "model_usage": usage,
                        }
                    )
                modes[mode] = turns
            parity = all(
                (
                    all(check["passed"] for check in a["checks"] + b["checks"])
                    if live
                    else canonical(a["payload"]) == canonical(b["payload"])
                )
                for a, b in zip(modes["json"], modes["sse"], strict=True)
            )
            results.append(
                {
                    "id": case["id"],
                    "family": case["family"],
                    "passed": parity
                    and all(t["passed"] for turns in modes.values() for t in turns),
                    "parity": parity,
                    "modes": modes,
                }
            )
            if progress:
                progress.write(json.dumps(results[-1]) + "\n")
                progress.flush()
            print(
                f"{case['id']}: {'PASS' if results[-1]['passed'] else 'FAIL'}",
                flush=True,
            )
    return results


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument(
        "--input-dir",
        type=Path,
        help="Private directory containing source.json and cases.json",
    )
    p.add_argument("--code-root", type=Path, default=ROOT)
    p.add_argument("--label", default="candidate")
    p.add_argument(
        "--live",
        action="store_true",
        help="Explicit opt-in to paid/provider model calls against frozen sources",
    )
    p.add_argument(
        "--max-model-calls",
        type=int,
        help="Required hard call budget for --live, including both transports",
    )
    p.add_argument(
        "--provider", choices=["openai", "claude", "openrouter"], default="openai"
    )
    p.add_argument("--model")
    p.add_argument("--case", action="append")
    p.add_argument("--max-requests", type=int, default=160)
    args = p.parse_args()
    source, document = load_inputs(ROOT, args.input_dir)
    input_dir = args.input_dir or ROOT / "tests/fixtures/ask"
    if args.case:
        document["cases"] = [c for c in document["cases"] if c["id"] in args.case]
        if len(document["cases"]) != len(set(args.case)):
            p.error("Unknown case ID")
    requests = 2 * sum(len(c["turns"]) for c in document["cases"])
    if not document["cases"] or requests > args.max_requests:
        p.error("Empty selection or request budget exceeded")
    if not args.live and (args.provider != "openai" or args.model):
        p.error(
            "Controlled runs do not score real providers/models; use --live with an explicit model and call budget"
        )
    if args.live and (
        not args.model
        or not args.max_model_calls
        or not 0 < args.max_model_calls <= 500
    ):
        p.error("--live requires --model and --max-model-calls between 1 and 500")
    if args.live and not args.case and not args.input_dir:
        p.error(
            "Select explicit cases for a live run; controlled invalid-plan injections are not real-provider cases"
        )
    if args.live and any(c["id"] == "invalid_plan" for c in document["cases"]):
        p.error(
            "invalid_plan tests a controlled malicious planner response, not a real model"
        )
    args.output.mkdir(parents=True, exist_ok=False)
    proc = None
    try:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        env = {
            **os.environ,
            "ASK_EVAL_CODE_ROOT": str(args.code_root.resolve()),
            "PYTHONPATH": str(ROOT),
            "ASK_EVAL_LIVE": "1" if args.live else "0",
            "ASK_EVAL_MAX_CALLS": str(args.max_model_calls or 0),
            "ASK_EVAL_INPUT_DIR": str(input_dir.resolve()),
            "ASK_EVAL_CALL_LOG": str((args.output / "model-calls.jsonl").resolve()),
        }
        log = (args.output / "server.log").open("w")
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
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                if proc.poll() is not None:
                    raise RuntimeError("Fixture server failed; see server.log")
                time.sleep(0.1)
        else:
            raise RuntimeError("Fixture server startup timed out")
        with (args.output / "progress.jsonl").open("x") as progress:
            results = evaluate(
                url,
                document,
                args.provider,
                args.model,
                live=args.live,
                progress=progress,
                season=source.get("season", "2024-25"),
            )
        with httpx.Client(base_url=url) as client:
            runtime = client.get("/__eval/metadata").json()
        revision = (
            subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=args.code_root,
                capture_output=True,
                text=True,
            ).stdout.strip()
            or "archived-baseline"
        )
        report = {
            "schema_version": 1,
            "label": args.label,
            "revision": revision,
            "code_sha256": hashlib.sha256(
                b"".join(
                    str(f.relative_to(args.code_root)).encode() + f.read_bytes()
                    for f in sorted((args.code_root / "app").rglob("*"))
                    if f.is_file()
                    and f.suffix in {".py", ".js", ".html", ".yml", ".json"}
                )
            ).hexdigest(),
            "case_sha256": digest(input_dir / "cases.json"),
            "source_sha256": digest(input_dir / "source.json"),
            "grader_sha256": digest(ROOT / "scripts/ask_eval/grading.py"),
            "execution_mode": "live_planner_"
            + source.get("source_kind", "synthetic")
            + "_http"
            if args.live
            else "controlled_planner_frozen_source_http",
            "provider": args.provider if args.live else "controlled",
            "model": args.model if args.live else "predeclared-plan",
            "human_reviewed": False,
            "source_kind": source.get("source_kind", "synthetic"),
            "source_description": source.get("description", "Synthetic evidence"),
            "runtime": runtime,
            "fixture_adapter_sha256": hashlib.sha256(
                b"".join(
                    (ROOT / "scripts/ask_eval" / name).read_bytes()
                    for name in (
                        "fixtures.py",
                        "production.py",
                        "server.py",
                        "budget.py",
                    )
                )
            ).hexdigest(),
            "parity_mode": "structured_expectations" if args.live else "full_payload",
            "request_count": requests,
            "passed": sum(r["passed"] for r in results),
            "total": len(results),
            "cases": document["cases"],
            "results": results,
            "limitations": [
                "Single live-model run per transport; not a statistically powered model comparison."
                if args.live
                else "Controlled plans do not measure language interpretation or real model accuracy.",
                "Cases await human review. Production facts and profiles are frozen; warehouse completeness is not independently verified."
                if source.get("source_kind") == "production_snapshot"
                else "Cases await human review. Source statistics are fictional. Award lookups use the checked-in reviewed reference catalog.",
                "JSON/SSE parity and endpoint behavior are tested; warehouse completeness and deployment are not.",
                "Baseline reference failures may indicate missing structured evidence contracts, not wrong narrative numbers.",
            ],
        }
        (args.output / "results.json").write_text(json.dumps(report, indent=2) + "\n")
        print(
            f"{report['passed']}/{report['total']} cases passed; {args.output / 'results.json'}"
        )
        return 0 if report["passed"] == report["total"] else 1
    except Exception as exc:
        (args.output / "failure.json").write_text(
            json.dumps(
                {
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "label": args.label,
                }
            )
            + "\n"
        )
        raise
    finally:
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            log.close()


if __name__ == "__main__":
    raise SystemExit(main())
