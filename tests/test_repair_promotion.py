from copy import deepcopy

import pytest
from app.agent.semantic_source import snapshot_digest
from scripts.promote_game_log_repair import (
    content_digest_sql,
    equal_rows_assertion,
    promote,
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
                candidate_digest="a" * 64,
                candidate_columns=["id", "value"],
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
        candidate_digest="a" * 64,
    )
    sql = transaction_sql([pair])
    assert sql.startswith("BEGIN TRANSACTION;") and sql.endswith("COMMIT TRANSACTION;")
    assert sql.index("ASSERT") < sql.index("DELETE") < sql.index("INSERT")
    assert "COUNT(*) AS copies" in sql  # duplicates cannot disappear in set equality
    assert sql.count("EXCEPT DISTINCT") == 2
    with pytest.raises(ValueError):
        equal_rows_assertion(pair["active"], pair["backup"], ["value` FROM secrets"])


def test_candidate_content_guard_precedes_all_live_mutations():
    pair = dict(
        active="test-project.test.active",
        candidate="test-project.test.candidate",
        backup="test-project.test.backup",
        baseline_columns=["id"],
        candidate_columns=["id"],
        candidate_digest="a" * 64,
    )
    sql = transaction_sql([pair])
    assert sql.index("Validated candidate changed") < sql.index("DELETE")
    digest = content_digest_sql(pair["candidate"], ["id"])
    assert digest in sql
    assert "ORDER BY row_hash" in digest
    assert "DISTINCT" not in digest  # Duplicate rows must change the digest.
    assert "COALESCE(STRING_AGG" in digest  # Empty tables have a stable digest.


def test_old_manifests_without_content_evidence_fail_closed():
    document = plan()
    del document["candidates"][0]["candidate_digest"]
    document["sha256"] = snapshot_digest(document)
    with pytest.raises(ValueError, match="restage"):
        validate_plan(document, document["sha256"])


def test_second_candidate_version_change_aborts_before_publication():
    from types import SimpleNamespace

    from google.cloud.bigquery import SchemaField

    document = plan()
    document["sources"] = []
    for pair in document["candidates"]:
        pair["etag"] = "live"
    document["sha256"] = snapshot_digest(document)

    class Client:
        def __init__(self):
            self.reads = {}

        def get_table(self, name):
            n = self.reads[name] = self.reads.get(name, 0) + 1
            candidate = any(p["candidate"] == name for p in document["candidates"])
            return SimpleNamespace(
                etag=("version" if n == 1 else "changed") if candidate else "live",
                schema=[SchemaField("id", "INT64"), SchemaField("value", "INT64")],
            )

        def query(self, *args, **kwargs):
            pytest.fail("Changed candidate must not reach publication")

        def update_table(self, *args, **kwargs):
            pytest.fail("Changed candidate must not alter schemas")

    with pytest.raises(ValueError, match="Candidate changed"):
        promote(document, document["sha256"], Client())
