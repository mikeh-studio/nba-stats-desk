"""Synthetic reader/selection fixtures; no source or production certification."""

import ast
import inspect
from dataclasses import replace

import pytest
from app.agent import formulas, semantics
from app.serving.metric_products import (
    CORE_SEMANTIC_REVISION,
    CORE_SEMANTICS,
    core_semantic_revision,
    make_core_product,
    validate_core_product,
)
from app.serving.publication_contract import digest
from scripts.build_synthetic_core_replay import synthetic_bundle


def test_revision_binds_definition_but_ignores_unrelated_metrics(monkeypatch):
    bundle = synthetic_bundle()
    product = make_core_product(bundle["manifest"], bundle["binding"])
    original = semantics.load_contract()
    metrics = dict(original.metrics)
    metrics["pts"] = replace(metrics["pts"], numerator="reb")
    metrics["fg_pct"] = replace(metrics["fg_pct"], label="Different display label")
    monkeypatch.setattr(
        semantics, "load_contract", lambda: replace(original, metrics=metrics)
    )
    assert core_semantic_revision() == CORE_SEMANTIC_REVISION
    metrics["fg_pct"] = replace(metrics["fg_pct"], denominator="fta")
    assert core_semantic_revision() != CORE_SEMANTIC_REVISION
    with pytest.raises(ValueError, match="unsupported core metric"):
        make_core_product(bundle["manifest"], bundle["binding"])
    with pytest.raises(ValueError):
        validate_core_product(product, bundle["manifest"], bundle["binding"])
    monkeypatch.undo()
    assert (
        validate_core_product(product, bundle["manifest"], bundle["binding"]) == product
    )


def test_ratio_algorithm_change_requires_reviewed_policy_revision():
    # Update this expectation only after reviewing arithmetic/null semantics and
    # deciding whether governed-ratio-policy/1 needs a version bump.
    functions = (
        semantics.aggregate,
        semantics._number,
        formulas.evaluate_formula,
        formulas._eval_node,
        formulas._coerce_float,
    )
    actual = digest(
        [
            ast.dump(ast.parse(inspect.getsource(fn)), include_attributes=False)
            for fn in functions
        ]
    )
    assert actual == "000d38046117134cd2535a4d7aa977a97120d8704caf5d5a343c5df8224c4540"


def test_independent_ratio_of_sums_missing_and_zero():
    metric = semantics.load_contract().metric("fg_pct")
    rows = [
        dict(game_id="a", fgm=1, fga=1),
        dict(game_id="b", fgm=0, fga=9),
        dict(game_id="c", fgm=None, fga=8),
    ]
    result = semantics.aggregate(rows, metric, "ratio")
    assert result["value"] == 0.1 and result["display_value"] == 10
    assert (result["numerator"], result["denominator"]) == (1, 10)
    assert (
        result["observed_games"],
        result["valid_games"],
        result["missing_component_games"],
    ) == (3, 2, 1)
    assert (
        semantics.aggregate([dict(game_id="z", fgm=0, fga=0)], metric, "ratio")["value"]
        is None
    )
    empty = semantics.aggregate([dict(game_id="m", fgm=None, fga=1)], metric, "ratio")
    assert empty["value"] is empty["numerator"] is empty["denominator"] is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("aggregation", "mean_of_ratios"),
        ("zero_denominator", "zero"),
        ("display_decimal_places", 2),
        ("missing", "fill_zero"),
    ],
)
def test_incompatible_policy_changes_digest_and_fails_admission(
    monkeypatch, field, value
):
    bundle = synthetic_bundle()
    product = make_core_product(bundle["manifest"], bundle["binding"])
    monkeypatch.setitem(CORE_SEMANTICS, field, value)
    assert core_semantic_revision() != CORE_SEMANTIC_REVISION
    with pytest.raises(ValueError, match="unsupported core metric"):
        make_core_product(bundle["manifest"], bundle["binding"])
    with pytest.raises(ValueError):
        validate_core_product(product, bundle["manifest"], bundle["binding"])


def test_roster_revision_is_separate_but_changes_descriptor():
    import copy

    bundle = synthetic_bundle()
    original = make_core_product(bundle["manifest"], bundle["binding"])
    manifest = copy.deepcopy(bundle["manifest"])
    manifest["versions"]["identity"] = "synthetic-roster/2"
    revised = make_core_product(manifest, bundle["binding"])
    assert digest(original) != digest(revised)
    assert revised["semantic_revision"] == original["semantic_revision"]
