"""Synthetic complete-case parity independent of warehouse and provider access."""

from dataclasses import replace

import pytest
from app.agent.semantics import COMPONENTS, SemanticError, aggregate, load_contract
from app.agent.serving_contract import (
    aggregate_summaries,
    metric_matrix,
    summarize_rows,
)


def rows():
    return [
        dict(
            **{c: float(i) for c in COMPONENTS},
            season="2024-25",
            season_type="Regular Season",
            player_id=1,
            game_id=str(i),
            team_abbr="AAA" if i < 2 else "BBB",
        )
        for i in range(3)
    ]


@pytest.mark.parametrize("key", list(load_contract().metrics))
def test_all_catalog_metrics_match_raw_complete_case_aggregation(key):
    metric = load_contract().metric(key)
    sample = rows()
    for field in metric.components:
        sample[1][field] = None
        break
    summary = summarize_rows(sample, metric, "publication-a")
    for aggregation in metric.aggregations:
        assert aggregate_summaries([summary], metric, aggregation) == aggregate(
            sample, metric, aggregation
        )


def test_independent_expected_points_and_disjoint_phase_combination():
    metric = load_contract().metric("pts")
    sample = rows()
    sample[0]["pts"] = 0
    sample[1]["pts"] = None
    sample[2]["pts"] = 20
    first = summarize_rows(sample, metric, "a")
    playoff = dict(sample[2], game_id="playoff-1", season_type="Playoffs", pts=40)
    second = summarize_rows([playoff], metric, "a")
    answer = aggregate_summaries([first, second], metric, "average")
    assert answer["value"] == 20
    assert (
        answer["observed_games"],
        answer["valid_games"],
        answer["missing_component_games"],
    ) == (4, 3, 1)
    with pytest.raises(SemanticError, match="Mixed publication"):
        aggregate_summaries(
            [first, replace(second, publication_id="b")], metric, "average"
        )
    with pytest.raises(SemanticError, match="overlapping"):
        aggregate_summaries([first, first], metric, "average")


def test_ratio_uses_same_valid_set_and_zero_is_not_missing():
    metric = load_contract().metric("fg_pct")
    sample = rows()
    sample[0].update(fgm=1, fga=1)
    sample[1].update(fgm=0, fga=9)
    sample[2].update(fgm=None, fga=100)
    answer = aggregate_summaries([summarize_rows(sample, metric, "a")], metric, "ratio")
    assert answer["value"] == 0.1
    assert answer["denominator"] == 10
    sample = [dict(sample[0], fgm=0, fga=0)]
    answer = aggregate_summaries([summarize_rows(sample, metric, "a")], metric, "ratio")
    assert answer["valid_games"] == 1 and answer["value"] is None


def test_matrix_covers_catalog_without_enabling_routing():
    matrix = metric_matrix()
    assert {m["key"] for m in matrix} == set(load_contract().metrics)
    assert [m["key"] for m in matrix if m["pilot_eligible"]] == ["pts"]
    assert not any(m["routing_enabled"] for m in matrix)


@pytest.mark.parametrize("bad", [float("inf"), float("nan"), "not-a-number"])
def test_invalid_components_are_rejected_not_treated_as_missing(bad):
    metric = load_contract().metric("pts")
    sample = rows()
    sample[0]["pts"] = bad
    with pytest.raises(SemanticError):
        summarize_rows(sample, metric, "a")
    with pytest.raises(SemanticError):
        aggregate(sample, metric, "average")


def test_committed_metric_matrix_matches_executable_catalog():
    import json
    from pathlib import Path

    assert (
        json.loads(Path("contracts/ask_serving_metrics.json").read_text())
        == metric_matrix()
    )


def test_missing_components_are_metric_specific_and_empty_valid_set_is_unavailable():
    sample = rows()
    for row in sample:
        row["pts"] = None
        row["ast"] = 4
    contract = load_contract()
    points = aggregate_summaries(
        [summarize_rows(sample, contract.metric("pts"), "a")],
        contract.metric("pts"),
        "average",
    )
    assists = aggregate_summaries(
        [summarize_rows(sample, contract.metric("ast"), "a")],
        contract.metric("ast"),
        "average",
    )
    assert points["value"] is None and points["missing_component_games"] == 3
    assert assists["value"] == 4 and assists["valid_games"] == 3
    with pytest.raises(SemanticError, match="No appearance"):
        summarize_rows([], contract.metric("pts"), "a")
