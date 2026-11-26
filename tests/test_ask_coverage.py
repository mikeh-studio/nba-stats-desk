"""Independent fixture arithmetic and deliberate contract mutations."""

import copy
import json
from collections import defaultdict
from pathlib import Path
from statistics import median

import pytest
from app.agent.availability_ask import parse_request
from app.agent.semantic_serving import source_players
from app.availability import load_availability
from scripts.ask_eval.fixtures import (
    FrozenWarehouse,
    expectation_digest,
    fixture_artifacts,
    frozen_repository,
    load_inputs,
    validate_cases,
)
from scripts.ask_eval.grading import grade
from tests.test_semantic_migration import settings

ROOT = Path(__file__).resolve().parents[1]
SOURCE, DOCUMENT = load_inputs(ROOT)
SCHEMA = json.loads((ROOT / "tests/fixtures/ask/schema.json").read_text())


def test_distinct_and_paraphrase_counts_and_parent_contracts():
    distinct = {c["id"]: c for c in DOCUMENT["cases"] if not c.get("variant_of")}
    variants = [c for c in DOCUMENT["cases"] if c.get("variant_of")]
    assert len(distinct) == 129 and len(variants) == 24
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
        "cases": 153,
        "distinct": 129,
        "paraphrases": 24,
        "turns": 195,
        "source_rows": 24,
    }
    assert summary["review_states"] == {"pending": 153}
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


def test_v1_summary_is_detached_and_approval_is_only_declared():
    from scripts.ask_eval.coverage import summarize

    document = copy.deepcopy(DOCUMENT)
    document["schema_version"] = 1
    document["cases"][1]["review"]["status"] = "approved"
    summary = summarize(SOURCE, document)
    document["cases"][0]["review"]["status"] = "approved"
    document["cases"][0]["turns"][0]["checks"][0]["equals"] = "mutated"
    assert summary["cases"][document["cases"][0]["id"]]["review"]["status"] == "pending"
    assert (
        summary["cases"][document["cases"][0]["id"]]["assertions"][0]["equals"] == "ok"
    )
    assert "Unverified declarations" in summary["approval_verification"]
    assert summary["schema_version"] == 1
    assert summary["verified_approved"] == 0
    assert summary["unverified_approved"] == 1


@pytest.fixture
def reviewed_document():
    case = copy.deepcopy(DOCUMENT["cases"][0])
    case["review"] = {
        "status": "approved",
        "reviewer": "Synthetic test reviewer",
        "reviewed_at": "2025-04-20T10:00:00Z",
        "expectation_sha256": expectation_digest(case),
    }
    return {"schema_version": 2, "human_reviewed": True, "cases": [case]}


def test_v2_approved_case_is_hash_verified(reviewed_document):
    from scripts.ask_eval.coverage import summarize

    case = reviewed_document["cases"][0]
    case["turns"][0]["planner_response"] = {"changed": "controlled plumbing"}
    case["oracle_note"] = "An editorial note changed."
    validate_cases(reviewed_document, SCHEMA)
    summary = summarize(SOURCE, reviewed_document)
    assert summary["schema_version"] == 2
    assert summary["declared_human_reviewed"]
    assert summary["verified_approved"] == 1
    assert summary["unverified_approved"] == 0
    assert "Hash-bound verification" in summary["approval_verification"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("question", "Changed question"),
        ("checks", [{"path": "status", "equals": "changed"}]),
        ("reset_context", True),
        ("client_context", {"question": "restored"}),
        ("expected_failure", {"http_status": 502, "detail": "failed"}),
    ],
)
def test_v2_approval_detects_changed_turn(reviewed_document, field, value):
    from scripts.ask_eval.coverage import summarize

    reviewed_document["human_reviewed"] = False
    reviewed_document["cases"][0]["turns"][0][field] = value
    with pytest.raises(ValueError, match="expectation_sha256 does not match"):
        validate_cases(reviewed_document, SCHEMA)
    summary = summarize(SOURCE, reviewed_document)
    assert summary["verified_approved"] == 0
    assert summary["unverified_approved"] == 1
    assert "Unverified declarations" in summary["approval_verification"]


