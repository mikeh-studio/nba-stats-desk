from copy import deepcopy

import pytest
from app.agent.semantic_source import snapshot_digest
from scripts.promote_game_log_repair import (
    equal_rows_assertion,
    transaction_sql,
    validate_plan,
)


def plan():
    document = dict(
        status="validated",
        project="test-project",
        suffix="repair_012345abcdef",
        similarity_players=5,
        candidates=[],
    )
    for name in (
        "bronze.raw_game_logs",
        "bronze.raw_schedule",
        "silver.stg_game_logs_clean",
        "silver.int_player_game_enriched",
        "gold.fct_player_game_stats",
        "gold.fct_team_game_scores",
        "gold.dim_game",
        "gold.player_similarity_features",
        "gold.player_archetypes",
    ):
        layer, table = name.split(".")
        candidate = f"test-project.nba_{layer}_repair_012345abcdef.{table}"
        document["candidates"].append(
            dict(
                active=f"test-project.nba_{name}",
                candidate=candidate,
                backup=candidate + "_before",
                candidate_etag="version",
            )
        )
    document["sha256"] = snapshot_digest(document)
    return document


def test_immutable_plan_and_all_required_dependencies():
    document = plan()
    validate_plan(document, document["sha256"])
    modified = deepcopy(document)
    modified["candidates"].pop()
    with pytest.raises(ValueError, match="unchanged"):
        validate_plan(modified, document["sha256"])
    modified["sha256"] = snapshot_digest(modified)
    with pytest.raises(ValueError, match="Required"):
        validate_plan(modified, modified["sha256"])


def test_target_escape_rejected_even_with_new_checksum():
    document = plan()
    document["candidates"][0]["candidate"] = (
        "test-project.nba_gold.fct_player_game_stats"
    )
    document["sha256"] = snapshot_digest(document)
    with pytest.raises(ValueError, match="escaped"):
        validate_plan(document, document["sha256"])


def test_transaction_checks_multiset_baselines_before_any_mutation():
    pair = dict(
        active="test-project.test.active",
        candidate="test-project.test.candidate",
        backup="test-project.test.backup",
        baseline_columns=["id", "value"],
        candidate_columns=["id", "value"],
    )
    sql = transaction_sql([pair])
    assert sql.startswith("BEGIN TRANSACTION;") and sql.endswith("COMMIT TRANSACTION;")
    assert sql.index("ASSERT") < sql.index("DELETE") < sql.index("INSERT")
    assert "COUNT(*) AS copies" in sql  # duplicates cannot disappear in set equality
    assert sql.count("EXCEPT DISTINCT") == 2
    with pytest.raises(ValueError):
        equal_rows_assertion(pair["active"], pair["backup"], ["value` FROM secrets"])
