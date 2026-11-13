"""Game membership predicates use source components before appearance windows."""

from dataclasses import replace
from datetime import date

import pytest
from app.agent.semantics import (
    Evidence,
    Query,
    SemanticError,
    load_contract,
    run_query,
    select_query_rows,
)
from app.agent.source_policy import capture_sql


def sample():
    rows = [
        dict(
            season="2024-25",
            season_type="Regular Season",
            game_id=f"{i:03}",
            player_id=player,
            game_date=f"2025-01-{i:02}",
            team_abbr="AAA",
            opponent_abbr="BBB",
            home_away="HOME",
            game_result="W" if i % 2 else "L",
            pts=i * 10,
            min=20 + i,
            fgm=1 if i == 1 else 0,
            fga=1 if i == 1 else 9,
            oreb=i,
            dreb=2 * i,
            pf=i,
        )
        for player in (1, 2)
        for i in range(1, 7)
    ]
    return Evidence(
        rows,
        frozenset({("2024-25", "Regular Season")}),
        "synthetic",
        "filters-v1",
        {("2024-25", "Regular Season"): "2025-01-06"},
        complete=True,
    )


def query(**changes):
    return Query("pts", "2024-25", "average", player_id=1, **changes)


def test_predicates_apply_before_last_and_prior_n_and_never_display_cap_selection():
    evidence = sample()
    current = query(
        game_result="W",
        stat_filters=[dict(component="min", op="gt", value=20)],
        window="last_n_games",
        n=2,
        limit=1,
    )
    selected = select_query_rows(evidence, current)
    assert [r["game_id"] for r in selected.rows if r["player_id"] == 1] == [
        "005",
        "003",
    ]
    assert run_query(evidence, current)["rows"][0]["value"] == 40
    prior = run_query(evidence, replace(current, window="prior_n_games"))["rows"][0]
    assert prior["game_ids"] == ["001"]
    assert prior["sample_warning"] == "partial_window"
    log = run_query(evidence, replace(current, operation="game_log"))
    assert log["observed_games"] == 2 and log["displayed_games"] == 1
    assert log["rows"][0]["game_id"] == "005"


@pytest.mark.parametrize(
    "op,value,expected",
    [
        ("gt", 30, ["006", "005", "004"]),
        ("gte", 30, ["006", "005", "004", "003"]),
        ("lt", 30, ["002", "001"]),
        ("lte", 30, ["003", "002", "001"]),
        ("eq", 30, ["003"]),
    ],
)
def test_raw_stat_operators(op, value, expected):
    result = run_query(
        sample(), query(stat_filters=[dict(component="pts", op=op, value=value)])
    )
    assert result["rows"][0]["game_ids"] == expected


def test_filters_combine_and_weekdays_are_monday_zero_without_becoming_a_range():
    evidence = sample()
    q = query(
        game_result="W",
        weekdays=[date(2025, 1, 3).weekday()],
        team_abbr="AAA",
        opponent_abbr="BBB",
        home_away="HOME",
        stat_filters=[
            dict(component="pts", op="gte", value=20),
            dict(component="oreb", op="lte", value=4),
        ],
    )
    assert run_query(evidence, q)["rows"][0]["game_ids"] == ["003"]
    assert run_query(evidence, replace(q, weekdays=[0]))["status"] == "no_observations"


@pytest.mark.parametrize(
    "changes",
    [
        dict(game_result="WIN"),
        dict(game_result=True),
        dict(weekdays=[]),
        dict(weekdays=[True]),
        dict(weekdays=[7]),
        dict(weekdays=[1, 1]),
        dict(weekdays="Monday"),
        dict(team_abbr=""),
        dict(opponent_abbr="???"),
        dict(stat_filters=[]),
        dict(stat_filters=[dict(component="fg_pct", op="gt", value=1)]),
        dict(stat_filters=[dict(component="pts", op="ne", value=1)]),
        dict(stat_filters=[dict(component="pts", op="eq", value=True)]),
        dict(stat_filters=[dict(component="pts", op="gt", value="10")]),
        dict(stat_filters=[dict(component="pts", op="gt", value=10**1000)]),
        dict(stat_filters=[dict(component="pts", op="gt", value=float("inf"))]),
        dict(stat_filters=[dict(component="pts", op="gt", value=float("nan"))]),
        dict(stat_filters=[dict(component="pts", op="gt", value=1, extra=True)]),
    ],
)
def test_invalid_filter_contract_refuses(changes):
    with pytest.raises(SemanticError) as exc:
        run_query(sample(), query(**changes))
    assert exc.value.code == "invalid_scope"


