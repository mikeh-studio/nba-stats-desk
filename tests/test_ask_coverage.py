"""Independent fixture arithmetic and deliberate contract mutations."""

import copy
import json
from pathlib import Path

import pytest
from app.agent.availability_ask import parse_request
from app.agent.semantic_serving import source_players
from app.availability import load_availability
from scripts.ask_eval.fixtures import fixture_artifacts, load_inputs, validate_cases
from scripts.ask_eval.grading import grade

ROOT = Path(__file__).resolve().parents[1]
SOURCE, DOCUMENT = load_inputs(ROOT)


def test_distinct_and_paraphrase_counts_and_parent_contracts():
    distinct = {c["id"]: c for c in DOCUMENT["cases"] if not c.get("variant_of")}
    variants = [c for c in DOCUMENT["cases"] if c.get("variant_of")]
    assert len(distinct) == 100 and len(variants) == 24
    for case in variants:
        assert (
            case["turns"][0]["checks"]
            == distinct[case["variant_of"]]["turns"][0]["checks"]
        )
    bad = copy.deepcopy(DOCUMENT)
    bad["cases"][-1]["variant_of"] = bad["cases"][-1]["id"]
    with pytest.raises(ValueError, match="distinct scenario"):
        validate_cases(
            bad, json.loads((ROOT / "tests/fixtures/ask/schema.json").read_text())
        )


def test_independent_availability_fixture_arithmetic():
    # Independent explicit membership: no call into compare_availability.
    rows = {
        r["game_id"]: r
        for r in SOURCE["availability"]["stats"]["rows"]
        if r["player_id"] == 101
    }
    both, absent, unknown = ["g0", "g2"], ["g1", "g3", "g5"], ["g4"]
    assert set(both + absent + unknown) == set(rows)
    assert sum(rows[g]["pts"] for g in both) / len(both) == 2
    assert sum(rows[g]["pts"] for g in absent) / len(absent) == 6
    assert (
        sum(rows[g]["fgm"] for g in absent) / sum(rows[g]["fga"] for g in absent) == 1
    )
    assert len(SOURCE["availability"]["reports"]) == 2
    assert SOURCE["availability"]["participation"][0]["game_id"] == "g5"


@pytest.mark.parametrize(
    "path,wrong",
    [
        ("availability_scope.player_id", 202),
        ("availability_scope.teammate_id", 101),
        ("availability_scope.season", "2025-26"),
        ("availability_evidence.metrics.0.groups.both_played.value", 999),
        ("availability_evidence.metrics.0.groups.did_not_play.observed_games", 4),
        ("availability_evidence.excluded.unverified_status_or_membership", 0),
    ],
)
def test_expanded_oracle_rejects_identity_scope_value_and_membership_mutations(
    path, wrong
):
    case = next(c for c in DOCUMENT["cases"] if c["id"] == "availability_points")
    # Minimal independent envelope; no application answers used to author expected values.
    payload = {
        "status": "ok",
        "availability_scope": {
            "player_id": 101,
            "teammate_id": 202,
            "season": "2024-25",
        },
        "availability_evidence": {
            "metrics": [
                {
                    "metric": "pts",
                    "groups": {
                        "both_played": {"value": 2, "observed_games": 2},
                        "did_not_play": {"value": 6, "observed_games": 3},
                    },
                }
            ],
            "excluded": {"unverified_status_or_membership": 1},
            "groups": {
                "both_played": [{"game_id": "g0"}],
                "did_not_play": [{}, {}, {"game_id": "g5"}],
            },
        },
    }
    checks = case["turns"][0]["checks"]
    assert all(c["passed"] for c in grade(payload, checks))
    target = payload
    parts = path.split(".")
    for part in parts[:-1]:
        target = target[int(part)] if isinstance(target, list) else target[part]
    target[parts[-1]] = wrong
    assert not all(c["passed"] for c in grade(payload, checks))


def test_missingness_oracle_rejects_zero_and_absent_fields():
    case = next(c for c in DOCUMENT["cases"] if c["id"] == "availability_away")
    check = next(c for c in case["turns"][0]["checks"] if c["equals"] is None)
    for value in ({}, {"value": 0}):
        assert not grade(value, [{**check, "path": "value"}])[0]["passed"]
    assert grade({"value": None}, [{**check, "path": "value"}])[0]["passed"]


def test_availability_how_many_preserves_extra_filters(tmp_path):
    paths = fixture_artifacts(SOURCE, tmp_path)
    loaded = load_availability(paths["availability"])
    q = "When Blair Sample was out, how many points did Avery Example average in 2024-25?"
    args = (source_players(loaded[1]), "2024-25", None, {"AAA", "BBB"})
    assert parse_request(q, *args)["metrics"] == ["pts"]
    from app.agent.semantics import SemanticError

    with pytest.raises(SemanticError):
        parse_request(q + " in the fourth quarter", *args)


@pytest.mark.parametrize(
    "question", ["Name the MVP winner in 2023-24", "Who received MVP for 2023-24?"]
)
def test_award_paraphrases_still_reject_unsupported_constraints(question):
    from app.agent.award_lookup import resolve_award_question
    from app.agent.semantics import SemanticError

    assert resolve_award_question(question, "2024-25")["player_id"] == 203999
    with pytest.raises(SemanticError):
        resolve_award_question(question + " among rookies", "2024-25")
