"""Independent synthetic contract oracle; no endpoint or authority claims."""

import copy

import pytest
from app.serving.metric_products import make_core_product
from app.serving.publication_contract import digest
from app.serving.shot_products import (
    make_shot_product,
    query_shots,
    validate_shot_product,
)
from scripts.build_synthetic_core_replay import synthetic_bundle


def test_combined_five_game_oracle_and_covered_empty():
    facts = [
        {"game_id": f"g{i}", "game_date": f"2026-10-{20 + i:02}", "player_id": 101}
        for i in range(5)
    ]
    artifact = {
        "rows": [
            {
                "game_id": "g0",
                "player_id": 101,
                "contested_attempts": 3,
                "observed_attempts": 5,
            },
            {
                "game_id": "g1",
                "player_id": 101,
                "contested_attempts": 1,
                "observed_attempts": 2,
            },
            {
                "game_id": "g2",
                "player_id": 101,
                "contested_attempts": 0,
                "observed_attempts": 0,
            },
        ],
        "product": {"product_id": "synthetic"},
    }
    result = query_shots(
        artifact, facts, player_id=101, first="2026-10-20", last="2026-10-24"
    )
    row = result["rows"][0]
    assert row["value"] == pytest.approx(4 / 7)
    assert (row["numerator"], row["denominator"]) == (4, 7)
    assert result["coverage"] == {
        "eligible_games": 5,
        "covered_games": 3,
        "unknown_games": 2,
        "unknown_game_ids": ["g3", "g4"],
    }
    assert row["status"] == "partial"
    zero = query_shots(
        artifact, facts, player_id=101, first="2026-10-22", last="2026-10-22"
    )
    assert zero["rows"][0]["value"] is None and zero["rows"][0]["denominator"] == 0
    assert zero["coverage"]["covered_games"] == 1
    unknown = query_shots(
        artifact, facts, player_id=101, first="2026-10-23", last="2026-10-23"
    )
    assert (
        unknown["rows"][0]["value"] is None
        and unknown["rows"][0]["denominator"] is None
    )
    assert unknown["coverage"]["covered_games"] == 0


def test_distinct_pinned_source_inventory_and_strict_coverage():
    bundle = synthetic_bundle()
    core = make_core_product(bundle["manifest"], bundle["binding"])
    games = bundle["models"]["accepted_game_results"]
    coverage = [
        {
            "game_id": g["game_id"],
            "game_date": g["game_date"],
            "status": "complete",
            "expected_events": 0,
        }
        for g in games
    ]
    rows = [
        {
            "game_id": r["game_id"],
            "player_id": r["player_id"],
            "contested_attempts": 0,
            "observed_attempts": 0,
        }
        for r in bundle["models"]["fct_player_game_stats"]
    ]
    kwargs = dict(
        coverage=coverage,
        rows=rows,
        expires_at=core["expires_at"],
        source_revision=digest("synthetic source A"),
        inventory_revision=digest("synthetic inventory"),
        source_through=max(g["game_date"] for g in games),
    )
    one = make_shot_product(core, **kwargs)
    assert (
        validate_shot_product(
            one, bundle["manifest"], bundle["binding"], bundle["models"]
        )
        == one
    )
    kwargs["source_revision"] = digest("synthetic source B")
    two = make_shot_product(core, **kwargs)
    assert (
        one["rows"] == two["rows"]
        and one["product"]["product_id"] != two["product"]["product_id"]
    )
    bad = copy.deepcopy(one)
    bad["rows"].pop()
    with pytest.raises(ValueError):
        validate_shot_product(
            bad, bundle["manifest"], bundle["binding"], bundle["models"]
        )


