"""Bounded, explicitly authorized two-season offline PTS composition.

This module is not registered with application routes or a runtime source.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from app.agent.snapshot_source import CurrentPTSAskShadow, HistoricalPTSAskShadow
from app.serving.comparison import (
    CurrentPTSComparisonShadow,
    HistoricalPTSComparisonShadow,
    PTSComparisonRequest,
)
from app.serving.context import PTSShadowRequest, ReadContext
from app.serving.contract import ServingError, canonical_json, content_digest
from app.serving.current import CurrentReadContext, CurrentSnapshotHandle
from app.serving.player import CurrentPlayerPTSShadow, PlayerPTSShadow
from app.serving.snapshot import SnapshotHandle

POLICY = "observed-pts-two-season-side-by-side/1"
MAX_ROWS = 100_000
MAX_BYTES = 100 * 1024 * 1024
MAX_SHARDS = 16
MAX_OUTPUT = 1024 * 1024
_CONSTRUCTION = object()


def bounded_json(value):
    """Stop serialization at the whole-response budget."""
    encoder = json.JSONEncoder(
        sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    result = bytearray()
    for part in encoder.iterencode(value):
        data = part.encode("utf-8")
        require(len(result) + len(data) <= MAX_OUTPUT, "budget_exceeded")
        result.extend(data)
    return bytes(result)


def require(condition, code):
    if not condition:
        raise ServingError(code)


@dataclass(frozen=True)
class CompositionAuthority:
    """Caller-reviewed inputs, independently supplied from either artifact.

    IDs and approved players are explicit compatibility decisions, not inferred
    from matching numeric IDs. The caller remains responsible for their trust.
    """

    policy_id: str
    identity_record_id: str
    historical_identity_id: str
    current_identity_id: str
    approved_player_ids: frozenset[int]
    allowed_operation: str = "side_by_side"

    def __post_init__(self):
        import re

        require(self.policy_id == POLICY, "composition_policy_denied")
        require(self.allowed_operation == "side_by_side", "composition_policy_denied")
        require(
            all(
                type(v) is str and re.fullmatch(r"[0-9a-f]{64}", v)
                for v in (
                    self.identity_record_id,
                    self.historical_identity_id,
                    self.current_identity_id,
                )
            ),
            "identity_compatibility_denied",
        )
        require(
            type(self.approved_player_ids) is frozenset
            and 0 < len(self.approved_player_ids) <= 10000
            and all(
                type(v) is int and 0 < v <= 2147483647 for v in self.approved_player_ids
            ),
            "identity_compatibility_denied",
        )


class TwoSeasonPTS:
    """Explicit offline composition, bound to independently supplied authorities."""

    def __init__(self, contexts, binding, authority, *, _token=None):
        require(_token is _CONSTRUCTION, "composition_policy_denied")
        self._contexts = contexts
        self._binding = canonical_json(binding)
        self._authority = authority

    @classmethod
    def open(
        cls,
        historical_root,
        historical_policy,
        current_root,
        current_policy,
        *,
        historical_snapshot_id,
        current_snapshot_id,
        authority,
    ):
        require(type(authority) is CompositionAuthority, "composition_policy_denied")
        # Both manifests are validated and authorized before any shard is read.
        handles = (
            SnapshotHandle.preflight(historical_root, historical_policy),
            CurrentSnapshotHandle.preflight(current_root, current_policy),
        )
        manifests = tuple(h._manifest_only() for h in handles)
        require(
            [m["scope"]["season"] for m in manifests] == ["2025-26", "2026-27"]
            and all(m["scope"]["phase"] == "Regular Season" for m in manifests),
            "capability_not_evaluated",
        )
        require(
            [m["snapshot_id"] for m in manifests]
            == [historical_snapshot_id, current_snapshot_id],
            "snapshot_binding_mismatch",
        )
        require(
            manifests[0]["scope"]["end_date"] < manifests[1]["scope"]["start_date"],
            "capability_not_evaluated",
        )
        require(
            manifests[0]["metric_semantics_id"] == manifests[1]["metric_semantics_id"],
            "semantic_mismatch",
        )
        require(
            [m["lineage"]["identity_id"] for m in manifests]
            == [authority.historical_identity_id, authority.current_identity_id],
            "identity_compatibility_denied",
        )
        require(
            sum(m["row_count"] for m in manifests) <= MAX_ROWS
            and sum(len(m["shards"]) for m in manifests) <= MAX_SHARDS
            and sum(s["byte_count"] for m in manifests for s in m["shards"])
            <= MAX_BYTES,
            "budget_exceeded",
        )
        contexts = (
            ReadContext.bind(
                handles[0],
                expected_snapshot_id=historical_snapshot_id,
                scope=manifests[0]["scope"],
            ),
            CurrentReadContext.bind(
                handles[1],
                expected_snapshot_id=current_snapshot_id,
                scope=manifests[1]["scope"],
            ),
        )
        binding = {
            "schema": "shared-serving-two-season/1",
            "policy_id": authority.policy_id,
            "identity_record_id": authority.identity_record_id,
            "approved_player_ids_sha256": content_digest(
                sorted(authority.approved_player_ids)
            ),
            "operation": authority.allowed_operation,
            "members": [c.binding for c in contexts],
            "freshness_policy_ids": [m["freshness_policy_id"] for m in manifests],
        }
        binding["selection_id"] = content_digest(binding)
        return cls(contexts, binding, authority, _token=_CONSTRUCTION)

    @property
    def binding(self):
        return json.loads(self._binding)

    def _authorize(self):
        return [context.authorize() for context in self._contexts]

    @staticmethod
    def _same_status(before, after):
        require(
            all(
                a["status_id"] == b["status_id"]
                and a["status_checked_at"] == b["status_checked_at"]
                for a, b in zip(before, after)
            ),
            "authority_changed",
        )

    def _answer(self, player_ids, aggregation, consumer):
        require(
            type(player_ids) is tuple
            and len(player_ids) in (1, 2)
            and all(
                type(p) is int and p in self._authority.approved_player_ids
                for p in player_ids
            ),
            "identity_compatibility_denied",
        )
        require(
            len(set(player_ids)) == len(player_ids), "identity_compatibility_denied"
        )
        require(aggregation in ("total", "average"), "capability_not_evaluated")
        before = self._authorize()
        members = []
        for i, context in enumerate(self._contexts):
            scope = context.binding["scope"]
            if len(player_ids) == 2:
                require(consumer == "ask", "capability_not_evaluated")
                adapter = (HistoricalPTSComparisonShadow, CurrentPTSComparisonShadow)[
                    i
                ](context)
                request = PTSComparisonRequest.create(
                    player_ids, aggregation, scope=scope
                )
                payload = adapter.answer(request)
            else:
                classes = (
                    (HistoricalPTSAskShadow, CurrentPTSAskShadow)
                    if consumer == "ask"
                    else (PlayerPTSShadow, CurrentPlayerPTSShadow)
                )
                require(consumer in ("ask", "player"), "capability_not_evaluated")
                adapter = classes[i](context)
                request = PTSShadowRequest.create(
                    player_ids[0], aggregation, scope=scope
                )
                payload = (
                    adapter.answer(request)
                    if consumer == "ask"
                    else adapter.get_pts(request)
                )
            members.append(payload)
        result = {
            "schema": "shared-serving-two-season-result/1",
            "binding": self.binding,
            "consumer": consumer,
            "player_ids": list(player_ids),
            "aggregation": aggregation,
            "members": members,
            "runtime_eligible": False,
            "cohort_complete": False,
        }
        bounded_json(result)
        self._same_status(before, self._authorize())
        for context, payload in zip(self._contexts, members):
            context.check_emission(payload)
        return result

    def answer(self, player_ids, aggregation, *, consumer="ask"):
        return self._answer(player_ids, aggregation, consumer)

    def answer_json(self, player_ids, aggregation, *, consumer="ask"):
        result = self._answer(player_ids, aggregation, consumer)
        data = bounded_json(result)
        for context, payload in zip(self._contexts, result["members"]):
            context.check_emission(payload)
        return data

    def answer_sse(self, player_ids, aggregation):
        data = self.answer_json(player_ids, aggregation)
        payload = json.loads(data)
        for context, member in zip(self._contexts, payload["members"]):
            context.check_emission(member)
        yield "event: final\ndata: " + data.decode("utf-8") + "\n\n"
