#!/usr/bin/env python3
"""Measure which Ask route each fixture question reaches.

``--mode current`` replays today's dispatch (``app.agent.routes``) plus the
deterministic checks each handler makes before any planner call. It makes no
model calls and needs no warehouse.

``--mode ask`` runs ``StatsAgent.answer`` on the frozen Ask fixture warehouse and
reads the payload's ``ask_route``. The default controlled model returns planner
refusals, so only route decisions are measured. ``--provider openai`` uses a live
model for local runs; provider keys are never added to CI.

Each case has three paraphrases. Progress is flushed to JSONL and reports belong
in ignored ``reports/`` directories.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import date
from pathlib import Path
from time import monotonic
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent.route_labels import (  # noqa: E402
    DISPATCH_LABELS,
    LEGACY_LABELS,
    PLANNED_LABELS,
    ROUTE_LABELS,
    ROUTE_SOURCES,
)

FIXTURE = ROOT / "tests/fixtures/semantics/route_cases.json"
SOURCE = ROOT / "tests/fixtures/ask/source.json"
CATEGORIES = ("route", "scope_drop", "followup", "refusal", "control")
OUTCOMES = ("answer", "clarify", "refuse", "unknown")
SETTINGS = ("research_studies", "research_availability", "teammate_study_v1")
# Labels whose final value is a research-planner choice; "research" means unchosen.
RESEARCH_FAMILY = {"research", "research_breakdown", "teammate_study"}
CONVERSATION_ID = "route-evaluation"


def valid_label(value: Any) -> bool:
    return value in ROUTE_LABELS or value in LEGACY_LABELS or value in PLANNED_LABELS


def as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else [value]


def validate(fixture: dict[str, Any]) -> None:
    ids = [case["id"] for case in fixture["cases"]]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate route case IDs")
    if set(fixture.get("settings", {})) - set(SETTINGS):
        raise ValueError("Unknown fixture settings")
    for case in fixture["cases"]:
        where = case["id"]
        if len(case["questions"]) != 3:
            raise ValueError(f"{where}: every case needs three phrasings")
        if case["category"] not in CATEGORIES:
            raise ValueError(f"{where}: unknown category")
        if "ask_route.route" not in case["expected"]:
            raise ValueError(f"{where}: expected ask_route.route is required")
        if set(case.get("settings", {})) - set(SETTINGS):
            raise ValueError(f"{where}: unknown case settings")
        checks = {
            "ask_route.route": valid_label,
            "ask_route.source": lambda v: v in ROUTE_SOURCES,
            "outcome": lambda v: v in OUTCOMES,
        }
        for field in (*case["expected"], *case.get("forbidden", {})):
            if field not in checks and field != "ask_route.reason_code":
                raise ValueError(f"{where}: unsupported field {field}")
        for field, value in case["expected"].items():
            check = checks.get(field)
            if check and not all(check(v) for v in as_list(value)):
                raise ValueError(f"{where}: invalid expected {field}")
        for field, values in case.get("forbidden", {}).items():
            check = checks.get(field)
            if check and not all(check(v) for v in values):
                raise ValueError(f"{where}: invalid forbidden {field}")
        context = case.get("context") or {}
        prior = context.get("prior_route")
        if prior is not None and prior not in PRIOR_CONTEXTS:
            raise ValueError(f"{where}: no synthetic context for {prior}")
        pending = context.get("pending")
        if pending and pending.get("route") and not valid_label(pending["route"]):
            raise ValueError(f"{where}: invalid pending route")


# Minimal stored turn context per prior route; only the keys routing reads.
PRIOR_CONTEXTS: dict[str, dict[str, Any]] = {
    "research_breakdown": {
        "research_scope": {
            "season": None,
            "phase": "Both",
            "player_ids": [101],
            "teammate_id": None,
            "metrics": ["pts"],
            "kind": "breakdown",
        }
    },
    "teammate_study": {
        "research_scope": {
            "season": None,
            "phase": "Both",
            "player_ids": [2544],
            "teammate_id": 1629029,
            "metrics": ["pts"],
            "kind": "study",
            "pair_id": "lebron-luka",
        }
    },
    "teammate_study_v1": {"teammate_study": True},
    "governed_metrics": {
        "players": [{"player_id": 101, "player_name": "Avery Example"}],
        "metrics": ["pts"],
        "scope": {"season": None},
    },
}


def prior_context(route: str | None, season: str) -> dict[str, Any]:
    if route is None:
        return {}
    context = json.loads(json.dumps(PRIOR_CONTEXTS[route]))
    for value in context.values():
        if isinstance(value, dict) and "season" in value:
            value["season"] = season
    return dict(context, route=route, question="Earlier question")


def case_settings(fixture: dict[str, Any], case: dict[str, Any]) -> dict[str, bool]:
    return {
        **{key: False for key in SETTINGS},
        **fixture.get("settings", {}),
        **case.get("settings", {}),
    }


class Attachments:
    """Materialize synthetic availability/study attachments once per run."""

    def __init__(self, source: dict[str, Any], directory: str):
        self.source = source
        self.directory = directory
        self.paths: dict[str, str] = {}

    def path(self, key: str) -> str:
        if key not in self.paths:
            from scripts.ask_eval.fixtures import fixture_artifacts

            self.paths.update(fixture_artifacts(self.source, self.directory, [key]))
        return self.paths[key]


def outcome_of(reason_code: str | None) -> str:
    if reason_code is None:
        return "answer"
    if "clarification" in reason_code or reason_code.startswith("overview_"):
        return "clarify"
    return "refuse"


def current_route(
    question: str,
    case: dict[str, Any],
    fixture: dict[str, Any],
    attachments: Attachments,
) -> dict[str, Any]:
    """Replay dispatch and pre-planner checks for one question; no model calls."""
    from app.agent.availability_ask import wants_availability
    from app.agent.followup import resolve_followup
    from app.agent.performance_overview import overview_scope
    from app.agent.player_comparison import comparison_scope
    from app.agent.research_ask import direct_study_plan, named_absence_pair
    from app.agent.routes import (
        RouteContext,
        dispatch_source,
        pending_routing,
        select_route,
    )
    from app.agent.semantic_serving import requested_seasons
    from app.agent.semantics import SemanticError

    season = fixture["selected_season"]
    flags = case_settings(fixture, case)
    settings = SimpleNamespace(
        season=season,
        research_studies_path="fixture-catalog" if flags["research_studies"] else None,
        research_availability_path=attachments.path("availability")
        if flags["research_availability"]
        else None,
        agent_teammate_study_path=attachments.path("study")
        if flags["teammate_study_v1"]
        else None,
    )
    context = case.get("context") or {}
    recent = prior_context(context.get("prior_route"), season)
    pending = context.get("pending")
    routing_question, reference_reply, _ = pending_routing(
        question,
        pending["question"] if pending else None,
        typed_overview=bool(
            pending and (pending.get("query_plan") or {}).get("overview_clarification")
        ),
    )
    route_context = RouteContext(
        question,
        routing_question,
        recent,
        recent,
        settings,
        True,
        reference_reply=reference_reply,
    )
    route = select_route(route_context)
    label = DISPATCH_LABELS.get(route.key, route.key)
    source = dispatch_source(route_context, route)
    reason: str | None = None
    outcome = "unknown"

    if route.handler == "scope_refusal":
        reason, outcome = "unsupported_round", "refuse"
    elif route.handler == "availability" and not settings.research_availability_path:
        reason, outcome = "availability_missing", "refuse"
    elif route.handler == "teammate_study" and not settings.agent_teammate_study_path:
        reason, outcome = "study_not_configured", "refuse"
    elif route.handler == "research":
        previous = recent.get("research_scope")
        seasons = requested_seasons(
            question, previous.get("season", season) if previous else season
        )
        pair = named_absence_pair(question)
        published = fixture.get("catalog", {}).get(pair["pair_id"], {}) if pair else {}
        if len(seasons) != 1:
            reason, outcome = "multi_season", "refuse"
        elif pair and published.get("status") != "published":
            label, reason, outcome = (
                "teammate_study",
                "study_coverage_missing",
                "refuse",
            )
        elif not previous and direct_study_plan(question, seasons[0]):
            label = "teammate_study"
    elif route.handler == "semantic":
        if wants_availability(question):
            label, source = "unsupported", "deterministic"
            reason, outcome = "availability_scope_required", "refuse"
        else:
            if source == "deterministic" and resolve_followup(question, recent)[1]:
                source = "prior_route"
            try:
                if label == "player_overview":
                    overview_scope(routing_question, season)
                    outcome = "answer"
                elif label == "player_comparison":
                    comparison_scope(routing_question, season)
                    outcome = "answer"
            except SemanticError as exc:
                reason, outcome = exc.code, outcome_of(exc.code)
    return {
        "ask_route": {"route": label, "source": source, "reason_code": reason},
        "outcome": outcome,
        "model_calls": 0,
    }


def research_unsupported(season: str) -> dict[str, Any]:
    from app.research import CORE_METRICS, ResearchQuery

    query = ResearchQuery(
        season=season, phase="Both", player_ids=[101], metrics=list(CORE_METRICS)
    ).model_dump(mode="json")
    return {
        "kind": "unsupported",
        "query": query,
        "pair_id": None,
        "message": "Controlled route evaluation: the research planner made no decision.",
    }


class CountingClient:
    """Count provider calls; route decisions never depend on this wrapper."""

    def __init__(self, client: Any, counter: list[int] | None = None):
        self.client = client
        self.counter = counter if counter is not None else [0]
        self.responses = self

    @property
    def calls(self) -> int:
        return self.counter[0]

    def with_options(self, **kwargs: Any) -> CountingClient:
        return CountingClient(self.client.with_options(**kwargs), self.counter)

    def create(self, **kwargs: Any) -> Any:
        self.counter[0] += 1
        return self.client.responses.create(**kwargs)


def ask_route_case(
    question: str,
    case: dict[str, Any],
    fixture: dict[str, Any],
    source: dict[str, Any],
    attachments: Attachments,
    *,
    provider: str,
    live_settings: Any = None,
) -> dict[str, Any]:
    from app.agent.conversation import InMemoryConversationStore
    from app.agent.observability import AgentTrace
    from app.agent.service import StatsAgent
    from app.config import Settings
    from scripts.ask_eval.fixtures import ControlledModel, frozen_repository

    season = fixture["selected_season"]
    flags = case_settings(fixture, case)
    settings = Settings(
        project_id="fixture-project",
        gold_dataset="fixture",
        metadata_dataset="fixture",
        freshness_threshold_hours=36,
        max_search_results=12,
        season=season,
        openai_api_key="fixture-key",
        agent_rate_limit_per_minute=0,
        agent_rate_limit_daily=0,
        # No v3 catalog file is frozen here; every study reads as coverage_pending.
        research_studies_path=None,
        research_availability_path=attachments.path("availability")
        if flags["research_availability"]
        else None,
        agent_teammate_study_path=attachments.path("study")
        if flags["teammate_study_v1"]
        else None,
    )
    if live_settings is not None:
        settings = replace(
            settings,
            openai_api_key=live_settings.openai_api_key,
            openai_agent_model=live_settings.openai_agent_model,
        )
        from openai import OpenAI

        inner: Any = OpenAI(api_key=live_settings.openai_api_key, max_retries=0)
    else:
        inner = ControlledModel(
            {
                "planner_response": {
                    "status": "unsupported",
                    "queries": [],
                    "message": "Controlled route evaluation: the planner made no decision.",
                },
                "research_response": research_unsupported(season),
                "study_response": {
                    "focal_player_id": None,
                    "teammate_id": None,
                    "season": None,
                    "start": None,
                    "end": None,
                    "metric": None,
                    "scope_matches": False,
                    "answer": "",
                },
            }
        )
    client = CountingClient(inner)
    store = InMemoryConversationStore()
    context = case.get("context") or {}
    if context.get("prior_route"):
        store.append_turn(
            CONVERSATION_ID,
            question="Earlier question",
            answer="",
            max_turns=settings.agent_conversation_max_turns,
            context=prior_context(context["prior_route"], season),
        )
    if context.get("pending"):
        pending = context["pending"]
        store.set_pending_clarification(
            CONVERSATION_ID,
            question=pending["question"],
            query_plan=pending.get("query_plan"),
            route=pending.get("route"),
        )
    agent = StatsAgent(
        settings,
        frozen_repository(settings, source, "complete"),
        client=client,
        conversation_store=store,
    )
    model = settings.openai_agent_model
    trace = AgentTrace(request_id="route-evaluation", question="", model=model)
    payload = agent.answer(
        question,
        conversation_id=CONVERSATION_ID,
        trace=trace,
        provider="openai",
        model=model,
    )
    label = payload["ask_route"]
    return {
        "ask_route": label,
        # A controlled model's refusal says nothing about the real planner outcome.
        "outcome": "unknown"
        if provider == "controlled" and client.calls
        else outcome_of(label["reason_code"]),
        "status": payload.get("status"),
        "trace_matches_payload": trace.ask_route == label,
        "model_calls": client.calls,
    }


def lookup(actual: dict[str, Any], path: str) -> Any:
    value: Any = actual
    for key in path.split("."):
        value = value.get(key) if isinstance(value, dict) else None
    return value


def score(case: dict[str, Any], actual: dict[str, Any]) -> dict[str, Any]:
    expected_routes = as_list(case["expected"]["ask_route.route"])
    route = actual["ask_route"]["route"]
    # "research" means the research handler answered or refused before choosing
    # breakdown vs study; it matches either expected research sub-route.
    family_match = route == "research" and any(
        target in RESEARCH_FAMILY for target in expected_routes
    )
    route_correct = route in expected_routes or bool(family_match)
    failures, unverified = [], []
    for path, target in case["expected"].items():
        value = route if path == "ask_route.route" else lookup(actual, path)
        if path == "ask_route.route":
            ok = route_correct
        elif value == "unknown":
            unverified.append(path)
            continue
        else:
            ok = value in as_list(target)
        if not ok:
            failures.append(path)
    forbidden_hits = [
        path
        for path, values in case.get("forbidden", {}).items()
        if (route if path == "ask_route.route" else lookup(actual, path)) in values
    ]
    outcome = actual.get("outcome", "unknown")
    return {
        "route_correct": route_correct,
        "research_family_match": bool(family_match),
        "passed": not failures and not forbidden_hits,
        "failure_fields": failures,
        "unverified_fields": unverified,
        "forbidden_hits": forbidden_hits,
        # A forbidden route that answers (or may answer) silently drops scope.
        "scope_drop": bool(forbidden_hits) and outcome in ("answer", "unknown"),
        "wrong_path_refusal": not route_correct and outcome in ("refuse", "clarify"),
        "misroute_outcome_unknown": not route_correct and outcome == "unknown",
    }


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    by_category: dict[str, dict[str, Any]] = {}
    for category in CATEGORIES:
        rows = [r for r in results if r["category"] == category]
        if rows:
            correct = sum(r["route_correct"] for r in rows)
            by_category[category] = {
                "total": len(rows),
                "route_correct": correct,
                "route_accuracy": round(correct / len(rows), 4),
            }
    confusion: dict[str, Counter[str]] = defaultdict(Counter)
    for r in results:
        confusion[as_list(r["expected"]["ask_route.route"])[0]][
            r["actual"]["ask_route"]["route"]
        ] += 1
    total = len(results)
    correct = sum(r["route_correct"] for r in results)
    reasons = Counter(
        r["actual"]["ask_route"]["reason_code"]
        for r in results
        if r["actual"]["ask_route"]["reason_code"]
    )
    return {
        "total": total,
        "route_correct": correct,
        "route_accuracy": round(correct / total, 4) if total else 0.0,
        "passed": sum(r["passed"] for r in results),
        "by_category": by_category,
        "scope_drops": sum(r["scope_drop"] for r in results),
        "forbidden_route_hits": sum(bool(r["forbidden_hits"]) for r in results),
        "wrong_path_refusals": sum(r["wrong_path_refusal"] for r in results),
        "misroutes_outcome_unknown": sum(
            r["misroute_outcome_unknown"] for r in results
        ),
        "research_family_matches": sum(r["research_family_match"] for r in results),
        "unverified_fields": sum(len(r["unverified_fields"]) for r in results),
        "model_calls": sum(r["actual"].get("model_calls", 0) for r in results),
        "by_source": dict(Counter(r["actual"]["ask_route"]["source"] for r in results)),
        "by_reason_code": dict(sorted(reasons.items())),
        "confusion": {
            expected: dict(sorted(row.items()))
            for expected, row in sorted(confusion.items())
        },
    }


def evaluate(
    fixture: dict[str, Any],
    *,
    mode: str,
    provider: str = "controlled",
    live_settings: Any = None,
    progress: Any = None,
) -> list[dict[str, Any]]:
    source = json.loads(SOURCE.read_text())
    results = []
    with tempfile.TemporaryDirectory() as directory:
        attachments = Attachments(source, directory)
        for case in fixture["cases"]:
            for variant, question in enumerate(case["questions"]):
                started = monotonic()
                try:
                    if mode == "current":
                        actual = current_route(question, case, fixture, attachments)
                    else:
                        actual = ask_route_case(
                            question,
                            case,
                            fixture,
                            source,
                            attachments,
                            provider=provider,
                            live_settings=live_settings,
                        )
                    scored = score(case, actual)
                except Exception as exc:
                    # Keep failures inspectable without persisting provider details.
                    actual = {
                        "ask_route": {
                            "route": "execution_error",
                            "source": None,
                            "reason_code": type(exc).__name__,
                        },
                        "outcome": "unknown",
                    }
                    scored = dict(
                        score(case, actual),
                        passed=False,
                        failure_fields=["execution"],
                    )
                result = {
                    "id": case["id"],
                    "finding": case.get("finding"),
                    "category": case["category"],
                    "variant": variant,
                    "question": question,
                    "expected": case["expected"],
                    "forbidden": case.get("forbidden", {}),
                    "actual": actual,
                    **scored,
                    "latency_ms": round((monotonic() - started) * 1000),
                }
                results.append(result)
                if progress is not None:
                    progress.write(json.dumps(result) + "\n")
                    progress.flush()
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("current", "ask"), required=True)
    parser.add_argument("--fixture", type=Path, default=FIXTURE)
    parser.add_argument(
        "--output",
        type=Path,
        help="Report path; defaults to reports/ask-routing/<date>/<mode>.json",
    )
    parser.add_argument(
        "--provider",
        choices=("controlled", "openai"),
        default="controlled",
        help="ask mode only: controlled planner refusals, or a live OpenAI model",
    )
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--max-calls", type=int, default=100)
    parser.add_argument("--fail-under", type=float, help="Minimum route accuracy")
    parser.add_argument("--max-scope-drops", type=int, help="Maximum scope drops")
    args = parser.parse_args()
    fixture = json.loads(args.fixture.read_text())
    validate(fixture)
    live_settings = None
    if args.mode == "ask" and args.provider == "openai":
        from dotenv import load_dotenv

        if args.env_file:
            load_dotenv(args.env_file)
        from app.config import get_settings

        live_settings = get_settings()
        if not live_settings.openai_agent_enabled or not live_settings.openai_api_key:
            parser.error("The configured OpenAI agent must be enabled with credentials")
        questions = sum(len(case["questions"]) for case in fixture["cases"])
        if questions > args.max_calls:
            parser.error("Evaluation exceeds the bounded question budget (--max-calls)")
    elif args.provider != "controlled":
        parser.error("--provider applies to --mode ask only")
    output = args.output or (
        ROOT / "reports/ask-routing" / date.today().isoformat() / f"{args.mode}.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.with_suffix(".jsonl").open("x") as progress:
        results = evaluate(
            fixture,
            mode=args.mode,
            provider=args.provider,
            live_settings=live_settings,
            progress=progress,
        )
    summary = summarize(results)
    report = {
        "mode": args.mode,
        "provider": args.provider if args.mode == "ask" else None,
        "entry_point": "app.agent.routes.select_route"
        if args.mode == "current"
        else "StatsAgent.answer",
        "evidence_kind": {
            ("current", "controlled"): "predicate_replay",
            ("ask", "controlled"): "fixture_warehouse_controlled_model",
            ("ask", "openai"): "fixture_warehouse_live_model",
        }[(args.mode, args.provider)],
        "fixture_sha256": hashlib.sha256(args.fixture.read_bytes()).hexdigest(),
        "human_reviewed": fixture.get("human_reviewed", False),
        "settings": fixture.get("settings", {}),
        "limitations": [
            "A 'research' label (handler reached, sub-route not chosen) matches an "
            "expected research_breakdown or teammate_study; see "
            "research_family_matches. current mode cannot replay planner choices.",
            "current: scope drops count forbidden routes whose outcome is answer or "
            "unknown, because an unknown planner outcome cannot be ruled out.",
            "ask: the frozen fixture has no v3 study catalog, so every study reads "
            "as coverage_pending; outcomes can differ from current mode.",
            "ask --provider controlled: planner calls return refusals, so outcomes "
            "of turns that called the model are reported as unknown.",
        ],
        "summary": summary,
        "results": results,
    }
    with output.open("x") as stream:
        stream.write(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"output": str(output), **summary}, indent=2))
    failed = (
        args.fail_under is not None and summary["route_accuracy"] < args.fail_under
    ) or (
        args.max_scope_drops is not None
        and summary["scope_drops"] > args.max_scope_drops
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