def _zero_product_inputs():
    bundle = synthetic_bundle()
    core = make_core_product(bundle["manifest"], bundle["binding"])
    games = bundle["models"]["accepted_game_results"]
    kwargs = dict(
        coverage=[
            {
                "game_id": g["game_id"],
                "game_date": g["game_date"],
                "status": "complete",
                "expected_events": 0,
            }
            for g in games
        ],
        rows=[
            {
                "game_id": r["game_id"],
                "player_id": r["player_id"],
                "contested_attempts": 0,
                "observed_attempts": 0,
            }
            for r in bundle["models"]["fct_player_game_stats"]
        ],
        expires_at=core["expires_at"],
        source_revision=digest("synthetic source"),
        inventory_revision=digest("synthetic inventory"),
        source_through=max(g["game_date"] for g in games),
    )
    return bundle, core, kwargs


@pytest.mark.parametrize("count", [True, 3.0, "3", 1e0, -0.0, -1, 2**53])
@pytest.mark.parametrize(
    "field", ["observed_attempts", "contested_attempts", "expected_events"]
)
def test_noncanonical_or_oversized_counts_rejected_before_hash(
    monkeypatch, count, field
):
    from app.serving import shot_products

    bundle, core, kwargs = _zero_product_inputs()
    valid = make_shot_product(core, **kwargs)
    target = kwargs["coverage"][0] if field == "expected_events" else kwargs["rows"][0]
    target[field] = count
    bad = copy.deepcopy(valid)
    (bad["coverage"][0] if field == "expected_events" else bad["rows"][0])[field] = (
        count
    )

    def forbidden_hash(_):
        raise AssertionError("invalid count reached hashing")

    monkeypatch.setattr(shot_products, "digest", forbidden_hash)
    with pytest.raises(ValueError, match="integer range"):
        make_shot_product(core, **kwargs)
    with pytest.raises(ValueError, match="integer range"):
        validate_shot_product(
            bad, bundle["manifest"], bundle["binding"], bundle["models"]
        )


def test_pooled_counts_are_bounded_even_when_each_game_is_exact():
    from app.serving.shot_products import MAX_EXACT_SHOT_COUNT

    bundle, core, kwargs = _zero_product_inputs()
    valid = make_shot_product(core, **kwargs)
    for partition in kwargs["coverage"]:
        partition["expected_events"] = (MAX_EXACT_SHOT_COUNT // 2) + 1
    for row in kwargs["rows"]:
        if row["player_id"] == 101:
            row["observed_attempts"] = (MAX_EXACT_SHOT_COUNT // 2) + 1
    with pytest.raises(ValueError, match="pooled shot sample"):
        make_shot_product(core, **kwargs)
    valid["rows"], valid["coverage"] = kwargs["rows"], kwargs["coverage"]
    with pytest.raises(ValueError, match="pooled shot sample"):
        validate_shot_product(
            valid, bundle["manifest"], bundle["binding"], bundle["models"]
        )


def test_exact_upper_boundary_remains_finite_and_core_roster_drift_rejects():
    from app.serving.shot_products import MAX_EXACT_SHOT_COUNT

    bundle, core, kwargs = _zero_product_inputs()
    row = kwargs["rows"][0]
    row["observed_attempts"] = row["contested_attempts"] = MAX_EXACT_SHOT_COUNT
    next(c for c in kwargs["coverage"] if c["game_id"] == row["game_id"])[
        "expected_events"
    ] = MAX_EXACT_SHOT_COUNT
    artifact = make_shot_product(core, **kwargs)
    validate_shot_product(
        artifact, bundle["manifest"], bundle["binding"], bundle["models"]
    )
    result = query_shots(
        artifact,
        bundle["models"]["fct_player_game_stats"],
        player_id=row["player_id"],
        first=core["scope"]["first"],
        last=core["scope"]["last"],
    )
    assert result["rows"][0]["value"] == 1
    assert result["rows"][0]["denominator"] == MAX_EXACT_SHOT_COUNT
    changed = copy.deepcopy(bundle["models"])
    extra = copy.deepcopy(changed["fct_player_game_stats"][0])
    extra["player_id"] = 999
    changed["fct_player_game_stats"].append(extra)
    with pytest.raises(ValueError, match="eligible components"):
        validate_shot_product(artifact, bundle["manifest"], bundle["binding"], changed)
