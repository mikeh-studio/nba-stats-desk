"""Explicit evidence-to-presentation bindings, independent of renderer output.

Paths are arrays of dictionary keys/list offsets. No value search or numerical
matching is performed. Digests identify evidence, not its real-world accuracy.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Sequence
from copy import deepcopy
from typing import Any

VERSION = "ask_claim_bindings/1"
EVIDENCE_ROOTS = {
    "semantic_evidence",
    "reference_evidence",
    "availability_evidence",
    "appearance_evidence",
    "research",
    "claim_evidence",
}
VISIBLE_ROOTS = (
    "answer",
    "assumptions",
    "scope_assumptions",
    "tables",
    "charts",
    "player_profile",
    "player_profiles",
    "research_highlights",
    "comparison_explorer",
    "metric_definitions",
)


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def resolve(document: Any, path: list) -> Any:
    if not isinstance(path, list) or not path or len(path) > 32:
        raise ValueError("Invalid claim path")
    value = document
    for key in path:
        if (
            isinstance(value, (list, tuple))
            and type(key) is int
            and 0 <= key < len(value)
        ):
            value = value[key]
        elif (
            isinstance(value, dict)
            and isinstance(key, str)
            and key in value
            and not key.startswith("_")
        ):
            value = value[key]
        else:
            raise ValueError("Missing or unsafe claim path")
    return value


def transform(values: list, spec: dict) -> Any:
    """Small fixed algebra; null is never silently replaced with zero."""
    if not values:
        raise ValueError("Missing operands")
    value: Any
    op = spec.get("op", "identity")
    if op == "nominal_p":
        if (
            len(values) != 1
            or type(values[0]) not in (int, float)
            or not 0 <= values[0] <= 1
        ):
            raise ValueError("Invalid nominal p")
        return "<0.001" if values[0] < 0.001 else f"{values[0]:.3f}"
    if op == "template":
        template = spec["template"]
        # Only numbered slots; no attribute/index traversal or arbitrary format.
        if re.search(r"\{(?!\d+\})|(?<!\{)\}", re.sub(r"\{\d+\}", "", template)):
            raise ValueError("Unsafe template")
        slots = [int(v) for v in re.findall(r"\{(\d+)\}", template)]
        if set(slots) != set(range(len(values))):
            raise ValueError("Template must use every operand")
        if re.search(r"\d", re.sub(r"\{\d+\}", "", template)):
            raise ValueError("Numeric template literals are not evidence")
        formats = spec.get("formats", ["identity"] * len(values))
        if len(formats) != len(values) or any(
            f
            not in (
                "identity",
                "join_comma",
                "signed2",
                "phase_lower",
                "words",
                "warnings",
            )
            for f in formats
        ):
            raise ValueError("Invalid operand format")
        values = [
            ", ".join(v)
            if f == "join_comma" and isinstance(v, list)
            else ("regular season and playoffs" if v == "Both" else v.lower())
            if f == "phase_lower"
            else v.replace("_", " ")
            if f == "words"
            else ("; ".join(v) or "—")
            if f == "warnings"
            else f"{v:+.2f}"
            if f == "signed2"
            else v
            for v, f in zip(values, formats)
        ]
        return template.format(*values)
    if op == "count":
        if len(values) != 1 or not isinstance(values[0], list):
            raise ValueError("Count requires a list")
        value = len(values[0])
    elif op == "difference":
        if len(values) != 2:
            raise ValueError("Difference requires two operands")
        value = None if None in values else values[0] - values[1]
    elif op == "identity":
        if len(values) != 1:
            raise ValueError("Identity requires one operand")
        value = values[0]
    else:
        raise ValueError("Unknown transform")
    if value is None:
        return spec.get("null", None)
    if isinstance(value, (float, int)) and not isinstance(value, bool):
        if not math.isfinite(value):
            raise ValueError("Nonfinite claim")
        scale = spec.get("scale", 1)
        if scale not in (1, 100):
            raise ValueError("Unsupported scale")
        value *= scale
    elif spec.get("scale", 1) != 1:
        raise ValueError("Non-numeric scale")
    if "round" in spec:
        precision = spec["round"]
        if (
            type(precision) is not int
            or not 0 <= precision <= 6
            or type(value) not in (int, float)
        ):
            raise ValueError("Invalid rounding")
        value = round(value, precision)
    if "digits" in spec:
        digits = spec["digits"]
        if (
            type(digits) is not int
            or not 0 <= digits <= 6
            or type(value) not in (int, float)
        ):
            raise ValueError("Invalid precision")
        if spec.get("normalize_zero") and round(value, digits) == 0:
            value = 0.0
        value = format(
            value,
            (
                "+"
                if spec.get("signed") and not (spec.get("unsigned_zero") and value == 0)
                else ""
            )
            + f".{digits}f",
        )
    elif spec.get("string"):
        value = str(value)
    return value + spec.get("suffix", "") if isinstance(value, str) else value


def descriptor(payload, sources, context):
    """Derive semantic descriptors from exact evidence fields or fixed field rules."""
    field = sources[0][-1]
    if sources[0][:2] == ["claim_evidence", "frozen_study"]:
        return (
            ("sample_count", "games")
            if field in ("original_played_n", "original_out_n", "played_n", "out_n")
            else ("nominal_p", "dimensionless")
            if field == "nominal_p"
            else ("ast", "assists per game")
        )
    fixed = {
        "season": ("season", "season"),
        "start": ("start", "date"),
        "end": ("end", "date"),
        "key": ("metric_key", "label"),
        "seasons": ("seasons", "season"),
        "as_of": ("as_of", "date"),
        "window_start": ("window_start", "date"),
        "opponent": ("opponent", "label"),
        "participation": ("participation", "label"),
        "reason": ("reason", "label"),
        "report_status": ("report_status", "label"),
        "warnings": ("warnings", "label"),
        "window": ("window", "label"),
        "home_away": ("home_away", "label"),
        "size": ("cohort_size", "players"),
        "min_games": ("min_games", "games"),
        "excluded_players": ("excluded_players", "players"),
        "min_attempts": ("min_attempts", "attempts"),
        "attempt_field": ("attempt_field", "label"),
        "source": ("source", "label"),
        "snapshot_id": ("snapshot_id", "identifier"),
        "data_through": ("data_through", "date"),
        "missing_component_games": ("missing_component_games", "games"),
        "player_name": ("player_name", "label"),
        "player_id": ("player_id", "identifier"),
        "label": ("label", "label"),
        "game_date": ("game_date", "label"),
        "game_id": ("game_id", "label"),
        "season_type": ("season_type", "label"),
        "team_abbr": ("team_abbr", "label"),
        "opponent_abbr": ("opponent_abbr", "label"),
        "observed_games": ("observed_games", "games"),
        "valid_games": ("valid_games", "games"),
        "filter_missing_games": ("filter_missing_games", "games"),
        "rank": ("rank", "ordinal"),
        "percentile": ("percentile", "percentile"),
        "similarity_score": ("similarity_score", "dimensionless"),
        "games": ("gp", "games"),
        "start_date": ("sample_dates", "date"),
        "end_date": ("sample_dates", "date"),
        "unit": ("unit", "label"),
        "aggregation": ("aggregation", "label"),
    }
    if type(field) is int and "groups" in sources[0]:
        return "group_label", "label"
    if field in fixed:
        return fixed[field]
    for path in context:
        definition = resolve(payload, path)
        if (
            isinstance(definition, dict)
            and "unit" in definition
            and ("key" in definition or "metric" in definition)
        ):
            unit = definition["unit"]
            if field == "difference" and unit in ("ratio", "%", "percent"):
                unit = "percentage points"
            return definition.get("key", definition.get("metric")), unit
    if len(sources[0]) >= 2 and sources[0][-2] == "values":
        return field, "per game"
    raise ValueError("No semantic definition for source field")


def bind(
    payload: dict,
    *,
    sources: list[list],
    destination: list,
    metric: str,
    unit: str,
    identity: list,
    scope: list,
    spec: dict | None = None,
    context: Sequence[list] = (),
) -> None:
    """Register a relationship known at the renderer, before final enrichment."""
    for path in [*sources, identity, scope, *context]:
        if path[0] not in EVIDENCE_ROOTS:
            raise ValueError("Not public route evidence")
    metric, unit = descriptor(payload, sources, context)
    manifest = payload.setdefault("claim_bindings", {"version": VERSION, "claims": []})
    paths = [*sources, identity, scope, *context]
    roots = {p[0] for p in paths}
    hashes = manifest.setdefault("evidence_sha256", {})
    for root in roots:
        if root not in hashes:
            hashes[root] = digest(payload[root])
    manifest["claims"].append(
        {
            "sources": deepcopy(sources),
            "destination": list(destination),
            "metric": metric,
            "unit": unit,
            "operands": [
                dict(zip(("metric", "unit"), descriptor(payload, [path], context)))
                for path in sources
            ],
            "identity": {
                "path": identity,
                "value": deepcopy(resolve(payload, identity)),
            },
            "scope": {"path": scope, "value": deepcopy(resolve(payload, scope))},
            "context": [
                {"path": p, "sha256": digest(resolve(payload, p))} for p in context
            ],
            "evidence_sha256": {root: hashes[root] for root in roots},
            "transform": spec or {"op": "identity"},
        }
    )


def leaves(value: Any, path: list):
    if isinstance(value, dict):
        for key, item in value.items():
            yield from leaves(item, path + [key])
    elif isinstance(value, list):
        for i, item in enumerate(value):
            yield from leaves(item, path + [i])
    else:
        yield path, value


def validate(payload: dict) -> dict:
    """Validate declared mappings without calling any renderer or builder.

    A missing mapping is incomplete, a contradictory mapping is invalid. The
    gap scanner reports all visible scalar fields, including identity labels;
    it never infers evidence links from equal numbers or strings.
    """
    manifest = payload.get("claim_bindings", {})
    errors, covered = [], set()
    if isinstance(manifest, dict) and manifest and manifest.get("version") != VERSION:
        errors.append({"destination": [], "reason": "unsupported_version"})
    if not isinstance(manifest, dict) or not isinstance(
        manifest.get("claims", []), list
    ):
        return {
            "version": VERSION,
            "status": "invalid",
            "errors": [{"destination": [], "reason": "malformed_manifest"}],
            "unbound": [],
            "bound_destinations": 0,
        }
    if manifest and set(manifest) != {"version", "claims", "evidence_sha256"}:
        errors.append({"destination": [], "reason": "malformed_manifest_fields"})
    hashes = {}
    for claim in manifest.get("claims", []):
        if not isinstance(claim, dict):
            errors.append({"destination": [], "reason": "malformed_claim"})
            continue
        destination = claim.get("destination", [])
        try:
            required = {
                "sources",
                "destination",
                "metric",
                "unit",
                "identity",
                "scope",
                "context",
                "evidence_sha256",
                "transform",
                "operands",
            }
            if set(claim) != required:
                raise ValueError("Invalid claim fields")
            paths = [
                *claim["sources"],
                claim["identity"]["path"],
                claim["scope"]["path"],
                *[c["path"] for c in claim["context"]],
            ]
            if set(claim["evidence_sha256"]) != {p[0] for p in paths}:
                raise ValueError("Incomplete evidence digests")
            from app.agent.claim_rules import mapping, transformation

            if claim["transform"] != transformation(payload, destination):
                raise ValueError("Transform contradicts presentation contract")
            expected_mapping = mapping(payload, destination)
            declared_mapping = (
                claim["sources"],
                claim["identity"]["path"],
                claim["scope"]["path"],
                [c["path"] for c in claim["context"]],
            )
            if declared_mapping != expected_mapping:
                raise ValueError(
                    "Source path does not match presentation identity/metric/scope"
                )
            if descriptor(
                payload, claim["sources"], [c["path"] for c in claim["context"]]
            ) != (claim["metric"], claim["unit"]):
                raise ValueError("Metric/unit contradicts evidence")
            expected_operands = [
                dict(
                    zip(
                        ("metric", "unit"),
                        descriptor(
                            payload, [path], [c["path"] for c in claim["context"]]
                        ),
                    )
                )
                for path in claim["sources"]
            ]
            if claim["operands"] != expected_operands:
                raise ValueError("Operand metric/unit contradicts evidence")
            key = tuple(destination)
            if key in covered:
                raise ValueError("Duplicate destination")
            if (
                destination[0] not in VISIBLE_ROOTS
                and destination[0] != "semantic_evidence"
            ):
                raise ValueError("Not a presentation destination")
            for root, expected in claim["evidence_sha256"].items():
                if manifest.get("evidence_sha256", {}).get(root) != expected:
                    raise ValueError("Manifest evidence digest mismatch")
                if root not in EVIDENCE_ROOTS:
                    raise ValueError("Invalid evidence root")
                if root not in hashes:
                    hashes[root] = digest(payload[root])
                if hashes[root] != expected:
                    raise ValueError("Evidence digest changed")
            for item in claim["context"]:
                if digest(resolve(payload, item["path"])) != item["sha256"]:
                    raise ValueError("Context changed")
            for item in [claim["identity"], claim["scope"]]:
                if resolve(payload, item["path"]) != item["value"]:
                    raise ValueError("Identity/scope/context changed")
            if not claim["metric"] or not claim["unit"]:
                raise ValueError("Missing metric/unit")
            expected = transform(
                [resolve(payload, p) for p in claim["sources"]], claim["transform"]
            )
            actual = resolve(payload, destination)
            if actual != expected or type(actual) is not type(expected):
                raise ValueError("Presentation contradicts evidence")
            covered.add(key)
        except (
            ValueError,
            TypeError,
            KeyError,
            IndexError,
            OverflowError,
            AttributeError,
        ) as exc:
            errors.append({"destination": destination, "reason": str(exc)})
    from app.agent.claim_contexts import guards

    for path, expected_label in guards(payload).items():
        try:
            if resolve(payload, list(path)) != expected_label:
                raise ValueError("Presentation label contradicts renderer contract")
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            errors.append({"destination": list(path), "reason": str(exc)})
    unbound = [
        path
        for root in VISIBLE_ROOTS
        for path, value in leaves(payload.get(root), [root])
        if value not in (None, "") and tuple(path) not in covered
    ]
    # These UI renderers consume evidence directly. Until instrumented upstream,
    # their fields remain gaps, never self-authenticating claim destinations.
    evidence = payload.get("semantic_evidence") or {}
    for root in (
        "metrics",
        "comparison_sections",
        "insights",
        "game_insights",
        "summary_paragraphs",
    ):
        if root in evidence:
            unbound.extend(
                path
                for path, value in leaves(evidence[root], ["semantic_evidence", root])
                if value not in (None, "") and tuple(path) not in covered
            )
    return {
        "version": VERSION,
        "status": "invalid" if errors else "incomplete" if unbound else "complete",
        "bound_destinations": len(covered),
        "errors": errors,
        "unbound": unbound,
    }


def finalize(payload: dict) -> dict:
    result = validate(payload)
    if result["errors"]:
        from app.agent.execution import CURRENT
        from app.agent.payload import answer_payload

        policy = CURRENT.get()
        if policy is not None:
            policy.on_success.clear()
            policy.pending_clarifications.clear()
            if policy.trace is not None:
                policy.trace.outcome = "error"
                policy.trace.error_type = "claim_binding_mismatch"

        return answer_payload(
            "The evidence-to-answer checks failed. Please retry this request.",
            status="error",
            claim_validation=result,
            reason_code="claim_binding_mismatch",
        )
    payload["claim_validation"] = result
    return payload
