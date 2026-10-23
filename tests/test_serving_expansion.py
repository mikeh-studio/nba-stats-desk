import pytest
from app.agent.semantic_source import (
    BigQuerySemanticSource,
    snapshot_digest,
    snapshot_evidence,
)
from app.agent.semantics import Query, load_contract, run_query
from app.agent.serving_runtime import PilotPublication
from tests.test_semantic_source import Client
from tests.test_serving_runtime import NOW


def make_prepared():
    snap = BigQuerySemanticSource(Client(), project="test-project").capture(["2024-25"])
    for index, row in enumerate(snap["rows"]):
        row["home_away"] = "HOME" if index % 2 == 0 else "AWAY"
    snap["source_timestamp"] = NOW.isoformat()
    snap["sha256"] = snapshot_digest(snap)
    return snapshot_evidence(snap), PilotPublication.build(
        snap,
        expected_snapshot_id=snap["sha256"],
        publication_id="expanded",
        relation_version="expanded/1",
        now=NOW,
        metrics=list(load_contract().metrics),
        include_detail=True,
    )


@pytest.fixture
def prepared():
    return make_prepared()


@pytest.mark.parametrize("metric", list(load_contract().metrics))
@pytest.mark.parametrize("phase", ["Regular Season", "Playoffs", "Both"])
def test_expanded_metrics_rank_and_summary_parity(prepared, metric, phase):
    evidence, pub = prepared
    m = load_contract().metric(metric)
    for aggregation in m.aggregations:
        for op in ["summary", "rank", "percentile"]:
            q = Query(
                metric=metric,
                season="2024-25",
                season_type=phase,
                aggregation=aggregation,
                operation=op,
                player_id=1 if op != "rank" else None,
                min_games=1,
                min_attempts=1 if m.denominator else None,
            )
            assert pub.run_expanded(q, now=NOW) == run_query(evidence, q)


@pytest.mark.parametrize(
    "changes",
    [
        dict(window="last_n_games", n=2),
        dict(window="date_range", start_date="2025-01-01", as_of="2025-06-01"),
        dict(home_away="HOME"),
        dict(team_abbr="AAA"),
        dict(opponent_abbr="BBB"),
        dict(operation="game_log"),
    ],
)
def test_bound_game_detail_parity(prepared, changes):
    e, p = prepared
    q = Query(
        metric="pts", season="2024-25", aggregation="average", player_id=1, **changes
    )
    assert p.run_expanded(q, now=NOW) == run_query(e, q)


def test_whole_source_game_evidence_for_existing_rich_builders(prepared):
    e, p = prepared
    assert p.detail_evidence(now=NOW) == e


@pytest.mark.parametrize("operation", ["summary", "rank", "percentile"])
def test_shared_cohort_isolated_outputs_and_exact_scope(prepared, operation):
    evidence, pub = prepared
    for player in [1, 2, None, 999]:
        q = Query(
            metric="pts",
            season="2024-25",
            aggregation="average",
            operation=operation,
            player_id=player,
            min_games=1,
        )
        expected = run_query(evidence, q)
        result = pub.run_expanded(q, now=NOW, reuse_cohort=True)
        assert result == expected
        result["cohort"]["size"] = -1
        if result["rows"]:
            result["rows"][0]["game_ids"].clear()
        assert pub.run_expanded(q, now=NOW, reuse_cohort=True) == expected
    assert len(pub._cohorts) == 1
