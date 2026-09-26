"""Research parity, temporal isolation, scope rejection and immutable publication."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace

import pytest
from app.agent.semantics import COMPONENTS, Evidence, SemanticError
from app.research import ResearchQuery, answer_payload, breakdown
from app.research_snapshots import (
    append_snapshot,
    build_context_snapshot,
    digest,
    read_snapshot,
)
from app.research_studies import PAIRS, catalog
from app.seasons import dataset_for_season
from pydantic import ValidationError


def evidence():
    rows = []
    for day in (1, 2, 4, 7):
        for pid in (1, 2):
            rows.append(
                {
                    **dict.fromkeys(COMPONENTS, 0),
                    "season": "2025-26",
                    "season_type": "Regular Season",
                    "game_id": f"g{day}",
                    "game_date": f"2025-11-{day:02}",
                    "player_id": pid,
                    "player_name": f"Player {pid}",
                    "team_abbr": "ATL",
                    "opponent_abbr": "BOS",
                    "home_away": "home" if day % 2 else "away",
                    "pts": day * pid,
                    "min": 30,
                    "fgm": 1 if day == 1 else 0,
                    "fga": 1 if day == 1 else 9,
                }
            )
    return Evidence(
        rows,
        frozenset({("2025-26", "Regular Season")}),
        "fixture",
        "frozen-1",
        {("2025-26", "Regular Season"): "2025-11-07"},
        complete=True,
    )


def test_shared_ratios_and_rest_precede_date_filter():
    e = evidence()
    q = ResearchQuery(player_ids=[1, 2], metrics=["fg_pct", "pts"], end="2025-11-02")
    r = breakdown(e, q)
    assert r["players"][0]["metrics"][0]["result"]["display_value"] == 10
    assert answer_payload(r)["research"] == r
    rested = breakdown(
        e,
        ResearchQuery(
            player_ids=[1], start="2025-11-04", end="2025-11-07", rest="one_day"
        ),
    )
    assert [g["game_id"] for g in rested["players"][0]["games"]] == ["g4"]


def test_home_missing_scope_and_no_zero_imputation():
    e = evidence()
    e.rows[0]["pts"] = None
    r = breakdown(e, ResearchQuery(player_ids=[1], metrics=["pts"], home_away="home"))
    m = r["players"][0]["metrics"][0]["result"]
    assert m["valid_games"] == 1 and m["observed_games"] == 2
    assert m["value"] == 7
    with pytest.raises(SemanticError, match="lack context"):
        breakdown(
            e,
            ResearchQuery(
                player_ids=[1], teammate_id=2, teammate_status="participated"
            ),
        )
    with pytest.raises(ValidationError):
        ResearchQuery(player_ids=[1, 1])
    with pytest.raises(ValidationError):
        ResearchQuery(player_ids=[1], start="2025-12-01", end="2025-11-01")


def test_context_cannot_duplicate_grain_or_silently_cross_seasons():
    row = {
        "season": "2025-26",
        "game_id": "g1",
        "player_id": 1,
        "teammate_id": 2,
        "teammate_status": "participated",
    }
    with pytest.raises(SemanticError, match="Duplicate"):
        breakdown(evidence(), ResearchQuery(player_ids=[1]), [row, row])
    with pytest.raises(SemanticError):
        breakdown(evidence(), ResearchQuery(season="2024-25", player_ids=[1]))
    assert dataset_for_season("nba_gold", "2025-26") == "nba_gold"
    assert dataset_for_season("nba_gold", "2024-25") == "nba_gold_2024_25"


def context_inputs():
    game = {
        "season": "2025-26",
        "game_id": "g1",
        "game_date": "2025-11-01",
        "team_abbr": "ATL",
        "opponent_abbr": "BOS",
        "home_away": "home",
        "scheduled_start_utc": "2025-11-01T23:00:00Z",
        "ingested_at_utc": "2025-11-01T10:00:00Z",
        "source_updated_at_utc": "2025-11-01T09:00:00Z",
        "postponed": False,
    }
    memberships = [
        {
            "season": "2025-26",
            "player_id": p,
            "team_abbr": "ATL",
            "valid_from": "2025-10-01",
            "valid_to": "2025-12-01",
            "source_urls": ["https://example.test/roster"],
            "source_published_at": ["2025-10-01T00:00:00Z"],
            "reviewed_at": "2025-10-02T00:00:00Z",
            "basis": "fixture",
        }
        for p in (1, 2)
    ]
    return dict(
        games=[game],
        reports=[],
        memberships=memberships,
        pairs=[{"player_id": 1, "teammate_id": 2}],
        as_of_ts="2025-11-01T12:00:00Z",
        captured_at="2025-11-01T12:00:00Z",
        kind="captured",
    )


def test_future_schedule_is_known_but_late_inputs_are_not():
    inputs = context_inputs()
    original = build_context_snapshot(**inputs)
    assert len(original["rows"]) == 1
    changed = deepcopy(inputs)
    changed["games"][0]["ingested_at_utc"] = "2025-11-01T13:00:00Z"
    assert build_context_snapshot(**changed)["rows"] == []
    changed = deepcopy(inputs)
    changed["memberships"][0]["reviewed_at"] = "2026-01-01T00:00:00Z"
    assert build_context_snapshot(**changed)["rows"] == []


def test_atomic_retry_conflict_corruption_and_concurrent_creation(tmp_path):
    doc = build_context_snapshot(**context_inputs())
    target = tmp_path / "snapshot.json"
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: append_snapshot(target, doc), range(4)))
    assert results.count(True) == 1
    assert read_snapshot(target) == doc
    changed = {**doc, "source_status": {"injuries": "failed"}}
    changed["sha256"] = digest({k: v for k, v in changed.items() if k != "sha256"})
    with pytest.raises(ValueError, match="different content"):
        append_snapshot(target, changed)
    changed["sha256"] = "bad"
    with pytest.raises(ValueError, match="checksum"):
        append_snapshot(tmp_path / "bad.json", changed)


def test_all_three_study_cards_remain_visible_without_artifacts():
    entries = catalog(None)
    assert len(entries) == 3
    assert [s["pair_id"] for s in entries] == [s["pair_id"] for s in PAIRS]
    assert all(
        len(s["metrics"]) == 17 and s["claim_level"] == "insufficient_evidence"
        for s in entries
    )


def test_future_stats_do_not_change_a_bounded_breakdown():
    e = evidence()
    q = ResearchQuery(player_ids=[1], end="2025-11-02")
    before = breakdown(e, q)
    rows = deepcopy(e.rows)
    rows[-1]["pts"] = 999
    assert breakdown(replace(e, rows=rows), q) == before


def test_multi_metric_builder_preserves_missing_stats_and_rejects_unbound_causal():
    from scripts.build_research_studies import build_study
    from tests.test_teammate_readiness import fixture

    stats, reports, games, memberships, spec = fixture()
    pair = dict(
        pair_id="fictional",
        player_id=1,
        teammate_id=2,
        player_name="Focal Player",
        teammate_name="Other Player",
    )
    study = build_study(pair, spec, stats, reports, games, memberships)
    metrics = {m["metric"]: m for m in study["metrics"]}
    assert len(metrics) == 17
    assert metrics["pts"]["descriptive"]["difference"] == 20
    assert metrics["ast"]["descriptive"]["difference"] == 4
    assert metrics["reb"]["descriptive"]["difference"] is None
    assert all(m["causal"]["estimate"] is None for m in study["metrics"][:9])
    with pytest.raises(ValueError, match="bind the exact"):
        build_study(
            pair, spec, stats, reports, games, memberships, causal_spec={"version": 1}
        )


def test_context_changes_query_identity():
    q = ResearchQuery(player_ids=[1])
    context = [{"season": "2025-26", "game_id": "g1", "player_id": 1, "teammate_id": 2}]
    assert (
        breakdown(evidence(), q)["query_id"]
        != breakdown(evidence(), q, context)["query_id"]
    )


def test_reviewed_game_context_can_supply_venue_without_status_filter():
    e = evidence()
    for row in e.rows:
        row.pop("home_away")
    context = [
        {
            "season": "2025-26",
            "game_id": "g1",
            "player_id": 1,
            "teammate_id": 2,
            "home_away": "home",
        }
    ]
    result = breakdown(e, ResearchQuery(player_ids=[1], home_away="home"), context)
    assert [g["game_id"] for g in result["players"][0]["games"]] == ["g1"]
    assert result["players"][0]["filter_missing_games"] == 3
    conflicting = [*context, {**context[0], "teammate_id": 3, "home_away": "away"}]
    with pytest.raises(SemanticError, match="Conflicting"):
        breakdown(e, ResearchQuery(player_ids=[1]), conflicting)


def test_research_followup_does_not_capture_unrelated_new_questions():
    from app.agent.research_ask import wants_research_followup

    assert wants_research_followup("And rebounds instead?")
    assert not wants_research_followup("Who leads the league in assists?")


@pytest.mark.parametrize(
    "question",
    [
        "LeBron with Luka out",
        "Jalen Johnson without Trae",
        "Brunson with Hart",
        "LeBron James without Luka Dončić",
    ],
)
def test_selected_pair_shorthand_routes_to_multi_stat_research(question):
    from app.agent.research_ask import wants_research

    assert wants_research(question)


def test_signed_plus_minus_breakdown_and_study_missingness():
    from scripts.build_research_studies import build_study
    from tests.test_teammate_readiness import fixture

    e = evidence()
    own = [r for r in e.rows if r["player_id"] == 1]
    for row, value in zip(own, [-12, 4, None, 0]):
        row["plus_minus"] = value
    result = breakdown(e, ResearchQuery(player_ids=[1], metrics=["plus_minus"]))
    metric = result["players"][0]["metrics"][0]["result"]
    assert metric["valid_games"] == 3
    assert metric["value"] == pytest.approx(-8 / 3)
    stats, reports, games, memberships, spec = fixture()
    stats[0]["plus_minus"], stats[1]["plus_minus"] = -10, 2
    pair = dict(pair_id="fictional", player_id=1, teammate_id=2)
    study = build_study(pair, spec, stats, reports, games, memberships)
    pm = next(m for m in study["metrics"] if m["metric"] == "plus_minus")
    assert pm["descriptive"]["difference"] == 12
    assert pm["causal"]["estimate"] is None
    stats[1]["plus_minus"] = None
    study = build_study(pair, spec, stats, reports, games, memberships)
    assert (
        next(m for m in study["metrics"] if m["metric"] == "plus_minus")["descriptive"][
            "difference"
        ]
        is None
    )
