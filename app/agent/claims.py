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

VERSION = "ask_claim_bindings/2"
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


class _Manifest(dict):
    """Construction-only indexes are attributes, never serialized proof fields."""

    def __init__(self):
        super().__init__(
            version=VERSION,
            claims=[],
            paths=[],
            descriptors=[],
            transforms=[],
            evidence_sha256={},
        )
        self.indexes: dict[str, dict[str, int]] = {}


def intern(manifest, registry, value):
    entries = manifest[registry]
    if isinstance(manifest, _Manifest):
        index = manifest.indexes.setdefault(registry, {})
        key = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if key in index:
            return index[key]
        index[key] = len(entries)
    else:
        # Rehydrated manifests are ordinary JSON dictionaries.
        try:
            return entries.index(value)
        except ValueError:
            pass
    entries.append(deepcopy(value))
    return len(entries) - 1


def path_ref(manifest, path):
    parent = -1
    for key in path:
        parent = intern(manifest, "paths", [parent, key])
    return parent


def dereference(entries, reference):
    if type(reference) is not int or not 0 <= reference < len(entries):
        raise ValueError("Invalid registry reference")
    return entries[reference]


def expand_path(manifest, reference):
    result = []
    while True:
        parent, key = dereference(manifest["paths"], reference)
        result.append(key)
        if len(result) > 32:
            raise ValueError("Path too deep")
        if type(parent) is not int or parent < -1 or parent >= reference:
            raise ValueError("Invalid path parent")
        if parent == -1:
            return list(reversed(result))
        reference = parent


def expand_claim(manifest, claim):
    """Inspectable expanded declaration; no renderer or expected value is called."""
    if not isinstance(claim, dict) or set(claim) != {
        "d",
        "s",
        "i",
        "q",
        "c",
        "t",
        "m",
        "o",
    }:
        raise ValueError("Invalid claim fields")
    for field in ("s", "c", "o"):
        if not isinstance(claim[field], list) or len(claim[field]) > 64:
            raise ValueError("Invalid reference list")
    metric, unit = dereference(manifest["descriptors"], claim["m"])
    return {
        "destination": expand_path(manifest, claim["d"]),
        "sources": [expand_path(manifest, ref) for ref in claim["s"]],
        "identity": expand_path(manifest, claim["i"]),
        "scope": expand_path(manifest, claim["q"]),
        "context": [expand_path(manifest, ref) for ref in claim["c"]],
        "transform": dereference(manifest["transforms"], claim["t"]),
        "metric": metric,
        "unit": unit,
        "operands": [dereference(manifest["descriptors"], ref) for ref in claim["o"]],
    }


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
    """Intern exact source coordinates and canonical anchors once per payload."""
    paths = [*sources, identity, scope, *context]
    for path in paths:
        if path[0] not in EVIDENCE_ROOTS:
            raise ValueError("Not public route evidence")
    manifest = payload.setdefault("claim_bindings", _Manifest())
    for root in {path[0] for path in paths}:
        if root not in manifest["evidence_sha256"]:
            manifest["evidence_sha256"][root] = digest(payload[root])
    manifest["claims"].append(
        {
            "d": path_ref(manifest, destination),
            "s": [path_ref(manifest, path) for path in sources],
            "i": path_ref(manifest, identity),
            "q": path_ref(manifest, scope),
            "c": [path_ref(manifest, path) for path in context],
            "t": intern(manifest, "transforms", spec or {"op": "identity"}),
            "m": intern(
                manifest, "descriptors", list(descriptor(payload, sources, context))
            ),
            "o": [
                intern(
                    manifest, "descriptors", list(descriptor(payload, [path], context))
                )
                for path in sources
            ],
        }
    )


