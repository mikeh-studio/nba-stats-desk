"""Inspectable assertion inventory, not a claim of reviewed behavioral coverage."""

import argparse
import copy
import json
import math
from collections import Counter
from pathlib import Path

# Turn numbers are one-based. These named assertions establish the precise
# recovery contract; arbitrary assertion presence is insufficient.
RECOVERY_INVARIANTS = {
    "context_season_override": [
        (2, "status", "unsupported_coverage"),
        (2, "tables", []),
    ],
    "overview_pending_player_recovery": [
        (3, "semantic_evidence.player_id", 202),
        (3, "semantic_evidence.scope.start", "2024-07-01"),
        (3, "semantic_evidence.scope.end", "2025-06-30"),
        (3, "semantic_evidence.scope.phases", ["Regular Season"]),
    ],
    "context_restored_baseline": [
        (2, "semantic_evidence.player_id", 101),
        (2, "semantic_evidence.kind", "league_baseline"),
        (2, "semantic_evidence.player.value", 2.5),
    ],
}


# Each selected risk is bound to explicit case/turn/path/value expectations.
# These are editorial contracts, independently chosen rather than copied from
# whatever assertions the input corpus happens to contain.
CRITICAL = {
    "identity": {
        "points_mean": [(1, "semantic_evidence.rows.0.player_id", 101)],
        "ambiguous_identity": [
            (1, "status", "clarification_required"),
            (1, "tables", []),
        ],
        "follow_new_player": [(2, "semantic_evidence.rows.0.player_id", 202)],
    },
    "metric_units": {
        "fg_pooled": [
            (1, "semantic_evidence.metric.key", "fg_pct"),
            (1, "semantic_evidence.rows.0.value", 0.1),
            (1, "tables.0.rows.0.1", "10.0%"),
        ],
        "baseline_ratio": [
            (1, "semantic_evidence.difference_unit", "percentage points")
        ],
        "follow_metric": [(2, "semantic_evidence.metric.key", "ast")],
    },
    "date_phase": {
        "boundary_baseline_one_date": [
            (1, "semantic_evidence.scope.start_date", "2025-04-10"),
            (1, "semantic_evidence.scope.window_end", "2025-04-10"),
        ],
        "playoff_mean": [
            (1, "semantic_evidence.scope.season_type", "Playoffs"),
            (1, "semantic_evidence.rows.0.value", 13),
        ],
        "context_phase_override": [
            (2, "semantic_evidence.scope.season_type", "Playoffs")
        ],
    },
    "null_zero_incomplete": {
        "fg_zero_denominator": [
            (1, "semantic_evidence.rows.0.value", None),
            (1, "tables.0.rows.0.1", "Unavailable"),
        ],
        "null_metric": [
            (1, "semantic_evidence.rows.0.valid_games", 5),
            (1, "semantic_evidence.rows.0.value", 6),
        ],
        "baseline_partial": [
            (1, "semantic_evidence.difference", None),
            (1, "semantic_evidence.player.valid_games", 5),
        ],
        "boundary_baseline_one_date": [(1, "semantic_evidence.player.value", 0)],
    },
    "unsupported": {
        cid: [(1, "status", "unsupported_scope"), (1, "tables", [])]
        for cid in ["unsupported_filter", "round_refusal"]
    },
    "conversation": {
        "follow_new_player": [
            (2, "semantic_evidence.rows.0.player_id", 202),
            (2, "semantic_evidence.rows.0.value", 12.5),
        ],
        "context_clarification_replace": [
            (1, "status", "clarification_required"),
            (2, "agent_plan.route", "league_baseline"),
            (2, "semantic_evidence.player_id", 101),
        ],
        **RECOVERY_INVARIANTS,
    },
    "availability": {
        "availability_points": [
            (1, "availability_scope.player_id", 101),
            (1, "availability_scope.teammate_id", 202),
            (1, "availability_evidence.metrics.0.groups.both_played.value", 2),
            (1, "availability_evidence.metrics.0.groups.did_not_play.value", 6),
            (1, "availability_evidence.excluded.unverified_status_or_membership", 1),
        ],
        "availability_ratio": [
            (1, "availability_evidence.metrics.0.metric", "fg_pct"),
            (1, "availability_evidence.metrics.0.groups.did_not_play.value", 1),
        ],
    },
    "research": {
        "research_home": [
            (1, "research_scope.player_ids", [101]),
            (1, "research_scope.home_away", "home"),
            (1, "research.players.0.metrics.0.result.value", 6),
        ],
        "research_playoffs": [
            (1, "research_scope.phase", "Playoffs"),
            (1, "research.players.0.metrics.0.result.value", 13),
        ],
        "research_published_study": [
            (1, "study_id", "synthetic-assists-v1"),
            (1, "evidence_scope.metric", "ast"),
            (1, "tables.0.rows.2.1", "+1.50"),
        ],
    },
    "reference": {
        "baseline_points": [
            (1, "semantic_evidence.player.value", 5),
            (1, "semantic_evidence.baseline.value", 35 / 3),
            (1, "semantic_evidence.difference", 5 - 35 / 3),
            (1, "semantic_evidence.baseline.observed_games", 18),
        ],
        "baseline_last_two": [
            (1, "semantic_evidence.baseline_scope.start_date", "2025-04-14"),
            (1, "semantic_evidence.baseline_scope.end_date", "2025-04-15"),
            (1, "semantic_evidence.difference", -4),
        ],
        "similarity": [
            (1, "semantic_evidence.player_id", 101),
            (1, "semantic_evidence.rows.0.player_id", 202),
            (1, "semantic_evidence.rows.0.similarity_score", 0.8),
        ],
    },
}


