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
