"""Offline serving parity over the endpoint harness's existing frozen source.

This companion does not enable serving routes or measure deployment latency.
"""

from __future__ import annotations

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
