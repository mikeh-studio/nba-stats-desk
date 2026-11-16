"""Synthetic-capable optional shot-event aggregate contract; no acquisition.

Only additive integer components cross this interface. Authority is supplied by
trusted composition, never inferred from an artifact's claims of completeness.
"""

import copy
import re

from app.agent.semantics import Metric, aggregate
from app.serving.metric_products import make_core_product, validate_core_product
from app.serving.publication_contract import digest, timestamp

# Components enter a float-based governed engine and browser JSON consumers.
# Nonnegative counts keep every bounded sample exactly representable.
MAX_EXACT_SHOT_COUNT = 2**53 - 1


def _bounded_integer(value):
    return type(value) is int and 0 <= value <= MAX_EXACT_SHOT_COUNT


def _validate_count_wire(coverage, rows):
    expected_total = 0
    observed_total = 0
    for partition in coverage:
        if not isinstance(partition, dict):
            raise ValueError("invalid shot coverage shape")
        count = partition.get("expected_events")
        if count is not None:
            if not _bounded_integer(count):
                raise ValueError("shot inventory count outside exact integer range")
            expected_total += count
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("invalid shot component shape")
        observed, contested = (
            row.get("observed_attempts"),
            row.get("contested_attempts"),
        )
        if (
            not _bounded_integer(observed)
            or not _bounded_integer(contested)
            or contested > observed
        ):
            raise ValueError("shot components outside exact integer range")
        observed_total += observed
    if expected_total > MAX_EXACT_SHOT_COUNT or observed_total > MAX_EXACT_SHOT_COUNT:
        raise ValueError("pooled shot sample outside exact integer range")


SHOT_METRIC = Metric(
    key="contested_attempt_share",
    label="Contested attempt share",
    numerator="contested_attempts",
    denominator="observed_attempts",
    attempt_field="observed_attempts",
    unit="ratio",
    direction="higher",
)
SHOT_CATALOG_ENTRY = {
    "product": "shot_events",
    "metric": "contested_attempt_share",
    "label": "Observed contested attempt share",
    "aggregation": "ratio",
    "unit": "ratio",
    "display_unit": "percent",
    "operation": "summary",
    "coverage_policy": "complete eligible player-games; unknown games excluded and disclosed",
}
SHOT_SEMANTIC_REVISION = digest(
    {
        "version": "shot-semantics/1",
        "metric": "contested_attempt_share",
        "numerator": SHOT_METRIC.numerator,
        "denominator": SHOT_METRIC.denominator,
        "unit": SHOT_METRIC.unit,
        "aggregation": "governed-ratio-policy/1",
        "coverage": "complete_eligible_player_games_only_disclose_unknown",
        "zero_denominator": "unavailable",
        "display_scale": 100,
    }
)


def read_shot_product(document, envelope, product_id=None):
    """Resolve one retained shot product without retrying an unavailable pin."""
    if (
        not isinstance(document, dict)
        or set(document) != {"version", "active_product_id", "products"}
        or document["version"] != "shot-product-registry/1"
        or not isinstance(document["products"], list)
        or len(document["products"]) > 100
    ):
        raise ValueError("invalid shot product registry")
    wanted = document["active_product_id"] if product_id is None else product_id
    candidates = [
        a
        for a in document["products"]
        if isinstance(a, dict) and a.get("product", {}).get("product_id") == wanted
    ]
    if (
        len(candidates) != 1
        or candidates[0]["product"].get("publication_id")
        != envelope["manifest"]["publication_id"]
    ):
        raise ValueError("shot product pin unavailable")
    return copy.deepcopy(candidates[0])


def make_shot_product(
    core_product,
    *,
    coverage,
    rows,
    expires_at,
    source_revision,
    inventory_revision,
    source_through,
    source_version="shot-events/1",
    inventory_version="shot-event-inventory/1",
):
    _validate_count_wire(coverage, rows)
    coverage = sorted(copy.deepcopy(coverage), key=lambda r: r["game_id"])
    rows = sorted(copy.deepcopy(rows), key=lambda r: (r["game_id"], r["player_id"]))
    product = {
        "version": "metric-product/1",
        "product": "shot_events",
        "publication_id": core_product["publication_id"],
        "core_reference": copy.deepcopy(core_product),
        "identity_revision": core_product["identity_revision"],
        "identity_digest": core_product["identity_digest"],
        "semantic_revision": SHOT_SEMANTIC_REVISION,
        "scope": copy.deepcopy(core_product["scope"]),
        "expires_at": expires_at,
        "coverage_digest": digest(coverage),
        "rows_digest": digest(rows),
        "source_revision": source_revision,
        "inventory_revision": inventory_revision,
        "source_version": source_version,
        "inventory_version": inventory_version,
        "source_through": source_through,
    }
    product["product_id"] = "shot_" + digest(product)[:16]
    return {"product": product, "coverage": coverage, "rows": rows}


