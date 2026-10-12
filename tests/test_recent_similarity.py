import copy
import math

import pytest
from app.agent.participation import answer_known_nonparticipation
from app.agent.recent_similarity import build_recent_similarity
from app.agent.reference_ask import parse_reference
from app.agent.semantics import SemanticError
from scripts.ask_eval.fixtures import FrozenWarehouse
from tests.test_ask_harness import SOURCE, agent


def test_default_recent_comparison_has_independent_values_and_standardized_distance():
    a = agent()
    a.client = object()
    result = a.answer("Who is similar to Avery Example in the last 5 games in 2024-25?")
    ev = result["semantic_evidence"]
    assert result["status"] == "ok"
    assert ev["kind"] == "recent_statistical_similarity"
    assert ev["metrics"] == ["pts", "reb", "ast"]
    assert ev["player"]["values"] == {"pts": 6, "reb": 4, "ast": 3}
    assert ev["player"]["game_ids"] == ["g1", "g2", "g3", "g4", "g5"]
    assert ev["rows"][0]["player_id"] == 202
    pts_sd = math.sqrt(sum((x - 12) ** 2 for x in [6, 13, 17]) / 3)
    ast_sd = math.sqrt(sum((x - 13 / 3) ** 2 for x in [3, 5, 5]) / 3)
    distance = math.sqrt(((7 / pts_sd) ** 2 + (2 / ast_sd) ** 2) / 3)
    assert ev["rows"][0]["similarity_score"] == pytest.approx(1 / (1 + distance))
    assert ev["cohort"]["standard_deviations"]["reb"] == 0
    assert "Since you did not specify" in result["answer"]
    assert "different dates" in result["assumptions"][0]


def test_explicit_metric_and_followup_keep_recent_scope():
    a = agent()
    a.answer(
        "Who is similar to Blair Sample in the last five games in 2024-25?",
        conversation_id="recent",
    )
    result = a.answer("What about assists?", conversation_id="recent")
    ev = result["semantic_evidence"]
    assert ev["player_id"] == 202 and ev["scope"]["n"] == 5
    assert ev["metrics"] == ["ast"] and not ev["metrics_defaulted"]
    assert ev["rows"][0]["player_id"] == 303
    assert ev["rows"][0]["similarity_score"] == 1


@pytest.mark.parametrize(
    "window",
    [
        "last 5 regular season games in the playoffs",
        "last 5 playoff games in regular season",
    ],
)
def test_recent_comparison_rejects_conflicting_phases(window):
    result = agent().answer(
        f"Who is similar to Avery Example in the {window} in 2024-25?"
    )
    assert result["status"] != "ok" and not result["tables"]


def test_suggested_recent_followup_preserves_playoff_scope():
    from app.agent.recent_similarity import render_recent_similarity

    result = agent().answer(
        "Who is similar to Avery Example in the last 5 games in 2024-25?"
    )
    evidence = copy.deepcopy(result["semantic_evidence"])
    evidence["scope"]["season_type"] = "Playoffs"
    player = {"player_id": 101, "player_name": "Avery Example", "aliases": []}
    followup = render_recent_similarity(evidence, player)["followups"][0]
    _, season, spec = parse_reference(followup, [player], "2024-25", "similarity")
    assert season == "2024-25"
    assert spec["season_type"] == "Playoffs"
    assert spec["metrics"] == ["pts"]
    assert spec["n"] == 5


@pytest.mark.parametrize(
    "suffix",
    [
        "at home",
        "per 36 minutes",
        "excluding Blair Sample",
        "in wins",
        "in shooting",
        "with at least 30 minutes",
    ],
)
def test_recent_similarity_does_not_silently_drop_filters(suffix):
    r = agent().answer(
        "Who is similar to Avery Example in the last 5 games in 2024-25 " + suffix + "?"
    )
    assert r["status"] != "ok" and not r["tables"]


@pytest.mark.parametrize("mode", ["missing", "short"])
def test_recent_similarity_requires_full_anchor_sample(mode):
    source = copy.deepcopy(SOURCE)
    if mode == "missing":
        next(
            r for r in source["rows"] if r["player_id"] == 101 and r["game_id"] == "g5"
        )["ast"] = None
    else:
        source["rows"] = [
            r
            for r in source["rows"]
            if r["player_id"] != 101 or r["game_id"] not in {"g0", "g1"}
        ]
    _, evidence = FrozenWarehouse(source, "complete").load(["2024-25"])
    with pytest.raises(SemanticError, match="complete values"):
        build_recent_similarity(
            evidence,
            {"player_id": 101},
            "2024-25",
            {
                "n": 5,
                "metrics": ["pts", "reb", "ast"],
                "season_type": "Regular Season",
                "metrics_defaulted": True,
                "limit": 5,
            },
        )


def test_verified_nonparticipation_has_no_fabricated_zero_and_no_model_call():
    a = agent()
    a.client = object()
    r = a.answer("Stephen Curry points in the 2025-26 Finals")
    assert r["status"] == "not_applicable" and r["tables"] == []
    assert r["participation_evidence"]["player_id"] == 201939
    assert r["participation_evidence"]["participated"] is False
    assert "did not participate" in r["answer"]
    assert "https://www.nba.com/" in r["answer"]


@pytest.mark.parametrize(
    "q",
    [
        "Stephen Curry points in the 2024-25 Finals",
        "Nikola Jokic points in the 2025-26 Finals",
        "Stephen Curry points in the 2025-26 conference finals",
        "Compare Stephen Curry and Jalen Brunson in the 2025-26 Finals",
        "If Stephen Curry played in the 2025-26 Finals",
        "Stephen Curry points in the 2025-26 Finals at home",
    ],
)
def test_unknown_or_different_participation_is_not_inferred(q):
    assert answer_known_nonparticipation(q, "2025-26") is None
