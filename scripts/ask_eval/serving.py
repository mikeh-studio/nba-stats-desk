"""Offline serving parity over the endpoint harness's existing frozen source.

This companion does not enable serving routes or measure deployment latency.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from app.agent.semantic_source import snapshot_digest, snapshot_evidence
from app.agent.semantics import SemanticError, aggregate, load_contract
from app.agent.serving_contract import aggregate_summaries
from app.agent.serving_publication import (
    candidate_manifest,
    candidate_summaries,
    validate_publication,
)
from scripts.ask_eval.fixtures import FrozenWarehouse


def equivalent(actual: Any, expected: Any) -> bool:
    """Float arithmetic: rel_tol=1e-12, abs_tol=1e-12; all structure exact."""
    if type(actual) in (float, int) and type(expected) in (float, int):
        return math.isclose(actual, expected, rel_tol=1e-12, abs_tol=1e-12)
    if isinstance(actual, dict) and isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(
            equivalent(actual[k], expected[k]) for k in actual
        )
    if isinstance(actual, (list, tuple)) and isinstance(expected, (list, tuple)):
        return len(actual) == len(expected) and all(
            equivalent(a, b) for a, b in zip(actual, expected)
        )
    return type(actual) is type(expected) and actual == expected


def evaluate(source: dict[str, Any], variant: str = "complete") -> dict[str, Any]:
    _, evidence = FrozenWarehouse(source, variant).load(["2024-25"])
    snapshot = dict(
        rows=evidence.rows,
        sources=[evidence.source],
        cohort_complete=True,
        source_timestamp="2026-10-21T23:59:00+00:00",
        coverage=[
            dict(
                season=s,
                phase=p,
                data_through=d,
                rows=sum(
                    r["season"] == s and r["season_type"] == p for r in evidence.rows
                ),
            )
            for (s, p), d in evidence.data_through.items()
        ],
    )
    snapshot["sha256"] = snapshot_digest(snapshot)
    evidence = snapshot_evidence(snapshot)
    contract = load_contract()
    summaries = candidate_summaries(evidence, "offline-fixture", list(contract.metrics))
    manifest = candidate_manifest(
        evidence,
        summaries,
        publication_id="offline-fixture",
        metrics=list(contract.metrics),
        built_at="2026-10-21T23:59:30+00:00",
        expires_at="2026-10-22T00:04:00+00:00",
        relation_version="frozen-fixture/1",
        source_timestamp=snapshot["source_timestamp"],
    )
    validate_publication(
        manifest,
        summaries,
        source=evidence,
        source_snapshot=snapshot,
        expected_snapshot_id=evidence.snapshot_id,
        now=datetime(2026, 10, 22, tzinfo=timezone.utc),
    )
    grouped = defaultdict(list)
    for summary in summaries:
        grouped[(summary.player_id, summary.metric)].append(summary)
    checks = []
    for (player, key), group in grouped.items():
        for phases in [
            ("Regular Season",),
            ("Playoffs",),
            ("Regular Season", "Playoffs"),
        ]:
            selected = [s for phase in phases for s in group if s.phase == phase]
            rows = [
                r
                for phase in phases
                for r in evidence.rows
                if r["player_id"] == player and r["season_type"] == phase
            ]
            for aggregation in contract.metric(key).aggregations:
                if not selected:
                    try:
                        aggregate_summaries(selected, contract.metric(key), aggregation)
                    except SemanticError as exc:
                        checks.append(
                            dict(
                                player_id=player,
                                metric=key,
                                phases=list(phases),
                                kind="empty_membership_refusal",
                                passed=exc.code == "unsupported_coverage",
                            )
                        )
                        continue
                    raise AssertionError("Empty membership was accepted")
                actual = aggregate_summaries(
                    selected, contract.metric(key), aggregation
                )
                expected = aggregate(rows, contract.metric(key), aggregation)
                checks.append(
                    dict(
                        player_id=player,
                        metric=key,
                        aggregation=aggregation,
                        phases=list(phases),
                        passed=equivalent(actual, expected),
                    )
                )
    return dict(
        kind="frozen_synthetic_serving_parity",
        variant=variant,
        parity_scope="aggregate_arithmetic_only",
        endpoint_payload_parity_established=False,
        source_snapshot_id=evidence.snapshot_id,
        checks=checks,
        passed=all(c["passed"] for c in checks),
        manifest=manifest,
    )


def measure_pilot(snapshot: dict, *, now: datetime, repetitions: int = 30) -> dict:
    """Paired offline full-result benchmark; excludes endpoints, models and BigQuery.

    Construction is measured separately. Caller owns the private result artifact.
    """
    import statistics
    from time import perf_counter

    from app.agent.semantics import Query, run_query
    from app.agent.serving_runtime import PilotPublication

    if type(repetitions) is not int or not 2 <= repetitions <= 100:
        raise ValueError("Repetitions must be 2..100")
    source = snapshot_evidence(snapshot)
    started = perf_counter()
    publication = PilotPublication.build(
        snapshot,
        expected_snapshot_id=source.snapshot_id,
        publication_id="offline-pilot",
        relation_version="offline-pilot/1",
        now=now,
    )
    build_ms = (perf_counter() - started) * 1000
    player = source.rows[0]["player_id"]
    records = []
    for phase in ("Regular Season", "Playoffs", "Both"):
        for aggregation in ("average", "total"):
            query = Query(
                metric="pts",
                season=source.rows[0]["season"],
                season_type=phase,
                aggregation=aggregation,
                player_id=player,
            )
            if not publication.approval.supports(query, now=now):
                continue
            for attempt in range(repetitions):
                values = {}
                timings = {}
                arms = (
                    ("facts", "serving") if attempt % 2 == 0 else ("serving", "facts")
                )
                for arm in arms:
                    started = perf_counter()
                    values[arm] = (
                        run_query(source, query)
                        if arm == "facts"
                        else publication.run(query, now=now)
                    )
                    timings[arm] = (perf_counter() - started) * 1000
                records.append(
                    dict(
                        phase=phase,
                        aggregation=aggregation,
                        attempt=attempt,
                        passed=equivalent(values["facts"], values["serving"]),
                        **timings,
                    )
                )
    return dict(
        kind="offline_full_result_pilot",
        source_snapshot_id=source.snapshot_id,
        endpoint_latency_measured=False,
        warehouse_cost_measured=False,
        clock="controlled_synthetic",
        build_ms=build_ms,
        records=records,
        passed=bool(records) and all(r["passed"] for r in records),
        median_facts_ms=statistics.median(r["facts"] for r in records)
        if records
        else None,
        median_serving_ms=statistics.median(r["serving"] for r in records)
        if records
        else None,
    )


def measure_endpoint_arms(
    factories: dict, *, question: str, season: str, warm: int = 30, cold: int = 10
) -> dict:
    """Paired real JSON/SSE handler measurements using caller-owned test clients.

    Factories are context managers accepting cold=True/False. Transport/network
    mode, source trust, read/provider budgets and trace capture belong to the
    owning evaluator. This helper never changes environment or starts resources.
    """
    import statistics
    from time import perf_counter

    if not 1 <= warm <= 100 or not 0 <= cold <= 30 or not 2 <= len(factories) <= 5:
        raise ValueError("Endpoint pilot exceeds bounded sample/arm counts")
    records = []
    payloads = []
    for cache, count in (("warm", warm), ("cold", cold)):
        for suffix in ("", "/stream"):
            for attempt in range(count):
                names = list(factories)
                if attempt % 2:
                    names.reverse()
                pair = {}
                for name in names:
                    with factories[name](cold=cache == "cold") as client:
                        started = perf_counter()
                        response = client.post(
                            "/api/agent/ask" + suffix + "?season=" + season,
                            json={"question": question},
                        )
                        response.raise_for_status()
                        if suffix:
                            events = [
                                json.loads(line[6:])
                                for line in response.text.splitlines()
                                if line.startswith("data: ")
                            ]
                            payload = next(
                                e["payload"] for e in events if e.get("type") == "final"
                            )
                        else:
                            payload = response.json()
                        elapsed = (perf_counter() - started) * 1000
                        records.append(
                            dict(
                                arm=name,
                                cache=cache,
                                transport="sse" if suffix else "json",
                                attempt=attempt,
                                status=payload["status"],
                                elapsed_ms=elapsed,
                                model_calls=payload.get("semantic_plan", {}).get(
                                    "model_calls"
                                ),
                            )
                        )
                        pair[name] = payload
                payloads.append(
                    dict(cache=cache, transport=suffix, attempt=attempt, arms=pair)
                )
    summary = []
    for name in factories:
        for cache in ("warm", "cold"):
            rows = [r for r in records if r["arm"] == name and r["cache"] == cache]
            if rows:
                summary.append(
                    dict(
                        arm=name,
                        cache=cache,
                        attempts=len(rows),
                        successes=sum(r["status"] == "ok" for r in rows),
                        median_ms=statistics.median(r["elapsed_ms"] for r in rows),
                    )
                )
    return dict(
        records=records,
        payloads=payloads,
        summary=summary,
        bounded_pilot=True,
        p95_commitment=False,
    )
