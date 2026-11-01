"""Closed observed-PTS shadow presentation, bound by the public claim validator."""

from __future__ import annotations

from app.agent import claims
from app.serving.context import ADAPTER_VERSION, AGGREGATE_FIELDS
from app.serving.contract import (
    PROJECTION_VERSION,
    ServingError,
    canonical_json,
    content_digest,
    historical_limitations,
)

KIND = "observed_pts_shadow"
FIELDS = ("game_date", "game_id", "pts")


def validate_envelope(payload):
    """Closed shadow metadata contract; it never grants present read authority."""
    e = payload["semantic_evidence"]
    if set(e) != {
        "kind",
        "binding",
        "player_id",
        "aggregation",
        "metric",
        "aggregates",
        "games",
        "limitations",
        "identity_mode",
        "cohort_complete",
        "runtime_eligible",
        "labels",
        "authority",
        "evidence_clocks",
    }:
        raise ValueError("Unexpected shadow evidence fields")
    required = {
        "schema",
        "consumer",
        "binding",
        "semantic_evidence",
        "limitations",
        "identity_mode",
        "runtime_eligible",
        "cohort_complete",
        "authority",
    }
    consumer = payload["consumer"]
    visible = "player_profile" if consumer == "player" else "tables"
    required.add(visible)
    if not required <= payload.keys() or not payload.keys() <= required | {
        "claim_bindings",
        "claim_validation",
    }:
        raise ValueError("Unexpected shadow envelope fields")
    if payload["schema"] != "historical-pts-shadow-payload/1" or consumer not in (
        "ask",
        "player",
    ):
        raise ValueError("Unsupported shadow envelope")
    for key in (
        "binding",
        "limitations",
        "identity_mode",
        "runtime_eligible",
        "cohort_complete",
        "authority",
    ):
        if payload[key] != e[key]:
            raise ValueError("Shadow envelope contradicts bound evidence")
    if (
        e["kind"] != KIND
        or e["runtime_eligible"] is not False
        or e["cohort_complete"] is not False
        or e["identity_mode"] != "canonical_id_only"
        or e["aggregation"] not in ("total", "average")
        or e["metric"] != {"key": "pts", "unit": "count"}
        or type(e["player_id"]) is not int
        or e["player_id"] <= 0
        or e["limitations"] != historical_limitations(e["evidence_clocks"])
    ):
        raise ValueError("Unsupported shadow capability")
    if set(e["aggregates"]) != {"total", "average"} or any(
        set(result) != set(AGGREGATE_FIELDS) for result in e["aggregates"].values()
    ):
        raise ValueError("Unsupported nested shadow aggregate fields")
    binding = e["binding"]
    if (
        set(binding)
        != {
            "adapter_version",
            "snapshot_id",
            "projection_version",
            "metric_semantics_id",
            "scope",
            "lineage",
            "dependency_ids",
            "capabilities",
            "context_id",
        }
        or binding["adapter_version"] != ADAPTER_VERSION
        or binding["projection_version"] != PROJECTION_VERSION
        or canonical_json(binding["capabilities"])
        != canonical_json(
            {
                "metric_id": "pts",
                "aggregations": ["total", "average"],
                "observed_sample": True,
                "cohort_complete": False,
                "runtime_eligible": False,
            }
        )
    ):
        raise ValueError("Unsupported shadow binding")
    if binding["context_id"] != content_digest(
        {k: v for k, v in binding.items() if k != "context_id"}
    ):
        raise ValueError("Shadow context digest changed")
    labels = [
        {"label": label}
        for label in (
            "Player ID",
            "Points total"
            if e["aggregation"] == "total"
            else "Points per valid appearance",
            "Observed appearances",
            "Valid PTS appearances",
            "Missing PTS appearances",
        )
    ]
    if e["labels"] != labels or any(
        set(row) != {"game_id", "game_date", "team_id", "pts"} for row in e["games"]
    ):
        raise ValueError("Unsupported shadow game or label fields")
    if consumer == "ask":
        tables = payload["tables"]
        if len(tables) != 1 or set(tables[0]) != {"columns", "rows"}:
            raise ValueError("Unsupported shadow tables")
        if len(tables[0]["columns"]) != 5 or any(
            set(c) != {"label"} for c in tables[0]["columns"]
        ):
            raise ValueError("Unsupported shadow columns")
        if len(tables[0]["rows"]) != 1 or len(tables[0]["rows"][0]) != 5:
            raise ValueError("Unsupported shadow rows")
    else:
        profile = payload["player_profile"]
        if set(profile) != {
            "player_id",
            "total_pts",
            "average_pts",
            "observed_games",
            "valid_games",
            "missing_component_games",
            "game_log",
        }:
            raise ValueError("Unsupported shadow profile")
        if len(profile["game_log"]) != len(e["games"]) or any(
            set(r) != set(FIELDS) for r in profile["game_log"]
        ):
            raise ValueError("Unsupported shadow game log")


