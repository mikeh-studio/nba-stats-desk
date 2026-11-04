"""Offline parse/schema checks plus dialect-limited aggregate execution."""

import sqlite3

import pytest
import sqlglot
from app.serving.core_dbt import staging_schema
from app.serving.core_sql import PROFILE, compile_candidate, metric_array
from scripts.build_synthetic_core_replay import synthetic_bundle
from sqlglot import exp


def test_sql_dependency_closure_and_columns():
    sql = compile_candidate(
        project="synthetic-offline",
        staging_dataset="staging_fixture",
        candidate_dataset="candidate_fixture",
        materialized_at="2026-11-01T16:00:00Z",
    )
    assert set(sql) == set(PROFILE["assets"])
    built = set()
    for name, query in sql.items():
        tree = sqlglot.parse_one(query, read="bigquery")
        assert set(tree.named_selects) == set(
            PROFILE["assets"][name]["required_fields"]
        )
        for table in tree.find_all(exp.Table):
            if table.db == "candidate_fixture":
                assert table.name in built
            if table.db:
                assert table.db in {"staging_fixture", "candidate_fixture"}
        built.add(name)
    for kwargs in [
        {"candidate_dataset": "staging_fixture"},
        {"project": "x`; DROP TABLE anything; --"},
        {"materialized_at": "2026-11-01"},
    ]:
        with pytest.raises(ValueError):
            compile_candidate(
                **(
                    dict(
                        project="synthetic-offline",
                        staging_dataset="staging_fixture",
                        candidate_dataset="candidate_fixture",
                        materialized_at="2026-11-01T16:00:00Z",
                    )
                    | kwargs
                )
            )


def test_nba_minutes_use_seconds_precision_in_sql_and_staging():
    sql = compile_candidate(
        project="synthetic-offline",
        staging_dataset="staging_fixture",
        candidate_dataset="candidate_fixture",
        materialized_at="2026-11-01T16:00:00Z",
        provider="nba_api",
    )
    assert "'second_reported' AS precision" in sql["core_player_summary"]
    assert staging_schema("appearances", provider="nba_api")["min"] == "FLOAT64"
    assert staging_schema("appearances", provider="espn")["min"] == "INT64"


def test_sql_metric_expressions_against_independent_expected_and_python():
    # This is SQLite execution after transpilation, NOT BigQuery acceptance.
    bundle = synthetic_bundle()
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    facts = bundle["models"]["fct_player_game_stats"]
    columns = list(facts[0])
    db.execute(
        "CREATE TABLE facts ("
        + ",".join(
            '"'
            + c
            + '" '
            + (
                "REAL"
                if c
                not in {
                    "game_id",
                    "player_name",
                    "team_abbr",
                    "opponent_abbr",
                    "home_away",
                    "game_date",
                    "matchup",
                    "season",
                    "season_type",
                    "wl",
                    "ingested_at_utc",
                }
                else "TEXT"
            )
            for c in columns
        )
        + ")"
    )
    db.executemany(
        "INSERT INTO facts VALUES (" + ",".join("?" for c in columns) + ")",
        [[r[c] for c in columns] for r in facts],
    )
    tree = sqlglot.parse_one(
        "SELECT " + metric_array("core_player_summary") + " AS metrics FROM facts f",
        read="bigquery",
    )

    def run(pid):
        result = []
        for struct in tree.find_all(exp.Struct):
            select = (
                exp.select(
                    *[
                        prop.expression.copy().as_(prop.this.name)
                        for prop in struct.expressions
                    ]
                )
                .from_("facts AS f")
                .where(f"player_id={pid}")
            )
            result.append(dict(db.execute(select.sql(dialect="sqlite")).fetchone()))
        return result

    rows = run(101)
    fg = next(r for r in rows if r["metric"] == "fg_pct")
    assert fg["value"] == pytest.approx(4 / 20) and fg["denominator"] == 20
    assert (
        next(
            r for r in rows if r["metric"] == "pts" and r["aggregation"] == "per_game"
        )["value"]
        == 5
    )
    for pid in [101, 202]:
        expected = next(
            p["metrics"]
            for p in bundle["models"]["core_player_summary"]
            if p["player_id"] == pid
        )
        for actual, want in zip(run(pid), expected):
            assert actual == pytest.approx(want)
    empty = run(999)
    assert next(r for r in empty if r["metric"] == "gp")["value"] == 0
    assert all(
        r["value"] is None and r["unavailable_reason"] == "no_appearances"
        for r in empty
        if r["metric"] != "gp"
    )
    db.close()