def validate_shot_product(artifact, manifest, binding, models):
    if not isinstance(artifact, dict) or set(artifact) != {
        "product",
        "coverage",
        "rows",
    }:
        raise ValueError("invalid shot artifact")
    product, coverage, rows = (
        artifact["product"],
        artifact["coverage"],
        artifact["rows"],
    )
    if (
        not isinstance(product, dict)
        or not isinstance(coverage, list)
        or not isinstance(rows, list)
    ):
        raise ValueError("invalid shot shape")
    _validate_count_wire(coverage, rows)
    core = make_core_product(manifest, binding)
    validate_core_product(product.get("core_reference"), manifest, binding)
    if (
        not timestamp(manifest["materialized_at"])
        < timestamp(product["expires_at"])
        <= timestamp(core["expires_at"])
    ):
        raise ValueError("shot expiry differs from core authority")
    games = {g["game_id"]: g["game_date"] for g in models["accepted_game_results"]}
    covered = {}
    for row in coverage:
        if not isinstance(row, dict) or set(row) != {
            "game_id",
            "game_date",
            "status",
            "expected_events",
        }:
            raise ValueError("invalid shot coverage fields")
        gid, count = row["game_id"], row["expected_events"]
        if gid in covered or gid not in games or row["game_date"] != games[gid]:
            raise ValueError("shot game identity/date differs")
        if row["status"] == "complete":
            if type(count) is not int or count < 0:
                raise ValueError("invalid complete shot inventory")
        elif row["status"] != "unknown" or count is not None:
            raise ValueError("unknown coverage cannot claim inventory")
        covered[gid] = row
    if set(covered) != set(games):
        raise ValueError("shot coverage must describe every core game")
    if (
        any(
            not isinstance(product.get(k), str)
            or not re.fullmatch(r"[0-9a-f]{64}", product[k])
            for k in ("source_revision", "inventory_revision")
        )
        or any(
            not isinstance(product.get(k), str) or not 1 <= len(product[k]) <= 100
            for k in ("source_version", "inventory_version")
        )
        or product.get("source_through")
        != max(
            (c["game_date"] for c in coverage if c["status"] == "complete"),
            default=None,
        )
    ):
        raise ValueError("shot source inventory/watermark differs")
    eligible = {
        (r["game_id"], r["player_id"])
        for r in models["fct_player_game_stats"]
        if covered[r["game_id"]]["status"] == "complete"
    }
    actual = set()
    totals = {gid: 0 for gid in games}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {
            "game_id",
            "player_id",
            "observed_attempts",
            "contested_attempts",
        }:
            raise ValueError("invalid shot component fields")
        key = row["game_id"], row["player_id"]
        observed, contested = row["observed_attempts"], row["contested_attempts"]
        if type(row["player_id"]) is not int or key not in eligible or key in actual:
            raise ValueError("shot appearance is not uniquely eligible")
        if (
            type(observed) is not int
            or type(contested) is not int
            or not 0 <= contested <= observed
        ):
            raise ValueError("invalid shot integer components")
        actual.add(key)
        totals[row["game_id"]] += observed
    if actual != eligible or any(
        totals[gid] != c["expected_events"]
        for gid, c in covered.items()
        if c["status"] == "complete"
    ):
        raise ValueError("shot inventory/eligible components differ")
    expected = make_shot_product(
        core,
        coverage=coverage,
        rows=rows,
        expires_at=product["expires_at"],
        **{
            k: product[k]
            for k in (
                "source_revision",
                "inventory_revision",
                "source_through",
                "source_version",
                "inventory_version",
            )
        },
    )
    if artifact != expected or not re.fullmatch(
        r"shot_[0-9a-f]{16}", product.get("product_id", "")
    ):
        raise ValueError("shot descriptor/content/semantics differ")
    return copy.deepcopy(artifact)


def query_shots(artifact, facts, *, player_id, first, last):
    selected = [
        r
        for r in facts
        if r["player_id"] == player_id and first <= r["game_date"] <= last
    ]
    components = {(r["game_id"], r["player_id"]): r for r in artifact["rows"]}
    rows = []
    unknown = []
    for fact in selected:
        row = components.get((fact["game_id"], player_id))
        if row is None:
            unknown.append(fact["game_id"])
            row = {
                "game_id": fact["game_id"],
                "observed_attempts": None,
                "contested_attempts": None,
            }
        rows.append(row)
    result = aggregate(rows, SHOT_METRIC, "ratio")
    return {
        "metric": SHOT_METRIC.key,
        "aggregation": "ratio",
        "unit": "ratio",
        "rows": [
            dict(
                result,
                player_id=player_id,
                status="unavailable"
                if result["value"] is None
                else "partial"
                if unknown
                else "ok",
            )
        ],
        "coverage": {
            "eligible_games": len(selected),
            "covered_games": len(selected) - len(unknown),
            "unknown_games": len(unknown),
            "unknown_game_ids": sorted(unknown),
        },
        "scope": {"first": first, "last": last},
        "provenance": {
            "product_id": artifact["product"]["product_id"],
            "semantic_revision": SHOT_SEMANTIC_REVISION,
        },
    }