@pytest.mark.parametrize(
    "field,changes,value",
    [
        ("game_result", dict(game_result="W"), None),
        ("game_result", dict(game_result="W"), "?"),
        ("home_away", dict(home_away="HOME"), None),
        ("team_abbr", dict(team_abbr="AAA"), None),
        ("opponent_abbr", dict(opponent_abbr="BBB"), ""),
        ("pts", dict(stat_filters=[dict(component="pts", op="gt", value=1000)]), None),
    ],
)
def test_unknown_membership_cannot_disappear_behind_other_predicates(
    field, changes, value
):
    evidence = sample()
    rows = [dict(row) for row in evidence.rows]
    rows[0][field] = value
    # This row would be removed by the explicit loss filter before its unknown
    # component could be seen if predicates were applied independently.
    if field != "game_result":
        changes["game_result"] = "L"
    with pytest.raises(SemanticError) as exc:
        run_query(replace(evidence, rows=rows), query(**changes))
    assert exc.value.code == "incomplete_evidence"


def test_retained_wl_is_supported_but_conflicts_are_unknown_membership():
    evidence = sample()
    rows = [dict(row) for row in evidence.rows]
    for row in rows:
        row["wl"] = row.pop("game_result")
    assert (
        run_query(replace(evidence, rows=rows), query(game_result="W"))["rows"][0][
            "value"
        ]
        == 30
    )
    rows[0]["game_result"] = "L"
    with pytest.raises(SemanticError, match="game_result"):
        run_query(replace(evidence, rows=rows), query(game_result="W"))


def test_unfiltered_optional_metric_missingness_and_partial_rank_exclusion():
    evidence = sample()
    rows = [dict(row) for row in evidence.rows]
    for row in rows:
        for field in ("oreb", "dreb", "pf"):
            row.pop(field)
    old = replace(evidence, rows=rows)
    assert run_query(old, query())["rows"][0]["value"] == 35
    for metric in ("oreb", "dreb", "pf"):
        result = run_query(old, replace(query(), metric=metric))["rows"][0]
        assert result["value"] is None and result["valid_games"] == 0
    rows[0]["oreb"] = 0
    result = run_query(old, replace(query(), metric="oreb"))["rows"][0]
    assert result["value"] == 0 and result["status"] == "partial"
    assert (
        run_query(
            old,
            replace(
                query(), metric="oreb", operation="rank", player_id=None, min_games=1
            ),
        )["rows"]
        == []
    )


def test_optional_counts_totals_averages_and_pooled_filtered_ratios():
    evidence = sample()
    for metric, average, total in (("oreb", 3.5, 21), ("dreb", 7, 42), ("pf", 3.5, 21)):
        assert (
            run_query(evidence, replace(query(), metric=metric))["rows"][0]["value"]
            == average
        )
        assert (
            run_query(evidence, replace(query(), metric=metric, aggregation="total"))[
                "rows"
            ][0]["value"]
            == total
        )
    ratio = replace(query(game_result="W"), metric="fg_pct", aggregation="ratio")
    result = run_query(evidence, ratio)["rows"][0]
    assert result["value"] == pytest.approx(1 / 19)
    assert result["components"] == {"fgm": 1, "fga": 19}