@pytest.mark.parametrize("field", ["reviewer", "reviewed_at", "expectation_sha256"])
def test_v2_approval_requires_review_provenance(reviewed_document, field):
    reviewed_document["cases"][0]["review"].pop(field)
    with pytest.raises(ValueError, match=field):
        validate_cases(reviewed_document, SCHEMA)


@pytest.mark.parametrize(
    ("field", "value"), [("reviewer", " "), ("reviewed_at", "April 20, 2025")]
)
def test_v2_approval_rejects_invalid_review_provenance(reviewed_document, field, value):
    reviewed_document["cases"][0]["review"][field] = value
    with pytest.raises(ValueError, match=field):
        validate_cases(reviewed_document, SCHEMA)


def test_rich_warehouse_loads_only_requested_seasons_and_observed_phases():
    warehouse = FrozenWarehouse(SOURCE, "rich_league")
    _, historical = warehouse.load(["2023-24"])
    _, current = warehouse.load(["2024-25"])
    _, union = warehouse.load(["2024-25", "2023-24"])
    assert len(historical.rows) == 48
    assert len(current.rows) == 110
    assert union.rows == historical.rows + current.rows
    assert historical.data_through == {("2023-24", "Regular Season"): "2024-04-05"}
    assert current.data_through == {("2024-25", "Regular Season"): "2025-04-09"}
    assert union.data_through == historical.data_through | current.data_through
    assert len({row["player_id"] for row in union.rows}) == 12
    assert {row["home_away"] for row in union.rows} == {"HOME", "AWAY"}
    assert all(row["reb"] == row["oreb"] + row["dreb"] for row in union.rows)

    repo = frozen_repository(settings(), SOURCE, "rich_league")
    health = repo.get_health()["season_coverage"]
    assert health["player_game_rows"] == 110
    assert health["game_count"] == 20
    assert health["first_game_date"] == "2024-10-23"
    assert repo.search_players("Casey")[0]["team_abbr"] == "CCC"
    detail = repo.get_player_detail(1103)
    assert detail["player"]["team_abbr"] == "CCC"
    assert len(detail["game_log"]["games"]) == 14
    log = repo.get_player_game_log(1103, start_date="2025-01-01", limit=2)
    assert [row["game_date"] for row in log["games"]] == ["2025-03-19", "2025-04-09"]


def expected_check(case_id, turn_index, path, actual):
    case = next(c for c in DOCUMENT["cases"] if c["id"] == case_id)
    check = next(c for c in case["turns"][turn_index]["checks"] if c["path"] == path)
    assert actual == check["equals"], (case_id, turn_index, path)


def test_rich_league_oracles_use_independent_populations_and_arithmetic():
    avery = [row for row in SOURCE["rich_league_rows"] if row["player_id"] == 1101]
    historical = [row for row in avery if row["season"] == "2023-24"]
    window = [row for row in avery if "2024-04-01" <= row["game_date"] <= "2024-11-30"]
    current = [row for row in avery if row["season"] == "2024-25"]
    value = "semantic_evidence.rows.0.value"
    games = "semantic_evidence.rows.0.observed_games"
    for index, sample in enumerate((window, historical)):
        expected_check("rich_archive_dates", index, games, len(sample))
        expected_check(
            "rich_archive_dates",
            index,
            value,
            sum(r["pts"] for r in sample) / len(sample),
        )
    assert {r["season"] for r in window} == {"2023-24", "2024-25"}

    league = [r for r in SOURCE["rich_league_rows"] if r["season"] == "2024-25"]
    for index, team in enumerate(("AAA", "CCC")):
        sample = [
            r for r in league if r["player_id"] == 1103 and r["team_abbr"] == team
        ]
        expected_check("rich_traded_player", index, games, len(sample))
        expected_check(
            "rich_traded_player",
            index,
            value,
            sum(r["pts"] for r in sample) / len(sample),
        )
    for index, (metric, result) in enumerate(
        (("oreb", "W"), ("dreb", "L"), ("pf", "L"))
    ):
        sample = [r for r in current if r["wl"] == result]
        expected_check("rich_boxscore_results", index, games, len(sample))
        expected_check(
            "rich_boxscore_results",
            index,
            value,
            sum(r[metric] for r in sample) / len(sample),
        )

    appearances, team_games = defaultdict(list), defaultdict(set)
    for row in league:
        appearances[row["player_id"]].append(row["pts"])
        team_games[row["team_abbr"]].add(row["game_id"])
    assert {team: len(ids) for team, ids in team_games.items()} == dict.fromkeys(
        ("AAA", "BBB", "CCC", "DDD"), 10
    )
    floor = 7  # 70% of 10 team games
    averages = {pid: sum(points) / len(points) for pid, points in appearances.items()}
    eligible = [pid for pid, points in appearances.items() if len(points) >= floor]
    winner = max(eligible, key=averages.__getitem__)
    assert len(appearances[1112]) == 6 and averages[1112] == 40 > averages[winner]
    for path, actual in {
        "semantic_evidence.cohort.min_games": floor,
        "semantic_evidence.cohort.size": len(eligible),
        "semantic_evidence.cohort.excluded_players": len(appearances) - len(eligible),
        "semantic_evidence.rows.0.player_id": winner,
        games: len(appearances[winner]),
        value: averages[winner],
    }.items():
        expected_check("rich_qualified_leader", 0, path, actual)

    totals = {
        k: sum(r[k] for r in current)
        for k in ("fg3m", "fg3a", "ftm", "fta", "pts", "fga", "min")
    }
    assert {row["min"] for row in current} == set(range(28, 38))
    components = [
        (totals["fg3m"], totals["fg3a"]),
        (totals["ftm"], totals["fta"]),
        (totals["pts"], 2 * (totals["fga"] + 0.44 * totals["fta"])),
        (36 * totals["pts"], totals["min"]),
    ]
    for index, (numerator, denominator) in enumerate(components):
        expected_check(
            "rich_efficiency", index, "semantic_evidence.rows.0.numerator", numerator
        )
        expected_check(
            "rich_efficiency",
            index,
            "semantic_evidence.rows.0.denominator",
            denominator,
        )
        expected_check("rich_efficiency", index, value, numerator / denominator)


