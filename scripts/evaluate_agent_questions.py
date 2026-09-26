from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.agent.service import AgentDisabledError, AgentExecutionError, StatsAgent
from app.config import get_settings
from app.repository import BigQueryWarehouseRepository

DEFAULT_FIXTURE = ROOT / "tests" / "fixtures" / "agent_questions.yml"


def _normalized_text(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.casefold()


def _load_cases(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or []
    if not isinstance(loaded, list):
        raise ValueError(f"{path} must contain a YAML list of question cases.")
    return [case for case in loaded if isinstance(case, dict)]


def _payload_text(payload: dict[str, Any]) -> str:
    return _normalized_text(json.dumps(payload, sort_keys=True, ensure_ascii=False))


def _actual_tools(payload: dict[str, Any]) -> set[str]:
    return {
        str(item.get("name"))
        for item in payload.get("tool_calls", [])
        if isinstance(item, dict) and item.get("name")
    }


def _expected_tool_options(tool: str) -> set[str]:
    normalized = tool.strip().casefold()
    aliases = {
        "percentille": {"calculate_player_percentile", "get_player_percentiles"},
        "percentile": {"calculate_player_percentile", "get_player_percentiles"},
    }
    return aliases.get(normalized, {tool.strip()})


def _check_case(case: dict[str, Any], payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    text = _payload_text(payload)
    tools = _actual_tools(payload)

    for expected in case.get("expected_tools", []) or []:
        options = _expected_tool_options(str(expected))
        if tools.isdisjoint(options):
            errors.append(
                f"missing expected tool {expected!r}; actual={sorted(tools) or []}"
            )

    for term in case.get("expected_terms", []) or []:
        if _normalized_text(term) not in text:
            errors.append(f"missing expected term {term!r}")

    for field in ("assumptions", "tables", "charts", "metric_definitions", "followups"):
        if not isinstance(payload.get(field), list):
            errors.append(f"invalid structured answer field: {field}")

    if not str(payload.get("answer") or "").strip():
        errors.append("empty answer")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run live Ask NBA Stats agent eval questions."
    )
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--case", action="append", default=[], help="Case id to run.")
    parser.add_argument("--json", action="store_true", help="Print full JSON payloads.")
    parser.add_argument(
        "--provider", choices=("openai", "claude", "openrouter"), default="openai"
    )
    parser.add_argument(
        "--model", help="Candidate model ID; does not enable it in Ask."
    )
    parser.add_argument(
        "--report-dir", type=Path, default=ROOT / "reports" / "model-evals"
    )
    args = parser.parse_args()
    settings = get_settings()
    key = (
        settings.openrouter_api_key
        if args.provider == "openrouter"
        else settings.anthropic_api_key
        if args.provider == "claude"
        else settings.openai_api_key
    )
    if not key:
        print(f"Credentials for {args.provider} are not configured.", file=sys.stderr)
        return 2
    model = args.model or (
        settings.openrouter_agent_model
        if args.provider == "openrouter"
        else settings.anthropic_agent_model
        if args.provider == "claude"
        else settings.openai_agent_model
    )

    cases = _load_cases(args.fixture)
    if args.case:
        requested = set(args.case)
        cases = [case for case in cases if case.get("id") in requested]
    if args.limit:
        cases = cases[: args.limit]
    if not cases:
        print("No eval cases selected.", file=sys.stderr)
        return 2

    repo = BigQueryWarehouseRepository(settings)
    agent = StatsAgent(settings, repo)

    failures: list[str] = []
    results = []
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    for index, case in enumerate(cases, start=1):
        case_id = str(case.get("id") or f"case_{index}")
        question = str(case.get("question") or "").strip()
        print(f"[{index}/{len(cases)}] {case_id}: {question}", flush=True)
        started = monotonic()
        try:
            payload = agent.answer(
                question,
                conversation_id=f"eval:{run_id}:{case_id}",
                provider=args.provider,
                model=model,
            )
        except (AgentDisabledError, AgentExecutionError, ValueError) as exc:
            error = type(exc).__name__
            failures.append(f"{case_id}: {error}")
            results.append(
                {
                    "id": case_id,
                    "errors": [error],
                    "seconds": round(monotonic() - started, 3),
                }
            )
            print(f"  FAIL {error}", flush=True)
            continue

        errors = _check_case(case, payload)
        results.append(
            {
                "id": case_id,
                "errors": errors,
                "seconds": round(monotonic() - started, 3),
            }
        )
        tool_names = [item.get("name") for item in payload.get("tool_calls", [])]
        print(f"  tools: {tool_names}", flush=True)
        print(f"  answer: {payload.get('answer')}", flush=True)
        if args.json:
            print(
                json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False),
                flush=True,
            )

        if errors:
            failures.extend([f"{case_id}: {error}" for error in errors])
            for error in errors:
                print(f"  FAIL {error}", flush=True)
        else:
            print("  PASS", flush=True)

    args.report_dir.mkdir(parents=True, exist_ok=True)
    report_path = args.report_dir / f"{run_id}.json"
    report_path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "provider": args.provider,
                "model": model,
                "fixture": str(args.fixture),
                "status": "failed" if failures else "passed",
                "scope": "Live warehouse smoke test: tools, answer fields, and expected terms. Does not certify numeric accuracy, streaming, conversation history, or cost.",
                "cases": results,
            },
            indent=2,
        )
        + "\n"
    )
    print(f"Report: {report_path}")
    if failures:
        print("\nFailures:")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("\nAll agent eval questions passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
