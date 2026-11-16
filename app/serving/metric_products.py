"""Closed, additive metric-product pins for an explicitly composed core reader.

A digest identifies reviewed semantics and evidence; it does not grant authority.
The existing publication/source gates remain responsible for every emission.
"""

from __future__ import annotations

import copy
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.agent import semantics
from app.serving.publication_contract import digest

_SUPPORTED_CORE_POLICY = {
    "version": "metric-semantics/1",
    "product": "core_box_score",
    "metric": "fg_pct",
    "aggregation": "ratio",
    "numerator": "SUM(fgm)",
    "denominator": "SUM(fga)",
    "missing": "exclude_incomplete_appearance_disclose_counts",
    "zero_denominator": "unavailable",
    "unit": "ratio",
    "display_scale": 100,
    "display_decimal_places": 1,
}
CORE_SEMANTICS = copy.deepcopy(_SUPPORTED_CORE_POLICY)
RATIO_ALGORITHM_VERSION = "governed-ratio-policy/1"
_SUPPORTED_FG_DEFINITION = {
    "numerator": "fgm",
    "denominator": "fga",
    "unit": "ratio",
    "components": ["fga", "fgm"],
    "aggregations": ["ratio"],
}


def _fg_definition():
    metric = semantics.load_contract().metric("fg_pct")
    return {
        "numerator": metric.numerator,
        "denominator": metric.denominator,
        "unit": metric.unit,
        "components": sorted(metric.components),
        "aggregations": list(metric.aggregations),
    }


def validate_core_semantics():
    """Reject unsupported definitions instead of assigning them an honest-looking pin."""
    if (
        CORE_SEMANTICS != _SUPPORTED_CORE_POLICY
        or _fg_definition() != _SUPPORTED_FG_DEFINITION
    ):
        raise ValueError("unsupported core metric definition or aggregation policy")


def core_semantic_revision():
    """Cover the executable definition and arithmetic, including null policy."""
    return digest(
        {
            "policy": CORE_SEMANTICS,
            "algorithm": RATIO_ALGORITHM_VERSION,
            "metric": _fg_definition(),
        }
    )


CORE_SEMANTIC_REVISION = core_semantic_revision()


def make_core_product(manifest, binding):
    """Describe an envelope; callers independently establish its authority."""
    validate_core_semantics()
    return {
        "version": "metric-product/1",
        "product": "core_box_score",
        "publication_id": manifest["publication_id"],
        "manifest_digest": digest(manifest),
        "binding_digest": digest(binding),
        "identity_revision": manifest["versions"]["identity"],
        "identity_digest": next(
            i["digest"] for i in manifest["inputs"] if i["role"] == "identity"
        ),
        "semantic_revision": core_semantic_revision(),
        "scope": copy.deepcopy(manifest["scope"]),
        "expires_at": manifest["freshness"]["expires_at"],
    }


def validate_core_product(product, manifest, binding):
    """Bind the exact closed sidecar to the actual reader envelope and code."""
    if not isinstance(product, dict) or product != make_core_product(manifest, binding):
        raise ValueError("metric product differs from publication or semantics")
    return copy.deepcopy(product)


class ResolvedMetricQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    player_id: int = Field(gt=0, strict=True)
    first: date
    last: date
    question_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class MetricSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal["metric-selection/1"]
    product: dict
    query: ResolvedMetricQuery


def read_core_product(document, manifest, binding):
    """Read a configured single sidecar or bounded retained-product registry."""
    if isinstance(document, dict) and document.get("version") == "metric-product/1":
        candidates = [document]
    elif (
        isinstance(document, dict)
        and set(document) == {"version", "products"}
        and document["version"] == "metric-product-registry/1"
        and isinstance(document["products"], list)
        and len(document["products"]) <= 100
    ):
        candidates = [
            p
            for p in document["products"]
            if isinstance(p, dict)
            and p.get("publication_id") == manifest["publication_id"]
        ]
    else:
        raise ValueError("invalid metric product registry")
    if len(candidates) != 1:
        raise ValueError("metric product unavailable or ambiguous")
    return validate_core_product(candidates[0], manifest, binding)
