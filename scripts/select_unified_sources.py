"""Read a retained observation snapshot and append decisions to a separate ledger."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.data.unified.identity import canonical, require  # noqa: E402
from app.data.unified.ledger import LedgerStore  # noqa: E402
from app.data.unified.selection import (  # noqa: E402
    pending_check,
    policy_record,
    select,
)
from app.data.unified.snapshot import snapshot  # noqa: E402


def run(source, ledger_path, output, scopes, selection_as_of):
    source, ledger_path, output = (
        Path(p).resolve() for p in (source, ledger_path, output)
    )
    require(
        ledger_path.is_relative_to(ROOT / "reports")
        and output.is_relative_to(ROOT / "reports"),
        "Offline ledger and output must stay in private reports/",
    )
    require(
        ledger_path.suffix == ".sqlite"
        and ledger_path != source
        and not output.exists(),
        "Use a separate ledger and new output directory",
    )
    require(
        not source.is_relative_to(output) and not ledger_path.is_relative_to(output),
        "Output must not contain source or ledger",
    )
    # Validate everything before creating the destination. No writer connection
    # ever touches the source SQLite file, even when validation fails.
    inventory = snapshot(source)
    policy = policy_record()
    decision = select(inventory, policy, scopes, selection_as_of)
    pending = [
        pending_check(
            decision, family, s["scope"], "Evaluator not implemented in this increment"
        )
        for s in decision["payload"]["scopes"]
        for family in ("reconciliation", "coverage", "pdf")
    ]
    ledger_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    output.mkdir(parents=True, mode=0o700)
    store = LedgerStore(ledger_path)
    records = [policy, inventory, decision, *pending]
    try:
        with store.atomic():
            statuses = [store.append(value) for value in records]
        # Read back dependency-bound records before emitting diagnostic exports.
        for value in records:
            require(store.read(value["record_id"]) == value, "Ledger readback differs")
        (output / "records.jsonl").write_text(
            "".join(canonical(value) + "\n" for value in records)
        )
        (output / "selection.json").write_text(json.dumps(decision, indent=2) + "\n")
        report = {
            "mode": "offline_source_selection",
            "status": decision["payload"]["status"],
            "selection_id": decision["record_id"],
            "inventory_id": inventory["record_id"],
            "recorded": statuses.count("recorded"),
            "noops": statuses.count("noop"),
            "scope_counts": [
                {
                    "season": s["scope"]["season"],
                    "provider": s["provider"],
                    "games": s["observed_games"],
                    "appearances": s["observed_appearances"],
                    "status": s["status"],
                    "reasons": s["reasons"],
                }
                for s in decision["payload"]["scopes"]
            ],
            "runtime_eligible": False,
            "coverage_evaluation": "not_evaluated",
        }
        (output / "result.json").write_text(json.dumps(report, indent=2) + "\n")
        return report
    finally:
        store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--selection-as-of", required=True)
    parser.add_argument(
        "--scope",
        nargs=3,
        action="append",
        required=True,
        metavar=("SEASON", "FIRST", "LAST"),
    )
    args = parser.parse_args()
    scopes = [
        {"season": season, "phase": "Regular Season", "first": first, "last": last}
        for season, first, last in args.scope
    ]
    report = run(args.source, args.ledger, args.output, scopes, args.selection_as_of)
    print(json.dumps(report))
    return 0 if report["status"] == "selected_observed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
