"""Fail closed on source defects before a repaired snapshot/model run is accepted."""

from copy import deepcopy

import pytest
from scripts.evaluate_reporting_evidence import require_complete_statistics
from scripts.repair_context_snapshot import COUNTS, validate_phase


def sample():
    players, teams = [], []
    for team, opponent, offset, win in [
        ("AAA", "BBB", 0, True),
        ("BBB", "AAA", 10, False),
    ]:
        group = []
        for i in range(5):
            r = dict.fromkeys(COUNTS, 0)
            r.update(
                SEASON_ID="22025",
                GAME_ID="0022500001",
                GAME_DATE="2025-11-01",
                TEAM_ABBREVIATION=team,
                MATCHUP=f"{team} {'vs.' if win else '@'} {opponent}",
                WL="W" if win else "L",
                PLAYER_ID=offset + i,
                MIN=48,
                PTS=20 if win else 18,
                FGM=10 if win else 9,
                FGA=20,
            )
            group.append(r)
        players.extend(group)
        t = {**group[0], **{c: sum(r[c] for r in group) for c in COUNTS}, "MIN": 240}
        teams.append(t)
    return players, teams


def validate(p, t):
    return validate_phase(p, t, "2025-26", "Regular Season", complete_season=False)


def test_reconciles_team_totals_and_tolerates_rounding_and_team_turnovers():
    p, t = sample()
    p[0]["MIN"] += 1
    t[0]["TOV"] += 1
    assert validate(p, t)["status"] == "passed"


@pytest.mark.parametrize(
    "defect",
    [
        "null",
        "duplicate",
        "wrong_season",
        "partial_team",
        "bad_total",
        "bad_identity",
        "wrong_opponent",
        "missing_team",
        "minutes",
    ],
)
def test_source_defects_block_repair(defect):
    p, t = sample()
    if defect == "null":
        p[0]["FGA"] = None
    if defect == "duplicate":
        p.append(deepcopy(p[0]))
    if defect == "wrong_season":
        p[0]["GAME_ID"] = "0022400001"
    if defect == "partial_team":
        p.pop()
    if defect == "bad_total":
        t[0]["AST"] = 1
    if defect == "bad_identity":
        p[0]["PTS"] = 99
    if defect == "wrong_opponent":
        p[0]["MATCHUP"] = "AAA vs. CCC"
    if defect == "missing_team":
        t.pop()
    if defect == "minutes":
        p[0]["MIN"] = 40
    with pytest.raises(ValueError):
        validate(p, t)


def test_completed_season_gate_rejects_partial_extract():
    p, t = sample()
    with pytest.raises(ValueError, match="Incomplete regular season"):
        validate_phase(p, t, "2025-26", "Regular Season")


def test_missing_components_block_generation_but_empty_sample_is_allowed():
    prepared = [
        {
            "case": {"id": "E01"},
            "bundle": {
                "statistics": {
                    "metrics": [
                        {"key": "fga", "missing_games": 1, "previous_missing_games": 0}
                    ]
                }
            },
        }
    ]
    with pytest.raises(ValueError, match="E01:fga"):
        require_complete_statistics(prepared)
    prepared[0]["bundle"]["statistics"]["metrics"][0]["missing_games"] = 0
    require_complete_statistics(prepared)
