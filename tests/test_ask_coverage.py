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
    assert len(distinct) == 123 and len(variants) == 24
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


def test_inventory_preserves_review_and_reports_missing_critical_selections():
    from scripts.ask_eval.coverage import CRITICAL, summarize

    before = copy.deepcopy(DOCUMENT)
    summary = summarize(SOURCE, DOCUMENT)
    assert summary["counts"] == {
        "cases": 147,
        "distinct": 123,
        "paraphrases": 24,
        "turns": 180,
        "source_rows": 24,
    }
    assert summary["review_states"] == {"pending": 147}
    assert all(not value["absent"] for value in summary["critical_risks"].values())
    assert DOCUMENT == before
    for ids in CRITICAL.values():
        for cid in ids:
            assert summary["cases"][cid]["assertions"]
    subset = {**DOCUMENT, "cases": DOCUMENT["cases"][:1]}
    assert summarize(SOURCE, subset)["critical_risks"]["research"]["absent"]


def test_raw_rows_independently_bind_metric_oracles():
    # No application aggregator or renderer participates in expected arithmetic.
    rows = [r for r in SOURCE["rows"] if r["season_type"] == "Regular Season"]
    focal = [r for r in rows if r["player_id"] == 101]
    assert SOURCE["rows"][0] is focal[0]
    assert (focal[0]["player_id"], focal[0]["game_id"], focal[0]["pts"]) == (
        101,
        "g0",
        0,
    )
    assert [r["pts"] for r in focal] == [0, 2, 4, 6, 8, 10]
    assert sum(r["fgm"] for r in focal) == 3
    assert sum(r["fga"] for r in focal) == 30
    assert sum(r["fta"] for r in focal) == 0
    assert len(rows) == 18 and sum(r["pts"] for r in rows) == 210
    assert {r["player_id"] for r in rows} == {101, 202, 303}
    assert sum(r["pts"] for r in focal[1:]) / 5 == 6  # missing g0, not zero
    playoff = [
        r
        for r in SOURCE["rows"]
        if r["season_type"] == "Playoffs" and r["player_id"] == 101
    ]
    assert [r["game_date"] for r in playoff] == ["2025-04-16", "2025-04-17"]
    expected = {
        "fg_pooled": ("semantic_evidence.rows.0.value", 3 / 30),
        "baseline_points": ("semantic_evidence.baseline.value", 210 / 18),
        "fg_zero_denominator": ("semantic_evidence.rows.0.value", None),
        "null_metric": ("semantic_evidence.rows.0.value", 6),
    }
    for cid, (path, value) in expected.items():
        case = next(c for c in DOCUMENT["cases"] if c["id"] == cid)
        assert (
            next(c["equals"] for c in case["turns"][0]["checks"] if c["path"] == path)
            == value
        )


@pytest.mark.parametrize(
    "path,wrong",
    [
        ("semantic_evidence.player_id", 202),
        ("semantic_evidence.scope.season_type", "Playoffs"),
        ("semantic_evidence.scope.season", "2023-24"),
        ("semantic_evidence.player.value", 10),
        ("semantic_evidence.difference_unit", "percent"),
        ("tables.0.rows.1.1", "90.0%"),
    ],
)
def test_independent_reference_envelope_rejects_defects(path, wrong):
    payload = {
        "status": "ok",
        "semantic_evidence": {
            "player_id": 101,
            "scope": {"season_type": "Regular Season", "season": "2024-25"},
            "player": {"value": 0.1},
            "baseline": {"value": 0.1},
            "difference": 0,
            "difference_unit": "percentage points",
        },
        "agent_plan": {"route": "league_baseline"},
        "tables": [{"rows": [[], ["League", "10.0%"]]}],
    }
    checks = next(c for c in DOCUMENT["cases"] if c["id"] == "baseline_ratio")["turns"][
        0
    ]["checks"]
    assert all(c["passed"] for c in grade(payload, checks))
    target = payload
    parts = path.split(".")
    for part in parts[:-1]:
        target = target[int(part)] if isinstance(target, list) else target[part]
    key = int(parts[-1]) if isinstance(target, list) else parts[-1]
    target[key] = wrong
    failed = [c["path"] for c in grade(payload, checks) if not c["passed"]]
    assert failed == [path]