def test_optional_source_projection_does_not_reference_absent_columns_directly():
    sql = capture_sql(
        ["test-project.nba_gold.fct_player_game_stats"], ["Regular Season"]
    )
    for field in ("oreb", "dreb", "pf"):
        assert (
            f"CAST(JSON_VALUE(TO_JSON_STRING(fact), '$.{field}') AS FLOAT64) AS {field}"
            in sql
        )
    assert "JSON_VALUE(TO_JSON_STRING(fact), '$.wl') AS game_result" in sql
    assert "COUNT(*) OVER()" in sql and "LIMIT @row_limit" in sql
    assert "WHERE season = @season_0 AND season_type IN UNNEST(@phases)" in sql
    assert {"oreb", "dreb", "pf"} <= set(load_contract().metrics)


def test_missing_peer_filter_membership_does_not_block_scalar_but_blocks_cohort():
    evidence = sample()
    rows = [dict(row) for row in evidence.rows]
    rows[-1]["game_result"] = None
    rows[-1]["min"] = None
    q = query(game_result="W", stat_filters=[dict(component="min", op="gt", value=20)])
    assert run_query(replace(evidence, rows=rows), q)["rows"][0]["value"] == 30
    for operation in ("rank", "percentile"):
        with pytest.raises(SemanticError) as exc:
            run_query(
                replace(evidence, rows=rows),
                replace(q, operation=operation, min_games=1),
            )
        assert exc.value.code == "incomplete_evidence"


@pytest.mark.parametrize(
    "changes",
    [
        dict(game_result="W"),
        dict(weekdays=[4]),
        dict(stat_filters=[dict(component="pts", op="gte", value=10)]),
    ],
)
def test_serving_requires_bound_detail_for_game_filters(changes):
    import json

    from app.agent.semantic_source import snapshot_digest, snapshot_evidence
    from app.agent.serving_runtime import PilotPublication
    from tests.test_serving_expansion import make_prepared
    from tests.test_serving_runtime import NOW

    _, previous = make_prepared()
    snapshot = json.loads(previous.detail_json)
    for index, row in enumerate(snapshot["rows"]):
        row["game_result"] = "W" if index % 2 else "L"
    snapshot["sha256"] = snapshot_digest(snapshot)
    evidence = snapshot_evidence(snapshot)
    pub = PilotPublication.build(
        snapshot,
        expected_snapshot_id=snapshot["sha256"],
        publication_id="filtered-detail",
        relation_version="filtered-detail/1",
        now=NOW,
        metrics=list(load_contract().metrics),
        include_detail=True,
    )
    q = query(**changes)
    assert not pub.approval.supports(q, now=NOW)
    assert pub.run_expanded(q, now=NOW, reuse_cohort=True) == run_query(evidence, q)
    assert (
        replace(pub, detail_json=None, detail_digest=None).run_expanded(
            q, now=NOW, reuse_cohort=True
        )
        is None
    )
    assert not pub._cohorts


def test_optional_projection_keeps_exact_source_policy_shape(monkeypatch):
    from app.agent import semantic_source
    from tests.test_scoped_source_policy import (
        CapturingClient,
        policy,
        regular_rows,
        source,
    )

    allowed = capture_sql(
        ["test-project.nba_gold.fct_player_game_stats"], ["Regular Season"]
    )
    # A different JSON path is a different projection, even with bounded reads.
    monkeypatch.setattr(
        semantic_source,
        "capture_sql",
        lambda *args: allowed.replace("$.oreb", "$.private"),
    )
    client = CapturingClient(rows=regular_rows())
    with pytest.raises(SemanticError) as exc:
        source(client, policy()).capture_scoped(["2024-25"], ["Regular Season"])
    assert exc.value.code == "source_policy_denied"
    assert client.calls == []


def test_capture_table_alias_precedes_time_travel_in_every_union_branch():
    sources = [
        "test-project.nba_gold_2023_24.fct_player_game_stats",
        "test-project.nba_gold_2024_25.fct_player_game_stats",
    ]
    sql = capture_sql(sources, ["Regular Season"])
    for relation in sources:
        assert f"FROM `{relation}` AS fact FOR SYSTEM_TIME AS OF @snapshot_at" in sql
    assert "FOR SYSTEM_TIME AS OF @snapshot_at AS fact" not in sql
