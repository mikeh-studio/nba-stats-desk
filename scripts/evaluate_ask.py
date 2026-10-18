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
from datetime import date
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.ask_eval.fixtures import load_inputs  # noqa: E402
from scripts.ask_eval.grading import canonical, grade  # noqa: E402
from scripts.ask_eval.private_io import (  # noqa: E402
    private_directory,
    private_open,
    private_write,
)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def evaluate(
    url,
    document,
    provider="openai",
    model=None,
    *,
    live=False,
    replay=False,
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
                    if turn.get("reset_context"):
                        client.post("/__eval/reset-conversations").raise_for_status()
                    if turn.get("client_context"):
                        body["previous_context"] = turn["client_context"]
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
                        # Dependency failures are JSON even for the SSE endpoint.
                        # Preserve the explicit capability error in both raw results.
                        if mode == "json" or status != 200:
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
                    if live or replay
                    else canonical(a["payload"]) == canonical(b["payload"])
                )
                for a, b in zip(modes["json"], modes["sse"], strict=True)
            )
            results.append(
                {
                    "id": case["id"],
                    "family": case["family"],
                    "variant_of": case.get("variant_of"),
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
    p.add_argument("--provider", choices=["openai", "claude", "openrouter"])
    p.add_argument("--model")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--record-cassettes", type=Path)
    group.add_argument("--replay-cassettes", type=Path)
    p.add_argument("--clock", help="ISO date for semantic calendar defaults only")
    p.add_argument("--case", action="append")
    p.add_argument(
        "--max-requests",
        type=int,
        help="HTTP request budget (offline default 400; live default 160)",
    )
    args = p.parse_args()
    replay_manifest = None
    if args.replay_cassettes:
        if args.live:
            p.error("--replay-cassettes is incompatible with --live")
        replay_manifest = json.loads(
            (args.replay_cassettes / "manifest.json").read_text()
        )
        if replay_manifest.get("schema_version") != 1:
            p.error("Unsupported cassette schema")
        for name in ("model", "clock"):
            supplied = getattr(args, name)
            if supplied is not None and supplied != replay_manifest[name]:
                p.error("Replay " + name + " does not match manifest")
            setattr(args, name, replay_manifest[name])
        if args.provider is not None and args.provider != replay_manifest["provider"]:
            p.error("Replay provider does not match manifest")
        args.provider = replay_manifest["provider"]
        if not args.case:
            args.case = list(replay_manifest["cases"])
    args.provider = args.provider or "openai"
    if args.clock:
        try:
            if date.fromisoformat(args.clock).isoformat() != args.clock:
                raise ValueError()
        except ValueError:
            p.error("--clock must be an ISO calendar date")
    if args.record_cassettes:
        if not args.live or not args.case or not args.clock:
            p.error("--record-cassettes requires --live, --case and --clock")
        destination = args.record_cassettes.resolve()
        private_root = (ROOT / "reports/ask-harness").resolve()
        if not destination.is_relative_to(private_root) or destination.exists():
            p.error("Record into a fresh private reports/ask-harness directory")
        output = args.output.resolve()
        if output == destination or output.is_relative_to(destination):
            p.error("Output must not overlap the cassette directory")
    if args.replay_cassettes and (
        args.output.resolve() == args.replay_cassettes.resolve()
        or args.output.resolve().is_relative_to(args.replay_cassettes.resolve())
    ):
        p.error("Output must not overlap replay inputs")
    if args.max_requests is not None and args.max_requests <= 0:
        p.error("--max-requests must be positive")
    request_budget = (
        args.max_requests
        if args.max_requests is not None
        else (160 if args.live else 400)
    )
    source, document = load_inputs(ROOT, args.input_dir)
    input_dir = args.input_dir or ROOT / "tests/fixtures/ask"
    from scripts.ask_eval.coverage import summarize

    corpus_summary = summarize(source, document)
    if args.case:
        document["cases"] = [c for c in document["cases"] if c["id"] in args.case]
        if len(document["cases"]) != len(set(args.case)):
            p.error("Unknown case ID")
    requests = 2 * sum(len(c["turns"]) for c in document["cases"])
    if not document["cases"] or requests > request_budget:
        p.error("Empty selection or request budget exceeded")
    if (
        not args.live
        and not args.replay_cassettes
        and (args.provider != "openai" or args.model)
    ):
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
    from scripts.ask_eval.cassettes import sha

    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True
    ).stdout.strip()
    binding = {
        "provider": args.provider,
        "model": args.model,
        "clock": args.clock,
        "cases": {c["id"]: sha(c) for c in document["cases"]},
        "source_sha256": digest(input_dir / "source.json"),
        "schema_sha256": digest(ROOT / "tests/fixtures/ask/schema.json"),
        "corpus_sha256": digest(input_dir / "cases.json"),
        "adapter_revision": revision,
    }
    if replay_manifest:
        for key in (
            "provider",
            "model",
            "clock",
            "cases",
            "source_sha256",
            "schema_sha256",
        ):
            if replay_manifest.get(key) != binding[key]:
                p.error("Cassette manifest mismatch: " + key)
    private_directory(args.output)
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
        env.pop("ASK_EVAL_CLOCK", None)
        if args.clock:
            env["ASK_EVAL_CLOCK"] = args.clock
        # Never inherit a stale recording/replay mode from the launching shell.
        for name in (
            "ASK_EVAL_CASSETTE_MODE",
            "ASK_EVAL_CASSETTE_DIR",
            "ASK_EVAL_BINDING",
        ):
            env.pop(name, None)
        if args.record_cassettes or args.replay_cassettes:
            env.update(
                ASK_EVAL_CASSETTE_MODE="record" if args.record_cassettes else "replay",
                ASK_EVAL_CASSETTE_DIR=str(
                    (args.record_cassettes or args.replay_cassettes).resolve()
                ),
                ASK_EVAL_BINDING=json.dumps(binding),
            )
        log = private_open(args.output / "server.log")
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
        # Cold imports can exceed ten seconds while other offline QA runs.
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
        with private_open(args.output / "progress.jsonl") as progress:
            results = evaluate(
                url,
                document,
                args.provider,
                args.model,
                live=args.live,
                replay=bool(args.replay_cassettes),
                progress=progress,
                season=source.get("season", "2024-25"),
            )
        with httpx.Client(base_url=url) as client:
            runtime = client.get("/__eval/metadata").json()
            cassette_status = client.post("/__eval/finalize").json()
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
            "clock": args.clock,
            "selected_cases_sha256": sha(binding["cases"]),
            "schema_sha256": binding["schema_sha256"],
            "cassette": cassette_status,
            "cassette_sha256": cassette_status.get("cassette_sha256"),
            "harness_passed": not cassette_status.get("errors")
            and not cassette_status.get("failed_attempts")
            and not runtime.get("fixture_capability_failures"),
            "execution_mode": "recorded_provider_replay_http"
            if args.replay_cassettes
            else "recorded_provider_capture_http"
            if args.record_cassettes
            else "live_planner_" + source.get("source_kind", "synthetic") + "_http"
            if args.live or args.replay_cassettes
            else "controlled_planner_frozen_source_http",
            "provider": args.provider
            if args.live or args.replay_cassettes
            else "controlled",
            "model": args.model
            if args.live or args.replay_cassettes
            else "predeclared-plan",
            "human_reviewed": False,
            "corpus_summary": corpus_summary,
            "selection_summary": summarize(source, document),
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
                        "cassettes.py",
                        "private_io.py",
                    )
                )
            ).hexdigest(),
            "parity_mode": "structured_expectations"
            if args.live or args.replay_cassettes
            else "full_payload",
            "request_count": requests,
            "passed": sum(r["passed"] for r in results),
            "total": len(results),
            "coverage": {
                kind: {
                    "total": len(selected),
                    "passed": sum(r["passed"] for r in selected),
                }
                for kind, selected in (
                    ("distinct", [r for r in results if not r.get("variant_of")]),
                    ("paraphrases", [r for r in results if r.get("variant_of")]),
                )
            },
            "families": {
                family: {
                    "total": len(selected),
                    "passed": sum(r["passed"] for r in selected),
                }
                for family in sorted({r["family"] for r in results})
                for selected in [[r for r in results if r["family"] == family]]
            },
            "cases": document["cases"],
            "results": results,
            "limitations": [
                "Recorded provider replay checks frozen output behavior, not current model quality or streaming timing."
                if args.replay_cassettes
                else "Single live-model run per transport; not a statistically powered model comparison."
                if args.live
                else "Controlled plans do not measure language interpretation or real model accuracy.",
                "Cases await human review. Production facts and profiles are frozen; warehouse completeness is not independently verified."
                if source.get("source_kind") == "production_snapshot"
                else "Cases await human review. Source statistics are fictional. Award lookups use the checked-in reviewed reference catalog.",
                "JSON/SSE parity and endpoint behavior are tested; warehouse completeness and deployment are not.",
                "Baseline reference failures may indicate missing structured evidence contracts, not wrong narrative numbers.",
            ],
        }
        private_write(args.output / "results.json", json.dumps(report, indent=2) + "\n")
        print(
            f"{report['passed']}/{report['total']} cases passed; {args.output / 'results.json'}"
        )
        return (
            0 if report["passed"] == report["total"] and report["harness_passed"] else 1
        )
    except Exception as exc:
        private_write(
            args.output / "failure.json",
            json.dumps(
                {
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "label": args.label,
                }
            )
            + "\n",
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