@pytest.mark.parametrize("remove", [False, True])
def test_date_boundary_mutation_rejected(remove):
    payload = {
        "status": "ok",
        "semantic_evidence": {
            "player": {"value": 0},
            "baseline": {"value": 10},
            "scope": {"start_date": "2025-04-10", "window_end": "2025-04-10"},
        },
    }
    checks = next(
        c for c in DOCUMENT["cases"] if c["id"] == "boundary_baseline_one_date"
    )["turns"][0]["checks"]
    assert all(c["passed"] for c in grade(payload, checks))
    if remove:
        del payload["semantic_evidence"]["scope"]["window_end"]
    else:
        payload["semantic_evidence"]["scope"]["start_date"] = "2025-04-11"
    assert not all(c["passed"] for c in grade(payload, checks))


@pytest.mark.parametrize(
    "metric,value,display",
    [("reb", 0.1, "10.0%"), ("fg_pct", None, "10.0%"), ("fg_pct", 0.1, "50.0%")],
)
def test_metric_and_display_mutations_use_actual_case_contract(metric, value, display):
    payload = {
        "status": "ok",
        "semantic_evidence": {
            "rows": [{"player_id": 101, "value": 0.1}],
            "metric": {"key": "fg_pct"},
            "scope": {"season": "2024-25", "season_type": "Regular Season"},
        },
        "agent_plan": {"route": "governed_metrics"},
        "tables": [{"rows": [["Avery Example", "10.0%"]]}],
    }
    checks = next(c for c in DOCUMENT["cases"] if c["id"] == "fg_pooled")["turns"][0][
        "checks"
    ]
    assert all(c["passed"] for c in grade(payload, checks))
    payload["semantic_evidence"]["metric"]["key"] = metric
    payload["semantic_evidence"]["rows"][0]["value"] = value
    payload["tables"][0]["rows"][0][1] = display
    assert not all(c["passed"] for c in grade(payload, checks))


def test_inventory_keeps_schema_valid_nonstring_labels_as_raw_assertions():
    from scripts.ask_eval.coverage import summarize

    document = copy.deepcopy(DOCUMENT)
    turn = next(c for c in document["cases"] if c["id"] == "points_mean")["turns"][0]
    for check in turn["checks"]:
        if check["path"] == "status":
            check["equals"] = ["ok"]
        if check["path"] == "agent_plan.route":
            check["equals"] = {"route": "governed_metrics"}
    validate_cases(
        document, json.loads((ROOT / "tests/fixtures/ask/schema.json").read_text())
    )
    record = summarize(SOURCE, document)["cases"]["points_mean"]
    assert record["asserted_routes"] == record["asserted_outcomes"] == []
    assert any(c["equals"] == ["ok"] for c in record["assertions"])
    assert any(
        c["equals"] == {"route": "governed_metrics"} for c in record["assertions"]
    )


def test_critical_recovery_invariants_are_exact_and_missing_checks_visible():
    from scripts.ask_eval.coverage import summarize

    summary = summarize(SOURCE, DOCUMENT)
    assert all(
        check["assertion_present"]
        for checks in summary["recovery_invariants"].values()
        for check in checks
    )
    altered = copy.deepcopy(DOCUMENT)
    case = next(
        c for c in altered["cases"] if c["id"] == "overview_pending_player_recovery"
    )
    case["turns"][2]["checks"] = [{"path": "status", "equals": "ok"}]
    assert not any(
        c["assertion_present"]
        for c in summarize(SOURCE, altered)["recovery_invariants"][case["id"]]
    )


