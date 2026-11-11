"""Describe asserted endpoint contracts without inventing missing expectations.

Counts use logical case-turns, with both transports required. These dimensions
cover only authored assertions; route assertions do not establish full-plan
correctness. This module does not change grading or human review decisions.
"""

from collections import Counter

OUTCOMES = {
    "status": {
        "ok": "success",
        "clarification_required": "clarification",
        **dict.fromkeys(
            (
                "unsupported_scope",
                "unsupported_coverage",
                "insufficient_sample",
                "not_applicable",
                "invalid_evidence",
                "duplicate_grain",
            ),
            "refusal",
        ),
    },
    "research_status": {"unsupported": "refusal"},
    "study_status": {"answered": "success", "unavailable": "refusal"},
}
DIMENSIONS = ("identity_scope", "route", "numeric_units", "outcome")
MODES = ("json", "sse")


def dimension(check):
    """Conservative explicit path vocabulary; other assertions stay visible."""
    path = check["path"]
    parts = path.split(".")
    leaf = parts[-1]
    if path in OUTCOMES or path == "detail":
        return "outcome"
    if path in {"agent_plan.route", "ask_route.route"}:
        return "route"
    if any(
        p
        in {
            "scope",
            "baseline_scope",
            "availability_scope",
            "research_scope",
            "evidence_scope",
        }
        for p in parts
    ) or leaf in {
        "player_id",
        "player_ids",
        "player_name",
        "teammate_id",
        "focal_player_id",
        "game_id",
        "season",
    }:
        return "identity_scope"
    if (
        leaf
        in {
            "value",
            "difference",
            "difference_unit",
            "unit",
            "valid_games",
            "observed_games",
            "similarity_score",
            "percentile",
            "delta",
        }
        or "values" in parts
        or (path.startswith("charts.") and leaf == "y")
    ):
        return "numeric_units"
    # Display cells are included only when their oracle explicitly names a
    # numeric representation or unavailable value, not arbitrary table prose.
    expected = check["equals"]
    if (
        path.startswith("tables.")
        and ".rows." in path
        and isinstance(expected, str)
        and (expected == "Unavailable" or expected[:1] in "0123456789-−")
        and expected
    ):
        return "numeric_units"
    if path.endswith("metric.key") or leaf == "unit":
        return "numeric_units"
    return None


def expected_outcome(turn):
    declared = [
        OUTCOMES[c["path"]].get(c["equals"], "unassessed")
        for c in turn["checks"]
        if c["path"] in OUTCOMES and isinstance(c["equals"], str)
    ]
    failure = turn.get("expected_failure")
    if failure and any(
        c["path"] == "detail" and c["equals"] == failure["detail"]
        for c in turn["checks"]
    ):
        declared.append("error")
    return declared[0] if declared and len(set(declared)) == 1 else "unassessed"


def actual_outcome(payload, turn, observed):
    if not isinstance(payload, dict):
        return "unknown"
    declared = [
        mapping.get(payload[path], "unknown")
        if isinstance(payload[path], str)
        else "unknown"
        for path, mapping in OUTCOMES.items()
        if path in payload
    ]
    failure = turn.get("expected_failure")
    if (
        failure
        and payload == {"detail": failure["detail"]}
        and not any(
            check.get("path") == "HTTP/terminal" and check.get("passed") is not True
            for check in observed.get("checks", [])
        )
    ):
        declared.append("error")
    return declared[0] if declared and len(set(declared)) == 1 else "unknown"


def check_passed(check, observed):
    """Use the original grader's result for the exact authored assertion."""
    return any(
        result.get("path") == check["path"]
        and type(result.get("equals")) is type(check["equals"])
        and result.get("equals") == check["equals"]
        and result.get("absolute_tolerance", 0) == check.get("absolute_tolerance", 0)
        and result.get("passed") is True
        for result in observed.get("checks", [])
    )