def strict_equal(actual, expected):
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(
            strict_equal(a, b) for a, b in zip(actual, expected)
        )
    if isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(
            strict_equal(actual[k], expected[k]) for k in expected
        )
    return actual == expected


def invariant_results(inventory, cid, invariants):
    results = []
    for turn, path, expected in invariants:
        maximum = 1e-9

        def matches(check):
            tolerance = check.get("absolute_tolerance", 0)
            return (
                check["turn"] == turn
                and check["path"] == path
                and strict_equal(check["equals"], expected)
                and type(tolerance) in (int, float)
                and math.isfinite(tolerance)
                and 0 <= tolerance <= maximum
            )

        results.append(
            {
                "turn": turn,
                "path": path,
                "equals": copy.deepcopy(expected),
                "maximum_absolute_tolerance": maximum,
                "assertion_present": any(
                    matches(c) for c in inventory.get(cid, {}).get("assertions", [])
                ),
            }
        )
    return results


def summarize(source, document):
    cases = document["cases"]
    by_id = {case["id"]: case for case in cases}
    rows = source.get("rows", source.get("snapshot", {}).get("rows", []))
    inventory = {}
    for case in cases:
        checks = [
            dict(turn=i + 1, **copy.deepcopy(check))
            for i, turn in enumerate(case["turns"])
            for check in turn["checks"]
        ]
        inventory[case["id"]] = {
            "family": case["family"],
            "variant_of": case.get("variant_of"),
            "review": copy.deepcopy(case["review"]),
            "assertions": checks,
            "asserted_routes": sorted(
                {
                    c["equals"]
                    for c in checks
                    if c["path"] in {"ask_route.route", "agent_plan.route"}
                    and isinstance(c["equals"], str)
                }
            ),
            "asserted_outcomes": sorted(
                {
                    c["equals"]
                    for c in checks
                    if c["path"] in {"status", "research_status", "study_status"}
                    and isinstance(c["equals"], str)
                }
            ),
            "multi_turn": len(case["turns"]) > 1,
            "restores_client_context": any(
                t.get("client_context") for t in case["turns"]
            ),
        }
    return {
        "schema_version": 1,
        "interpretation": "Assertion presence and manually selected risk tags only; execution and human approval are separate evidence.",
        "counts": {
            "cases": len(cases),
            "distinct": sum(not c.get("variant_of") for c in cases),
            "paraphrases": sum(bool(c.get("variant_of")) for c in cases),
            "turns": sum(len(c["turns"]) for c in cases),
            "source_rows": len(rows),
        },
        "source_kind": source.get("source_kind", "synthetic"),
        "review_states": dict(
            sorted(Counter(c["review"]["status"] for c in cases).items())
        ),
        "declared_human_reviewed": document["human_reviewed"],
        "approval_verification": "Unverified declarations: schema v1 permits optional reviewer identity and does not bind approval to expectation content. No human-reviewed rubric credit is established.",
        "critical_risks": {
            risk: {
                "present": [cid for cid in selections if cid in by_id],
                "absent": [cid for cid in selections if cid not in by_id],
                "invariants": {
                    cid: invariant_results(inventory, cid, values)
                    for cid, values in selections.items()
                },
                "missing_assertions": {
                    cid: [
                        check
                        for check in invariant_results(inventory, cid, values)
                        if not check["assertion_present"]
                    ]
                    for cid, values in selections.items()
                    if not all(
                        check["assertion_present"]
                        for check in invariant_results(inventory, cid, values)
                    )
                },
            }
            for risk, selections in CRITICAL.items()
        },
        "recovery_invariants": {
            cid: invariant_results(inventory, cid, values)
            for cid, values in RECOVERY_INVARIANTS.items()
        },
        "cases": inventory,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "tests/fixtures/ask",
    )
    args = parser.parse_args()
    from scripts.ask_eval.fixtures import load_inputs

    source, document = load_inputs(Path(__file__).resolve().parents[2], args.input_dir)
    print(json.dumps(summarize(source, document), indent=2))


if __name__ == "__main__":
    main()
