"""Supplementary score denominators must survive adversarial answer mutations."""

import copy

import httpx
import pytest
from scripts.ask_eval.grading import grade
from scripts.ask_eval.summary import summarize
from scripts.evaluate_ask import evaluate


def run(checks, payload, *, sse=None, expected_failure=None):
    turn = {"checks": checks}
    if expected_failure:
        turn["expected_failure"] = expected_failure
    cases = [{"id": "sample", "turns": [turn]}]
    modes = {}
    for mode, value in (("json", payload), ("sse", payload if sse is None else sse)):
        assertions = grade(value, checks)
        modes[mode] = [
            {
                "payload": value,
                "checks": assertions,
                "passed": all(c["passed"] for c in assertions),
            }
        ]
    return summarize(cases, [{"id": "sample", "modes": modes}])


def test_answer_and_refusal_denominators_count_logical_turns_once():
    answer = run([{"path": "status", "equals": "ok"}], {"status": "ok"})
    refusal = run(
        [
            {"path": "status", "equals": "unsupported_scope"},
            {"path": "tables", "equals": []},
        ],
        {"status": "unsupported_scope", "tables": []},
    )
    assert answer["total_turns"] == refusal["total_turns"] == 1
    assert answer["refusal_diagnostics"]["expected_success_turns"] == 1
    assert answer["refusal_diagnostics"]["expected_refusal_turns"] == 0
    assert refusal["refusal_diagnostics"]["contract_correct_refusals"] == 1
    assert refusal["dimensions"]["numeric_units"] == {
        "assessed": 0,
        "passed": 0,
        "failed": 0,
        "unassessed": 1,
    }


def test_wrong_refusal_does_not_receive_unrelated_numeric_credit_as_success():
    checks = [
        {"path": "status", "equals": "ok"},
        {"path": "semantic_evidence.rows.0.value", "equals": 5},
    ]
    result = run(
        checks,
        {"status": "unsupported_scope", "semantic_evidence": {"rows": [{"value": 5}]}},
    )
    assert result["outcome_groups"]["success"] == {"total": 1, "passed": 0}
    assert result["refusal_diagnostics"]["incorrect_refusals_any_transport"] == 1
    assert result["dimensions"]["numeric_units"]["passed"] == 1
    assert result["dimensions"]["outcome"]["failed"] == 1


@pytest.mark.parametrize("status", ["invented_status", None, 7])
def test_invalid_actual_status_is_unknown_despite_other_passing_checks(status):
    result = run(
        [{"path": "status", "equals": "ok"}, {"path": "answer", "equals": "unchanged"}],
        {"status": status, "answer": "unchanged"},
    )
    assert result["turns"][0]["actual_outcome"] == "unknown"
    assert result["refusal_diagnostics"]["expected_success_unknown_any_transport"] == 1
    assert result["refusal_diagnostics"]["incorrect_refusals_any_transport"] == 0
    assert result["outcome_groups"]["success"]["passed"] == 0


@pytest.mark.parametrize("payload", [None, 7, "response", [], ["status"]])
def test_nonobject_payload_remains_inspectable_as_unknown(payload):
    result = run([{"path": "status", "equals": "ok"}], payload)
    assert result["turns"][0]["actual_outcome"] == "unknown"
    assert result["dimensions"]["outcome"]["failed"] == 1


def test_success_refusal_transport_mix_fails_one_turn_and_exposes_both_states():
    result = run(
        [{"path": "status", "equals": "ok"}],
        {"status": "ok"},
        sse={"status": "unsupported_scope"},
    )
    assert result["total_turns"] == 1
    assert result["turns"][0]["actual_outcomes"] == {
        "json": "success",
        "sse": "refusal",
    }
    assert result["turns"][0]["actual_outcome"] == "mixed"
    assert result["refusal_diagnostics"]["incorrect_refusals_any_transport"] == 1
    assert result["refusal_diagnostics"]["expected_success_mixed_transports"] == 1
    assert result["dimensions"]["outcome"]["failed"] == 1


def test_missing_outcome_expectation_never_uses_observed_success_to_set_denominator():
    result = run(
        [{"path": "answer", "equals": "text"}], {"status": "ok", "answer": "text"}
    )
    assert result["outcome_groups"]["unassessed"] == {"total": 1, "passed": None}
    assert result["outcome_groups"]["success"]["total"] == 0
    assert result["dimensions"]["outcome"]["unassessed"] == 1
    assert result["uncategorized_assertions"] == {"answer": 1}


@pytest.mark.parametrize(
    ("checks", "payload"),
    [
        (
            [{"path": "status", "equals": "invalid_context"}],
            {"status": "invalid_context"},
        ),
        (
            [
                {"path": "status", "equals": "ok"},
                {"path": "research_status", "equals": "unsupported"},
            ],
            {"status": "ok", "research_status": "unsupported"},
        ),
        ([{"path": "detail", "equals": "example"}], {"detail": "example"}),
    ],
)
def test_unassessed_outcome_expectation_cannot_receive_dimension_credit(
    checks, payload
):
    result = run(checks, payload)
    assert result["outcome_groups"]["unassessed"] == {"total": 1, "passed": None}
    assert result["turns"][0]["dimensions"]["outcome"] is None
    assert result["dimensions"]["outcome"] == {
        "assessed": 0,
        "passed": 0,
        "failed": 0,
        "unassessed": 1,
    }