def test_zero_denominator_null_cannot_be_replaced_with_zero():
    payload = {
        "status": "ok",
        "semantic_evidence": {
            "rows": [{"player_id": 101, "value": None}],
            "metric": {"key": "ft_pct"},
            "scope": {"season": "2024-25", "season_type": "Regular Season"},
        },
        "agent_plan": {"route": "governed_metrics"},
        "tables": [{"rows": [["Avery Example", "Unavailable"]]}],
    }
    checks = next(c for c in DOCUMENT["cases"] if c["id"] == "fg_zero_denominator")[
        "turns"
    ][0]["checks"]
    assert all(c["passed"] for c in grade(payload, checks))
    payload["semantic_evidence"]["rows"][0]["value"] = 0
    assert [c["path"] for c in grade(payload, checks) if not c["passed"]] == [
        "semantic_evidence.rows.0.value"
    ]


def test_recorded_zero_cannot_be_replaced_with_null():
    payload = {
        "status": "ok",
        "semantic_evidence": {
            "player": {"value": 0},
            "baseline": {"value": 10},
            "scope": {"start_date": "2025-04-10", "window_end": "2025-04-10"},
        },
    }
    checks = next(
        c for c in DOCUMENT["cases"] if c["id"] == "boundary_baseline_one_date"
    )["turns"][0]["checks"]
    assert all(c["passed"] for c in grade(payload, checks))
    payload["semantic_evidence"]["player"]["value"] = None
    assert [c["path"] for c in grade(payload, checks) if not c["passed"]] == [
        "semantic_evidence.player.value"
    ]


def test_requested_season_list_cannot_be_dropped():
    # Independent scoped archive envelope. Other overview checks are exercised
    # by the full endpoint suite; this isolates the actual case's archive check.
    case = next(c for c in DOCUMENT["cases"] if c["id"] == "overview_regular")
    checks = [
        c
        for c in case["turns"][0]["checks"]
        if c["path"] == "semantic_evidence.scope.seasons"
    ]
    assert len(checks) == 1
    payload = {"semantic_evidence": {"scope": {"seasons": ["2024-25", "2023-24"]}}}
    assert all(c["passed"] for c in grade(payload, checks))
    del payload["semantic_evidence"]["scope"]["seasons"]
    assert not grade(payload, checks)[0]["passed"]


@pytest.mark.parametrize(
    "value,display,failed",
    [
        (0.5, "10.0%", ["semantic_evidence.rows.0.value"]),
        (0.1, "50.0%", ["tables.0.rows.0.1"]),
        (0.5, "50.0%", ["semantic_evidence.rows.0.value", "tables.0.rows.0.1"]),
    ],
)
def test_mean_of_game_ratios_mutation_is_rejected_independently(value, display, failed):
    payload = {
        "status": "ok",
        "semantic_evidence": {
            "rows": [{"player_id": 101, "value": 0.1}],
            "metric": {"key": "fg_pct"},
            "scope": {"season": "2024-25", "season_type": "Regular Season"},
        },
        "agent_plan": {"route": "governed_metrics"},
        "tables": [{"rows": [["Avery Example", "10.0%"]]}],
    }
    checks = next(c for c in DOCUMENT["cases"] if c["id"] == "fg_pooled")["turns"][0][
        "checks"
    ]
    assert all(c["passed"] for c in grade(payload, checks))
    payload["semantic_evidence"]["rows"][0]["value"] = value
    payload["tables"][0]["rows"][0][1] = display
    assert [c["path"] for c in grade(payload, checks) if not c["passed"]] == failed


