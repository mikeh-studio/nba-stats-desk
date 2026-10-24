#!/usr/bin/env python3
"""Evaluate real model plans plus deterministic evidence against frozen expectations.

Each case has three paraphrases; ambiguous cases repeat three times. API failures
count as failures. Partial progress is flushed to JSONL for inspection/resumption.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from time import monotonic
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent.intelligence import get_provider, plan_question  # noqa: E402
from app.agent.plan_execution import execute_plan  # noqa: E402
from app.agent.semantics import CATALOG_PATH, Evidence, SemanticError  # noqa: E402
from app.config import get_settings  # noqa: E402

FIXTURE = ROOT / "tests/fixtures/semantics/language_cases.json"
SOURCE = ROOT / "tests/fixtures/semantics/cases.json"


def mismatches(actual: dict[str, Any], expected: dict[str, Any]) -> list[str]:
    failed = []
    for path, target in expected.items():
        value: Any = actual
        try:
            for key in path.split("."):
                value = value[int(key)] if isinstance(value, list) else value[key]
        except (KeyError, IndexError, TypeError, ValueError):
            failed.append(path)
            continue
        if type(value) in (int, float) and type(target) in (int, float):
            equal = math.isclose(value, target, abs_tol=1e-6, rel_tol=0)
        else:
            equal = value == target
        if not equal:
            failed.append(path)
    return failed


def run_case(
    client: Any,
    model: str,
    fixture: dict[str, Any],
    source: dict[str, Any],
    case: dict[str, Any],
    variant: int,
    repetition: int,
    ask: bool = False,
) -> dict[str, Any]:
    started = monotonic()
    plan = None
    model_calls = 0

    def counted_create(**kwargs):
        nonlocal model_calls
        model_calls += 1
        return client.responses.create(**kwargs)

    counted_client = SimpleNamespace(responses=SimpleNamespace(create=counted_create))
    try:
        evidence = Evidence(
            source["rows"],
            frozenset((s, p) for s, p, _ in source["coverage"]),
            "synthetic/player_games",
            hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
            {(s, p): d for s, p, d in source["coverage"]},
            complete=True,
        )
        if ask:
            from app.agent.service import StatsAgent
            from app.config import Settings

            warehouse = SimpleNamespace(
                load=lambda seasons: (
                    {"capture": {"query_id": "synthetic-fixture", "query_count": 0}},
                    evidence,
                ),
                players=lambda evidence: source["players"],
            )
            settings = Settings(
                project_id="fixture-project",
                gold_dataset="fixture",
                metadata_dataset="fixture",
                freshness_threshold_hours=36,
                max_search_results=12,
                season=fixture["selected_season"],
            )
            payload = StatsAgent(
                settings,
                SimpleNamespace(),
                client=counted_client,
                semantic_warehouse=warehouse,
            ).answer(case["questions"][variant], model=model)
            plan = payload.get("semantic_plan")
            actual = {
                "status": payload["status"],
                "ask_route": payload.get("ask_route"),
                "resolved_queries": payload.get("query_plan", {}).get("queries", []),
                "evidence": payload.get("semantic_evidence"),
                "identity": payload.get("identity"),
                "rendered_answer": payload["answer"],
                "rendered_tables": payload["tables"],
            }
        else:
            plan = plan_question(
                counted_client,
                settings=get_settings(),
                model=model,
                question=case["questions"][variant],
                selected_season=fixture["selected_season"],
                players=source["players"],
                teams=sorted(
                    {
                        r[k]
                        for r in source["rows"]
                        for k in ("team_abbr", "opponent_abbr")
                    }
                ),
            )
            actual = execute_plan(plan, evidence, source["players"])
        failures = mismatches(actual, case["expected"])
    except Exception as exc:
        # Keep provider errors inspectable without persisting request headers or keys.
        actual = {
            "status": "execution_error",
            "exception_type": type(exc).__name__,
            "error_code": exc.code if isinstance(exc, SemanticError) else None,
            "detail": str(exc) if isinstance(exc, SemanticError) else None,
        }
        failures = ["execution"]
    return {
        "id": case["id"],
        "variant": variant,
        "repetition": repetition,
        "question": case["questions"][variant],
        "passed": not failures,
        "failure_fields": failures,
        "plan": plan,
        "actual": actual,
        "latency_ms": round((monotonic() - started) * 1000),
        "model_calls": model_calls,
        "warehouse_queries": 0,
        "scanned_bytes": 0,
    }


def main() -> int:
    from dotenv import load_dotenv
    from openai import OpenAI

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--fixture", type=Path, default=FIXTURE)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--ask",
        action="store_true",
        help="Exercise the migrated StatsAgent response path",
    )
    parser.add_argument("--max-calls", type=int, default=60)
    args = parser.parse_args()
    if args.env_file:
        load_dotenv(args.env_file)
    from app.config import get_settings

    settings = get_settings()
    if not settings.openai_agent_enabled or not settings.openai_api_key:
        parser.error("The configured OpenAI agent must be enabled with credentials")
    if not get_provider(settings).descriptor.requires_model:
        parser.error(
            "Live language evaluation requires an explicitly selected model-backed intelligence provider"
        )
    fixture = json.loads(args.fixture.read_text())
    source = json.loads(SOURCE.read_text())
    jobs = [
        (case, variant, repeat)
        for case in fixture["cases"]
        for variant in range(len(case["questions"]))
        for repeat in range(case.get("repeats", 1))
    ]
    if any(len(case["questions"]) != 3 for case in fixture["cases"]):
        parser.error("Every case must have three phrasing variants")
    if len(jobs) > args.max_calls or args.max_calls > 100:
        parser.error("Evaluation exceeds bounded model-call budget (maximum 100)")
    progress = args.output.with_suffix(".jsonl")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    model = settings.agent_planner_model or settings.openai_agent_model
    client = OpenAI(api_key=settings.openai_api_key, max_retries=0)
    results = []
    with progress.open("x") as stream, ThreadPoolExecutor(max_workers=3) as pool:
        futures = [
            pool.submit(run_case, client, model, fixture, source, *job, ask=args.ask)
            for job in jobs
        ]
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            stream.write(json.dumps(result) + "\n")
            stream.flush()
            print(
                f"{len(results)}/{len(jobs)} {result['id']} {'PASS' if result['passed'] else result['failure_fields']}",
                flush=True,
            )
    results.sort(key=lambda r: (r["id"], r["variant"], r["repetition"]))
    report = {
        "model": model,
        "evidence_kind": "synthetic_with_live_planner",
        "entry_point": "StatsAgent.answer"
        if args.ask
        else "intelligence.plan_question",
        "llm_runs": sum(r["model_calls"] for r in results),
        "passed": sum(r["passed"] for r in results),
        "total": len(results),
        "case_families": len(fixture["cases"]),
        "phrases_per_case": 3,
        "human_reviewed": fixture["human_reviewed"],
        "release_ready": False,
        "prompt_sha256": hashlib.sha256(
            (get_provider(settings).prompts().planning_system or "reference").encode()
        ).hexdigest(),
        "contract_sha256": hashlib.sha256(CATALOG_PATH.read_bytes()).hexdigest(),
        "fixture_sha256": hashlib.sha256(args.fixture.read_bytes()).hexdigest(),
        "limitations": [
            "Synthetic evidence; use --ask to include the migrated StatsAgent answer path.",
            "Synthetic evidence; historical references are evaluated separately.",
        ],
        "results": results,
    }
    with args.output.open("x") as stream:
        stream.write(json.dumps(report, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "results"}, indent=2))
    return 0 if report["passed"] == report["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
