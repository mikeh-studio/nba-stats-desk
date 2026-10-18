"""Inspectable assertion inventory, not a claim of reviewed behavioral coverage."""

import argparse
import json
from collections import Counter
from pathlib import Path

# Explicit existing-case selections; no case quota is an accuracy threshold.
CRITICAL = {
    "identity": ["points_mean", "ambiguous_identity", "follow_new_player"],
    "metric_units": ["fg_pooled", "baseline_ratio", "follow_metric"],
    "date_phase": ["date_range", "playoff_mean", "context_phase_override"],
    "null_zero_incomplete": [
        "fg_zero_denominator",
        "null_metric",
        "baseline_partial",
        "boundary_baseline_one_date",
    ],
    "unsupported": ["unsupported_filter", "round_refusal"],
    "conversation": [
        "follow_new_player",
        "context_clarification_replace",
        "context_restored_baseline",
        "context_season_override",
        "overview_pending_player_recovery",
    ],
    "availability": ["availability_points", "availability_ratio"],
    "research": ["research_home", "research_playoffs", "research_published_study"],
    "reference": ["baseline_points", "baseline_last_two", "similarity"],
}


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


def summarize(source, document):
    cases = document["cases"]
    by_id = {case["id"]: case for case in cases}
    rows = source.get("rows", source.get("snapshot", {}).get("rows", []))
    inventory = {}
    for case in cases:
        checks = [
            dict(turn=i + 1, **check)
            for i, turn in enumerate(case["turns"])
            for check in turn["checks"]
        ]
        inventory[case["id"]] = {
            "family": case["family"],
            "variant_of": case.get("variant_of"),
            "review": case["review"],
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
        "critical_risks": {
            risk: {
                "present": [cid for cid in ids if cid in by_id],
                "absent": [cid for cid in ids if cid not in by_id],
            }
            for risk, ids in CRITICAL.items()
        },
        "recovery_invariants": {
            cid: [
                {
                    "turn": turn,
                    "path": path,
                    "equals": expected,
                    "assertion_present": any(
                        c["turn"] == turn
                        and c["path"] == path
                        and c["equals"] == expected
                        for c in inventory.get(cid, {}).get("assertions", [])
                    ),
                }
                for turn, path, expected in invariants
            ]
            for cid, invariants in RECOVERY_INVARIANTS.items()
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
