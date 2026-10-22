from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.gamebook_cloud import CONTRACT, FACTS, publication_sql, target
from scripts.gamebook_store import SCHEMA, TABLES


def test_bigquery_contract_matches_local_column_order_and_nullability():
    local = sqlite3.connect(":memory:")
    local.executescript(SCHEMA.read_text())
    schemas = json.loads(CONTRACT.read_text())
    assert set(schemas) == set(TABLES)
    for table in TABLES:
        fields = schemas[table]
        sqlite_fields = local.execute(f"PRAGMA table_info({table})").fetchall()
        assert [f["name"] for f in fields] == [f[1] for f in sqlite_fields]
        for field, (_, name, kind, notnull, _, pk) in zip(fields, sqlite_fields):
            assert field["mode"] == ("REQUIRED" if notnull or pk else "NULLABLE")
            expected = (
                "DATE"
                if name == "game_date"
                else "TIMESTAMP"
                if name in ("started_at", "finished_at", "published_at")
                else {"TEXT": "STRING", "INTEGER": "INTEGER", "REAL": "FLOAT"}[kind]
            )
            assert field["type"] == expected
    local.close()


@pytest.mark.parametrize(
    "dataset",
    ["nba_gold", "nba_bronze", "nba_gamebook_dev;DROP TABLE x", "nba_gamebook_prod"],
)
def test_production_and_injected_targets_are_refused(dataset):
    with pytest.raises(ValueError):
        target("example-project", dataset)


def test_publication_has_atomic_guarded_scope_and_no_destructive_replacement():
    staging = {t: f"candidate_abcdef_{t}" for t in FACTS}
    sql = publication_sql(
        target("example-project", "nba_gamebook_dev_test_demo"), staging
    )
    assert sql.startswith("BEGIN TRANSACTION;")
    assert sql.index("stale_publisher") < sql.index("INSERT INTO")
    assert sql.index("injected_failure_after_publication_writes") < sql.index(
        "COMMIT TRANSACTION;"
    )
    assert (
        "UPDATE `example-project.nba_gamebook_dev_test_demo.gamebook_store_control`"
        in sql
    )
    assert "DELETE FROM" not in sql and "TRUNCATE" not in sql
    staging["gamebook_events"] = "production_table"
    with pytest.raises(ValueError):
        publication_sql("example-project.nba_gamebook_dev", staging)
