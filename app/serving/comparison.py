"""Offline observed PTS side-by-side comparison; no edges or cohort claims."""

from __future__ import annotations

import json
from dataclasses import dataclass

from app.agent import claims
from app.serving.context import PTSShadowRequest, ReadContext
from app.serving.contract import ServingError, canonical_json
from app.serving.presentation import render, validate_envelope

KIND = "observed_pts_comparison_shadow"
POLICY = "observed_pts_side_by_side/1"
LIMITATIONS = ["side_by_side_observed_samples_only", "comparison_edges_unavailable"]
FIELDS = (
    "player_id",
    "display_value",
    "observed_games",
    "valid_games",
    "missing_component_games",
)
MAX_OUTPUT_BYTES = 1024 * 1024


@dataclass(frozen=True)
class PTSComparisonRequest:
    player_ids: tuple[int, int]
    aggregation: str
    scope_bytes: bytes

    @classmethod
    def create(cls, player_ids, aggregation, *, scope):
        if type(player_ids) not in (tuple, list) or len(player_ids) != 2:
            raise ServingError("capability_not_evaluated")
        request = PTSShadowRequest.create(player_ids[0], aggregation, scope=scope)
        return cls(tuple(player_ids), aggregation, request.scope_bytes)


def _side_payload(side):
    mode = side["aggregation"]
    return {
        "schema": "historical-pts-shadow-payload/1",
        "consumer": "ask",
        **{
            key: side[key]
            for key in (
                "binding",
                "authority",
                "limitations",
                "identity_mode",
                "runtime_eligible",
                "cohort_complete",
            )
        },
        "semantic_evidence": side,
        "tables": [
            {
                "columns": side["labels"],
                "rows": [
                    [
                        side["player_id"],
                        *[side["aggregates"][mode][key] for key in FIELDS[1:]],
                    ]
                ],
            }
        ],
    }


def claim_contracts(payload):
    """Closed comparison envelope and exact public claim destinations."""
    e = payload["semantic_evidence"]
    required = {
        "schema",
        "consumer",
        "binding",
        "authority",
        "limitations",
        "identity_mode",
        "runtime_eligible",
        "cohort_complete",
        "semantic_evidence",
        "tables",
    }
    if not required <= payload.keys() or not payload.keys() <= required | {
        "claim_bindings",
        "claim_validation",
    }:
        raise ValueError("Unexpected comparison envelope")
    if set(e) != {
        "kind",
        "policy",
        "binding",
        "authority",
        "limitations",
        "identity_mode",
        "runtime_eligible",
        "cohort_complete",
        "player_ids",
        "aggregation",
        "sides",
        "labels",
    }:
        raise ValueError("Unexpected comparison evidence")
    if (
        payload["schema"] != "historical-pts-comparison-payload/1"
        or payload["consumer"] != "comparison"
        or e["kind"] != KIND
        or e["policy"] != POLICY
    ):
        raise ValueError("Unsupported comparison")
    for key in (
        "binding",
        "authority",
        "limitations",
        "identity_mode",
        "runtime_eligible",
        "cohort_complete",
    ):
        if canonical_json(payload[key]) != canonical_json(e[key]):
            raise ValueError("Contradictory comparison metadata")
    if (
        e["runtime_eligible"] is not False
        or e["cohort_complete"] is not False
        or e["identity_mode"] != "canonical_id_only"
        or e["aggregation"] not in ("total", "average")
        or type(e["player_ids"]) is not list
        or len(e["player_ids"]) != 2
        or any(
            type(pid) is not int or not 0 < pid <= 2147483647 for pid in e["player_ids"]
        )
        or len(set(e["player_ids"])) != 2
        or type(e["sides"]) is not list
        or len(e["sides"]) != 2
    ):
        raise ValueError("Unsupported comparison request")
    for i, side in enumerate(e["sides"]):
        validate_envelope(_side_payload(side))
        if (
            side["player_id"] != e["player_ids"][i]
            or side["aggregation"] != e["aggregation"]
            or canonical_json(side["binding"]) != canonical_json(e["binding"])
            or canonical_json(side["authority"]) != canonical_json(e["authority"])
            or side["evidence_clocks"] != e["sides"][0]["evidence_clocks"]
            or side["labels"] != e["labels"]
        ):
            raise ValueError("Mixed comparison binding")
    if e["limitations"] != e["sides"][0]["limitations"] + LIMITATIONS:
        raise ValueError("Missing comparison restrictions")
    tables = payload["tables"]
    if (
        type(tables) is not list
        or len(tables) != 1
        or set(tables[0]) != {"columns", "rows"}
        or tables[0]["columns"] != e["labels"]
        or len(tables[0]["rows"]) != 2
        or any(type(row) is not list or len(row) != 5 for row in tables[0]["rows"])
    ):
        raise ValueError("Unsupported comparison table")
    result = {}
    base = ["semantic_evidence"]
    for i in range(2):
        side = base + ["sides", i]
        identity = side + ["player_id"]
        for j, key in enumerate(FIELDS):
            source = (
                identity if j == 0 else side + ["aggregates", e["aggregation"], key]
            )
            result[("tables", 0, "rows", i, j)] = (
                [source],
                identity,
                base + ["binding", "scope"],
                [base + ["binding"], base + ["policy"], side + ["metric"]],
                {"op": "identity"},
            )
    for j in range(5):
        result[("tables", 0, "columns", j, "label")] = (
            [base + ["labels", j, "label"]],
            base + ["player_ids"],
            base + ["binding", "scope"],
            [base + ["binding"], base + ["policy"]],
            {"op": "identity"},
        )
    return result