@pytest.mark.parametrize(
    ("json_status", "sse_status"), [(502, 200), (200, 502), (200, 200), (502, 502)]
)
def test_error_outcome_requires_real_evaluator_terminal_contract(
    monkeypatch, json_status, sse_status
):
    detail = "sanitized failure"
    document = {
        "cases": [
            {
                "id": "terminal_error",
                "family": "Guards",
                "turns": [
                    {
                        "question": "Question",
                        "checks": [{"path": "detail", "equals": detail}],
                        "expected_failure": {"detail": detail, "http_status": 502},
                    }
                ],
            }
        ]
    }

    def response(request):
        if request.url.path.endswith("/stream"):
            if sse_status == 200:
                return httpx.Response(
                    200, text='data: {"type":"error","detail":"sanitized failure"}\n\n'
                )
            return httpx.Response(sse_status, json={"detail": detail})
        return httpx.Response(json_status, json={"detail": detail})

    client = httpx.Client
    monkeypatch.setattr(
        "scripts.evaluate_ask.httpx.Client",
        lambda **kwargs: client(**kwargs, transport=httpx.MockTransport(response)),
    )
    records = evaluate("http://fixture", document)
    terminal_failures = [
        check
        for mode in ("json", "sse")
        for check in records[0]["modes"][mode][0]["checks"]
        if check["path"] == "HTTP/terminal"
    ]
    valid = json_status == 502 and sse_status == 200
    assert bool(terminal_failures) is not valid
    assert records[0]["passed"] is valid
    result = summarize(document["cases"], records)
    assert result["turns"][0]["actual_outcomes"] == {
        "json": "error" if json_status == 502 else "unknown",
        "sse": "error" if sse_status == 200 else "unknown",
    }
    assert result["turns"][0]["outcome_matches"] is valid
    assert result["turns"][0]["dimensions"]["outcome"] is valid
    assert result["outcome_groups"]["error"]["passed"] == int(valid)


def test_duplicate_path_boolean_expectation_cannot_reuse_passing_numeric_check():
    result = run(
        [
            {"path": "semantic_evidence.rows.0.value", "equals": 1},
            {"path": "semantic_evidence.rows.0.value", "equals": True},
        ],
        {"semantic_evidence": {"rows": [{"value": 1}]}},
    )
    assert result["dimensions"]["numeric_units"]["failed"] == 1


def test_successful_null_is_answer_and_zero_mutation_fails_numeric_dimension():
    checks = [
        {"path": "status", "equals": "ok"},
        {"path": "semantic_evidence.rows.0.value", "equals": None},
        {"path": "tables.0.rows.0.1", "equals": "Unavailable"},
    ]
    payload = {
        "status": "ok",
        "semantic_evidence": {"rows": [{"value": None}]},
        "tables": [{"rows": [["FT%", "Unavailable"]]}],
    }
    correct = run(checks, payload)
    assert correct["outcome_groups"]["success"]["passed"] == 1
    assert correct["refusal_diagnostics"]["expected_refusal_turns"] == 0
    payload["semantic_evidence"]["rows"][0]["value"] = 0
    assert run(checks, payload)["dimensions"]["numeric_units"]["failed"] == 1


def test_scope_route_value_and_units_mutations_fail_only_assessed_dimensions():
    checks = [
        {"path": "status", "equals": "ok"},
        {"path": "semantic_evidence.scope.season", "equals": "2024-25"},
        {"path": "agent_plan.route", "equals": "league_baseline"},
        {"path": "semantic_evidence.difference", "equals": 3},
        {"path": "semantic_evidence.difference_unit", "equals": "percentage points"},
    ]
    payload = {
        "status": "ok",
        "agent_plan": {"route": "league_baseline"},
        "semantic_evidence": {
            "scope": {"season": "2024-25"},
            "difference": 3,
            "difference_unit": "percentage points",
        },
    }
    mutations = [
        (
            "identity_scope",
            lambda p: p["semantic_evidence"]["scope"].update(season="2025-26"),
        ),
        ("route", lambda p: p["agent_plan"].update(route="similarity")),
        ("numeric_units", lambda p: p["semantic_evidence"].update(difference=4)),
        (
            "numeric_units",
            lambda p: p["semantic_evidence"].update(difference_unit="ratio"),
        ),
    ]
    for dimension, mutate in mutations:
        changed = copy.deepcopy(payload)
        mutate(changed)
        result = run(checks, changed)
        assert [
            name for name, counts in result["dimensions"].items() if counts["failed"]
        ] == [dimension]


def test_refusal_with_substitute_table_is_not_contract_correct():
    result = run(
        [
            {"path": "status", "equals": "unsupported_scope"},
            {"path": "tables", "equals": []},
        ],
        {"status": "unsupported_scope", "tables": [{"rows": [[99]]}]},
    )
    assert result["refusal_diagnostics"]["contract_correct_refusals"] == 0


def test_missing_transport_cannot_pass_an_assessed_dimension():
    cases = [
        {"id": "sample", "turns": [{"checks": [{"path": "status", "equals": "ok"}]}]}
    ]
    result = summarize(cases, [])
    assert result["dimensions"]["outcome"]["failed"] == 1
    assert not result["turns"][0]["contract_passed"]


@pytest.mark.parametrize(
    ("checks", "payload", "group", "failure"),
    [
        (
            [{"path": "status", "equals": "clarification_required"}],
            {"status": "clarification_required"},
            "clarification",
            None,
        ),
        (
            [{"path": "study_status", "equals": "answered"}],
            {"study_status": "answered"},
            "success",
            None,
        ),
        (
            [{"path": "research_status", "equals": "unsupported"}],
            {"research_status": "unsupported"},
            "refusal",
            None,
        ),
        (
            [{"path": "detail", "equals": "sanitized failure"}],
            {"detail": "sanitized failure"},
            "error",
            {"detail": "sanitized failure", "http_status": 502},
        ),
    ],
)
def test_explicit_alternate_outcomes(checks, payload, group, failure):
    result = run(checks, payload, expected_failure=failure)
    assert result["outcome_groups"][group] == {"total": 1, "passed": 1}