def validate_registries(manifest):
    if not isinstance(manifest, dict) or set(manifest) != {
        "version",
        "claims",
        "paths",
        "descriptors",
        "transforms",
        "evidence_sha256",
    }:
        raise ValueError("Malformed manifest")
    if manifest["version"] != VERSION:
        raise ValueError("Unsupported version")
    if not isinstance(manifest["evidence_sha256"], dict):
        raise ValueError("Invalid evidence digest registry")
    for registry in ("paths", "descriptors", "transforms", "claims"):
        entries = manifest[registry]
        if not isinstance(entries, list) or len(entries) > 100_000:
            raise ValueError("Invalid registry")
        if registry != "claims" and len(
            {json.dumps(item, sort_keys=True, allow_nan=False) for item in entries}
        ) != len(entries):
            raise ValueError("Duplicate registry entries")
    for i, node in enumerate(manifest["paths"]):
        if not isinstance(node, list) or len(node) != 2:
            raise ValueError("Invalid path node")
        parent, key = node
        if type(parent) is not int or not -1 <= parent < i:
            raise ValueError("Invalid path parent")
        if not (
            (type(key) is int and key >= 0)
            or (
                type(key) is str and key and not key.startswith("_") and len(key) <= 256
            )
        ):
            raise ValueError("Invalid path component")
        expand_path(manifest, i)
    for item in manifest["descriptors"]:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or not all(type(v) is str and 0 < len(v) <= 256 for v in item)
        ):
            raise ValueError("Invalid descriptor")
    for item in manifest["transforms"]:
        if not isinstance(item, dict) or not set(item) <= {
            "op",
            "template",
            "formats",
            "null",
            "scale",
            "digits",
            "round",
            "signed",
            "normalize_zero",
            "unsigned_zero",
            "string",
            "suffix",
        }:
            raise ValueError("Invalid transform registry")


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
    manifest: Any = payload.get("claim_bindings")
    errors, covered = [], set()
    roots, claims = set(), []
    if manifest is not None:
        try:
            validate_registries(manifest)
            claims = manifest["claims"]
            for root, expected in manifest["evidence_sha256"].items():
                if (
                    root not in EVIDENCE_ROOTS
                    or type(expected) is not str
                    or not re.fullmatch("[0-9a-f]{64}", expected)
                ):
                    raise ValueError("Invalid evidence digest")
                if digest(payload[root]) != expected:
                    raise ValueError("Evidence digest changed")
        except (
            ValueError,
            TypeError,
            KeyError,
            IndexError,
            OverflowError,
            AttributeError,
        ) as exc:
            errors.append({"destination": [], "reason": str(exc)})
            claims = []
    for record in claims:
        destination = []
        try:
            claim = expand_claim(manifest, record)
            destination = claim["destination"]
            paths = [
                *claim["sources"],
                claim["identity"],
                claim["scope"],
                *claim["context"],
            ]
            claim_roots = {path[0] for path in paths}
            roots.update(claim_roots)
            if not claim_roots <= set(manifest["evidence_sha256"]):
                raise ValueError("Incomplete evidence digests")
            from app.agent.claim_rules import mapping, transformation

            if json.dumps(claim["transform"], sort_keys=True) != json.dumps(
                transformation(payload, destination), sort_keys=True
            ):
                raise ValueError("Transform contradicts presentation contract")
            if (
                claim["sources"],
                claim["identity"],
                claim["scope"],
                claim["context"],
            ) != mapping(payload, destination):
                raise ValueError(
                    "Source path does not match presentation identity/metric/scope"
                )
            if descriptor(payload, claim["sources"], claim["context"]) != (
                claim["metric"],
                claim["unit"],
            ):
                raise ValueError("Metric/unit contradicts evidence")
            if claim["operands"] != [
                list(descriptor(payload, [path], claim["context"]))
                for path in claim["sources"]
            ]:
                raise ValueError("Operand metric/unit contradicts evidence")
            key = tuple(destination)
            if key in covered:
                raise ValueError("Duplicate destination")
            if (
                destination[0] not in VISIBLE_ROOTS
                and destination[0] != "semantic_evidence"
            ):
                raise ValueError("Not a presentation destination")
            # Anchors are exact coordinates into the hashed canonical root; no
            # duplicate identity/scope snapshot or per-claim digest is necessary.
            for path in [claim["identity"], claim["scope"], *claim["context"]]:
                resolve(payload, path)
            expected = transform(
                [resolve(payload, path) for path in claim["sources"]],
                claim["transform"],
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
    if claims and roots != set(manifest["evidence_sha256"]):
        errors.append(
            {"destination": [], "reason": "Surplus or missing evidence digests"}
        )
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
        "status": "invalid"
        if errors
        else "incomplete"
        if unbound or not claims
        else "complete",
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