def summarize(cases, results):
    """Return a supplementary report; never alter the runner's pass decision."""
    by_id = {result["id"]: result for result in results}
    turns = []
    uncategorized = Counter()
    for case in cases:
        result = by_id.get(case["id"], {})
        for index, turn in enumerate(case["turns"]):
            observed = {}
            for mode in MODES:
                mode_turns = result.get("modes", {}).get(mode, [])
                observed[mode] = mode_turns[index] if index < len(mode_turns) else {}
            expected = expected_outcome(turn)
            actual = {
                mode: actual_outcome(item.get("payload", {}), turn, item)
                for mode, item in observed.items()
            }
            actual_group = (
                actual["json"] if actual["json"] == actual["sse"] else "mixed"
            )
            scored = {}
            for name in DIMENSIONS:
                checks = [check for check in turn["checks"] if dimension(check) == name]
                scored[name] = (
                    all(
                        check_passed(check, item)
                        for check in checks
                        for item in observed.values()
                    )
                    if checks
                    else None
                )
            # An unrelated passing assertion cannot validate an absent/invalid
            # outcome, including when a grader record was incomplete.
            outcome_matches = expected != "unassessed" and actual_group == expected
            if expected != "unassessed":
                scored["outcome"] = scored["outcome"] is True and outcome_matches
            else:
                scored["outcome"] = None
            contract_passed = all(
                item.get("passed") is True for item in observed.values()
            )
            uncategorized.update(
                check["path"] for check in turn["checks"] if dimension(check) is None
            )
            turns.append(
                {
                    "case_id": case["id"],
                    "turn": index + 1,
                    "expected_outcome": expected,
                    "actual_outcomes": actual,
                    "actual_outcome": actual_group,
                    "contract_passed": contract_passed,
                    "outcome_matches": outcome_matches,
                    "dimensions": scored,
                }
            )

    groups = {}
    for name in ("success", "refusal", "clarification", "error", "unassessed"):
        selected = [turn for turn in turns if turn["expected_outcome"] == name]
        passed = sum(
            turn["contract_passed"] and turn["outcome_matches"] for turn in selected
        )
        groups[name] = {
            "total": len(selected),
            "passed": passed if name != "unassessed" else None,
        }
    dimensions = {}
    for name in DIMENSIONS:
        assessed = [
            turn["dimensions"][name]
            for turn in turns
            if turn["dimensions"][name] is not None
        ]
        dimensions[name] = {
            "assessed": len(assessed),
            "passed": sum(assessed),
            "failed": len(assessed) - sum(assessed),
            "unassessed": len(turns) - len(assessed),
        }
    successful = [turn for turn in turns if turn["expected_outcome"] == "success"]
    return {
        "schema_version": 1,
        "counting_unit": "logical_case_turn_requires_json_and_sse",
        "total_turns": len(turns),
        "outcome_definitions": OUTCOMES,
        "outcome_groups": groups,
        "refusal_diagnostics": {
            "expected_refusal_turns": groups["refusal"]["total"],
            "contract_correct_refusals": groups["refusal"]["passed"],
            "expected_success_turns": len(successful),
            "incorrect_refusals_any_transport": sum(
                "refusal" in turn["actual_outcomes"].values() for turn in successful
            ),
            "expected_success_unknown_any_transport": sum(
                "unknown" in turn["actual_outcomes"].values() for turn in successful
            ),
            "expected_success_mixed_transports": sum(
                turn["actual_outcome"] == "mixed" for turn in successful
            ),
        },
        "dimensions": dimensions,
        "uncategorized_assertions": dict(sorted(uncategorized.items())),
        "turns": turns,
        "limitations": [
            "Only explicit authored assertions are assessed; missing outcome expectations remain unassessed.",
            "Refusal means an explicitly expected withholding, including invalid evidence, insufficient sample and not applicable; it does not always mean an unsupported basketball question.",
            "Route assertions do not establish full-plan correctness; numeric and scope dimensions cover only asserted fields.",
            "Dimension scores use existing grader records; neither synthetic execution nor these scores establish model accuracy or human approval.",
        ],
    }