def _render(sides, authority):
    sides = [render(side, "ask", authority)["semantic_evidence"] for side in sides]
    e = {
        "kind": KIND,
        "policy": POLICY,
        "player_ids": [s["player_id"] for s in sides],
        "aggregation": sides[0]["aggregation"],
        "sides": sides,
        "labels": sides[0]["labels"],
        **{
            key: sides[0][key]
            for key in (
                "binding",
                "identity_mode",
                "runtime_eligible",
                "cohort_complete",
            )
        },
        "authority": authority,
        "limitations": sides[0]["limitations"] + LIMITATIONS,
    }
    payload = {
        "schema": "historical-pts-comparison-payload/1",
        "consumer": "comparison",
        **{
            key: e[key]
            for key in (
                "binding",
                "authority",
                "limitations",
                "identity_mode",
                "runtime_eligible",
                "cohort_complete",
            )
        },
        "semantic_evidence": e,
        "tables": [
            {
                "columns": e["labels"],
                "rows": [_side_payload(s)["tables"][0]["rows"][0] for s in sides],
            }
        ],
    }
    for dest, (sources, identity, scope, context, spec) in claim_contracts(
        payload
    ).items():
        claims.bind(
            payload,
            destination=list(dest),
            sources=sources,
            identity=identity,
            scope=scope,
            context=context,
            spec=spec,
            metric="pts",
            unit="count",
        )
    check = claims.validate(payload)
    if check["status"] != "complete":
        raise ServingError("claim_binding_mismatch")
    payload["claim_validation"] = check
    return payload


class HistoricalPTSComparisonShadow:
    """Two explicit canonical IDs, one context, all-or-refuse side-by-side output."""

    def __init__(self, context: ReadContext):
        self._context = context

    def answer(self, request: PTSComparisonRequest):
        if (
            type(request) is not PTSComparisonRequest
            or type(request.player_ids) is not tuple
            or len(request.player_ids) != 2
            or any(
                type(pid) is not int or not 0 < pid <= 2147483647
                for pid in request.player_ids
            )
            or len(set(request.player_ids)) != 2
        ):
            raise ServingError("capability_not_evaluated")
        sides = [
            self._context.observe(
                PTSShadowRequest(pid, request.aggregation, request.scope_bytes)
            )
            for pid in request.player_ids
        ]
        try:
            payload = _render(sides, self._context.authorize())
            if len(canonical_json(payload)) > MAX_OUTPUT_BYTES:
                raise ServingError("comparison_response_budget")
        except (
            ValueError,
            TypeError,
            KeyError,
            IndexError,
            OverflowError,
            AttributeError,
            RecursionError,
        ) as exc:
            if isinstance(exc, ServingError):
                raise
            raise ServingError("claim_binding_mismatch") from None
        self._context.check_emission(payload)
        return payload

    def answer_json(self, request: PTSComparisonRequest):
        payload = self.answer(request)
        data = canonical_json(payload)
        self._context.check_emission(payload)
        return data

    def answer_sse(self, request: PTSComparisonRequest):
        data = self.answer_json(request).decode("utf-8")
        self._context.check_emission(json.loads(data))
        yield "event: final\ndata: " + data + "\n\n"