def test_raw_oracle_tolerances_and_difference_operands_are_bound():
    expected = [
        ("fg_pooled", "semantic_evidence.rows.0.value", 3 / 30),
        ("baseline_points", "semantic_evidence.player.value", 5),
        ("baseline_points", "semantic_evidence.baseline.value", 35 / 3),
        ("baseline_points", "semantic_evidence.difference", 5 - 35 / 3),
        ("null_metric", "semantic_evidence.rows.0.value", 6),
        ("baseline_ratio", "semantic_evidence.player.value", 0.1),
        ("baseline_ratio", "semantic_evidence.baseline.value", 0.1),
    ]
    for cid, path, expected_value in expected:
        case = next(c for c in DOCUMENT["cases"] if c["id"] == cid)
        check = next(c for c in case["turns"][0]["checks"] if c["path"] == path)
        assert check["equals"] == expected_value
        assert 0 <= check.get("absolute_tolerance", 0) <= 1e-9


@pytest.mark.parametrize("swap", [False, True])
def test_reference_points_sign_and_swapped_operands_fail(swap):
    payload = {
        "status": "ok",
        "semantic_evidence": {
            "player_id": 101,
            "scope": {"season_type": "Regular Season", "season": "2024-25"},
            "player": {"value": 5},
            "baseline": {"value": 35 / 3, "observed_games": 18},
            "difference": 5 - 35 / 3,
        },
        "agent_plan": {"route": "league_baseline"},
        "tables": [{"rows": [[], ["League", "11.7 per game"]]}],
        "charts": [{"series": [{"points": [{}, {"y": 11.7}]}]}],
    }
    checks = next(c for c in DOCUMENT["cases"] if c["id"] == "baseline_points")[
        "turns"
    ][0]["checks"]
    assert all(c["passed"] for c in grade(payload, checks))
    if swap:
        payload["semantic_evidence"]["player"]["value"] = 35 / 3
        payload["semantic_evidence"]["baseline"]["value"] = 5
        expected = [
            "semantic_evidence.player.value",
            "semantic_evidence.baseline.value",
        ]
    else:
        payload["semantic_evidence"]["difference"] = 35 / 3 - 5
        expected = ["semantic_evidence.difference"]
    assert [c["path"] for c in grade(payload, checks) if not c["passed"]] == expected


def test_every_critical_risk_requires_its_assertions_not_just_case_ids():
    from scripts.ask_eval.coverage import CRITICAL, summarize

    summary = summarize(SOURCE, DOCUMENT)
    assert all(
        not risk["missing_assertions"] for risk in summary["critical_risks"].values()
    )
    for risk, selections in CRITICAL.items():
        for cid in selections:
            document = copy.deepcopy(DOCUMENT)
            case = next(c for c in document["cases"] if c["id"] == cid)
            for turn in case["turns"]:
                turn["checks"] = [{"path": "status", "equals": "ok"}]
            assert summarize(SOURCE, document)["critical_risks"][risk][
                "missing_assertions"
            ][cid]


@pytest.mark.parametrize(
    "equals,tolerance", [(True, 0), (202.0, 0), (202, 1), (202, float("nan"))]
)
def test_invariant_matching_rejects_wrong_types_and_unsafe_tolerances(
    equals, tolerance
):
    from scripts.ask_eval.coverage import invariant_results

    inventory = {
        "case": {
            "assertions": [
                {
                    "turn": 1,
                    "path": "player",
                    "equals": equals,
                    "absolute_tolerance": tolerance,
                }
            ]
        }
    }
    assert not invariant_results(inventory, "case", [(1, "player", 202)])[0][
        "assertion_present"
    ]


def test_summary_is_detached_and_approval_is_only_declared():
    from scripts.ask_eval.coverage import summarize

    document = copy.deepcopy(DOCUMENT)
    summary = summarize(SOURCE, document)
    document["cases"][0]["review"]["status"] = "approved"
    document["cases"][0]["turns"][0]["checks"][0]["equals"] = "mutated"
    assert summary["cases"][document["cases"][0]["id"]]["review"]["status"] == "pending"
    assert (
        summary["cases"][document["cases"][0]["id"]]["assertions"][0]["equals"] == "ok"
    )
    assert "Unverified declarations" in summary["approval_verification"]