def claim_contracts(payload):
    """Independent destination coordinates; no lookup by matching output values."""
    validate_envelope(payload)
    e = payload["semantic_evidence"]
    base = ["semantic_evidence"]
    identity = base + ["player_id"]
    scope = base + ["binding", "scope"]
    context = [base + ["metric"], base + ["binding"]]
    result = {}

    def add(dest, source, template=None):
        spec = (
            {"op": "identity"}
            if template is None
            else {"op": "template", "template": template}
        )
        result[tuple(dest)] = ([source], identity, scope, context, spec)

    if payload["consumer"] == "player":
        for target, source in (
            ("player_id", identity),
            ("total_pts", base + ["aggregates", "total", "display_value"]),
            ("average_pts", base + ["aggregates", "average", "display_value"]),
            *[
                (field, base + ["aggregates", "total", field])
                for field in (
                    "observed_games",
                    "valid_games",
                    "missing_component_games",
                )
            ],
        ):
            add(["player_profile", target], source)
        for i, _ in enumerate(e["games"]):
            for field in FIELDS:
                add(
                    ["player_profile", "game_log", i, field], base + ["games", i, field]
                )
    elif payload["consumer"] == "ask":
        mode = e["aggregation"]
        fields = (
            "player_id",
            "display_value",
            "observed_games",
            "valid_games",
            "missing_component_games",
        )
        labels = (
            "Player ID",
            "Points total" if mode == "total" else "Points per valid appearance",
            "Observed appearances",
            "Valid PTS appearances",
            "Missing PTS appearances",
        )
        for i, (field, label) in enumerate(zip(fields, labels)):
            source = (
                identity if field == "player_id" else base + ["aggregates", mode, field]
            )
            add(["tables", 0, "rows", 0, i], source)
            if e["labels"][i] != {"label": label}:
                raise ValueError("Unsupported observed-PTS label")
            add(["tables", 0, "columns", i, "label"], base + ["labels", i, "label"])
    else:
        raise ValueError("Unsupported shadow consumer")
    return result


def _render(evidence, consumer, authority):
    evidence["authority"] = authority
    mode = evidence["aggregation"]
    evidence["labels"] = [
        {"label": label}
        for label in (
            "Player ID",
            "Points total" if mode == "total" else "Points per valid appearance",
            "Observed appearances",
            "Valid PTS appearances",
            "Missing PTS appearances",
        )
    ]
    payload = {
        "schema": "historical-pts-shadow-payload/1",
        "authority": authority,
        "consumer": consumer,
        "binding": evidence["binding"],
        "semantic_evidence": evidence,
        "limitations": evidence["limitations"],
        "identity_mode": evidence["identity_mode"],
        "runtime_eligible": False,
        "cohort_complete": False,
    }
    if consumer == "player":
        total, average = (evidence["aggregates"][mode] for mode in ("total", "average"))
        payload["player_profile"] = {
            "player_id": evidence["player_id"],
            "total_pts": total["display_value"],
            "average_pts": average["display_value"],
            **{
                key: total[key]
                for key in ("observed_games", "valid_games", "missing_component_games")
            },
            "game_log": [
                {key: row[key] for key in FIELDS} for row in evidence["games"]
            ],
        }
    elif consumer == "ask":
        mode = evidence["aggregation"]
        result = evidence["aggregates"][mode]
        payload["tables"] = [
            {
                "columns": [
                    {"label": label}
                    for label in (
                        "Player ID",
                        "Points total"
                        if mode == "total"
                        else "Points per valid appearance",
                        "Observed appearances",
                        "Valid PTS appearances",
                        "Missing PTS appearances",
                    )
                ],
                "rows": [
                    [
                        evidence["player_id"],
                        result["display_value"],
                        result["observed_games"],
                        result["valid_games"],
                        result["missing_component_games"],
                    ]
                ],
            }
        ]
    else:
        raise ServingError("capability_not_evaluated")
    for destination, (sources, identity, scope, context, spec) in claim_contracts(
        payload
    ).items():
        claims.bind(
            payload,
            destination=list(destination),
            sources=sources,
            identity=identity,
            scope=scope,
            context=context,
            spec=spec,
            metric="pts",
            unit="count",
        )
    validation = claims.validate(payload)
    if validation["status"] != "complete":
        raise ServingError("claim_binding_mismatch")
    payload["claim_validation"] = validation
    return payload


def render(evidence, consumer, authority):
    try:
        return _render(evidence, consumer, authority)
    except (ValueError, TypeError, KeyError, IndexError):
        raise ServingError("claim_binding_mismatch") from None