def test_rich_availability_oracle_uses_report_provenance_and_final_minutes(tmp_path):
    attachment = SOURCE["rich_availability"]
    assert {game["home_away"] for game in attachment["games"]} == {"home", "away"}
    assert {row["home_away"] for row in attachment["stats"]["rows"]} == {"HOME", "AWAY"}
    path = fixture_artifacts(SOURCE, tmp_path, ["rich_availability"])[
        "rich_availability"
    ]
    load_availability(path)  # Validates the independently authored joins and digests.
    focal, teammate = (
        {
            row["game_id"]: row
            for row in attachment["stats"]["rows"]
            if row["player_id"] == pid
        }
        for pid in (1101, 1102)
    )
    schedule = list(focal)
    usual, limited, dnp, out, unknown = schedule[:6], *schedule[6:]
    prior_median = median(teammate[game]["min"] for game in usual)
    assert len(usual) == 6 and teammate[limited]["min"] < prior_median * 0.5
    assert set((dnp, out, unknown)).isdisjoint(teammate)
    assert attachment["participation"][0]["game_id"] == dnp
    assert attachment["participation"][0]["minutes"] == 0
    conflict, report_only = attachment["reports"]
    assert teammate[usual[2]]["min"] == 32
    assert (conflict["game_date"], report_only["game_date"]) == (
        focal[usual[2]]["game_date"],
        focal[out]["game_date"],
    )
    for report in (conflict, report_only):
        assert report["injury_status"] == "Out"
        assert report["source_url"] and report["ingested_at_utc"]
        assert report["report_timestamp_utc"] < report["game_date"] + "T20:00:00Z"
    for index in range(3):
        include = index == 1
        both = [*usual, limited] if include else usual
        location = "groups.both_played.6" if include else "excluded_games.0"
        for path, actual in {
            "policy.include_limited_minutes": include,
            "metrics.0.groups.both_played.value": sum(
                focal[game]["pts"] for game in both
            )
            / len(both),
            "metrics.0.groups.did_not_play.value": (
                focal[dnp]["pts"] + focal[out]["pts"]
            )
            / 2,
            f"{location}.minutes_checks.1.baseline_minutes": prior_median,
            f"{location}.minutes_checks.1.cutoff_minutes": prior_median * 0.5,
            f"excluded_games.{0 if include else 1}.game_id": unknown,
        }.items():
            expected_check(
                "rich_availability_recovery",
                index,
                "availability_evidence." + path,
                actual,
            )
