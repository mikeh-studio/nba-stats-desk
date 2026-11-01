"""Offline scope reconciliation and coverage over a pinned selection; no fetchers."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.data.unified.artifacts import retain, source_text  # noqa: E402
from app.data.unified.evaluation import (  # noqa: E402
    coverage,
    expected_inventory,
    metric_catalog,
    reconciliation,
)
from app.data.unified.identity import canonical, require  # noqa: E402
from app.data.unified.ledger import LedgerStore, validate  # noqa: E402
from app.data.unified.selection import policy_record, select  # noqa: E402
from app.data.unified.snapshot import evaluation_snapshot, snapshot  # noqa: E402
from app.data.unified.store import parse_json  # noqa: E402


def run(
    source,
    selection_path,
    ledger_path,
    output,
    requests,
    authority_path=None,
    disputes_path=None,
    revalidations_path=None,
):
    source, selection_path, ledger_path, output = (
        Path(p).resolve() for p in (source, selection_path, ledger_path, output)
    )
    require(
        ledger_path.is_relative_to(ROOT / "reports")
        and output.is_relative_to(ROOT / "reports")
        and ledger_path.suffix == ".sqlite"
        and ledger_path not in (source, selection_path)
        and not output.exists(),
        "Use a separate ledger and new private reports output",
    )
    require(
        not ledger_path.is_relative_to(output)
        and not source.is_relative_to(output)
        and not selection_path.is_relative_to(output),
        "Output must not contain inputs or ledger",
    )
    require(
        selection_path.stat().st_size <= 4 * 1024 * 1024, "Selection file exceeds bound"
    )
    decision = parse_json(selection_path.read_bytes())
    validate(decision)
    inventory, policy, catalog = snapshot(source), policy_record(), metric_catalog()
    require(
        decision
        == select(
            inventory,
            policy,
            [s["scope"] for s in decision["payload"]["scopes"]],
            decision["payload"]["selection_as_of"],
        ),
        "Source/selection pin changed; rebuild explicit selection",
    )
    authorities: list[dict[str, Any]] = []
    disputes: list[dict[str, Any]] = []
    revalidations: list[dict[str, Any]] = []
    for path, target in (
        (authority_path, authorities),
        (disputes_path, disputes),
        (revalidations_path, revalidations),
    ):
        if path is not None:
            path = Path(path).resolve()
            require(
                path.is_file()
                and path.stat().st_size <= 8 * 1024 * 1024
                and path != ledger_path
                and not path.is_relative_to(output),
                "Invalid evidence file path/size",
            )
            value = parse_json(path.read_bytes())
            require(
                isinstance(value, list), "Evidence file must contain an explicit list"
            )
            target.extend(value)
    for check in revalidations:
        check["source_path"] = retain(
            source_text(check).encode(),
            check["receipt"],
            ledger_path.parent / "evidence",
            ".body",
        )
        check["source_body"] = None
    require(
        len(authorities) <= 2
        and len({canonical(a["scope"]) for a in authorities}) == len(authorities),
        "Duplicate/oversized authority scopes",
    )
    scopes = [s["scope"] for s in decision["payload"]["scopes"]]
    require(
        all(a["scope"] in scopes for a in authorities),
        "Authority evidence outside selection",
    )
    roots, reconciliations = [], []
    for requested in scopes:
        authority = expected_inventory(
            requested, next((a for a in authorities if a["scope"] == requested), None)
        )
        # Bind disputes to the selected observation, never to a guessed date or ID.
        ids = {
            r["observation_id"]
            for r in inventory["payload"]["observations"]
            if r["scope"]["season"] == requested["season"]
            and r["scope"]["phase"] == requested["phase"]
            and requested["first"] <= r["scope"]["date"] <= requested["last"]
        }
        inputs = evaluation_snapshot(
            source,
            inventory,
            requested,
            [d for d in disputes if d["observation_id"] in ids],
            [c for c in revalidations if c["observation_id"] in ids],
        )
        roots.extend([authority, inputs])
        reconciliations.append(reconciliation(decision, inputs, authority))
    require(
        all(
            any(
                d["observation_id"]
                in {o["observation_id"] for o in value["payload"]["observations"]}
                for value in roots
                if value["kind"] == "evaluation_inputs"
            )
            for d in disputes
        ),
        "Dispute outside pinned input observations",
    )
    result = coverage(reconciliations, catalog, requests)
    require(
        all(
            any(
                c["observation_id"]
                in {o["observation_id"] for o in value["payload"]["observations"]}
                for value in roots
                if value["kind"] == "evaluation_inputs"
            )
            for c in revalidations
        ),
        "Correction revalidation outside pinned scope",
    )
    records = [policy, inventory, decision, catalog, *roots, *reconciliations, result]
    ledger_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    output.mkdir(parents=True, mode=0o700)
    store = LedgerStore(ledger_path)
    try:
        with store.atomic():
            statuses = [store.append(value) for value in records]
        require(store.read(result["record_id"]) == result, "Coverage readback differs")
        (output / "coverage.json").write_text(json.dumps(result, indent=2) + "\n")
        (output / "reconciliation.json").write_text(
            json.dumps(reconciliations, indent=2) + "\n"
        )
        report = {
            "mode": "offline_coverage_evaluation",
            "coverage_id": result["record_id"],
            "recorded": statuses.count("recorded"),
            "noops": statuses.count("noop"),
            "metrics": [
                {
                    k: m[k]
                    for k in (
                        "request",
                        "level",
                        "qualified_population",
                        "league_rank_eligible",
                        "percentile_eligible",
                        "reasons",
                    )
                }
                for m in result["payload"]["metrics"]
            ],
            "runtime_eligible": False,
            "pdf_corroborated": False,
        }
        (output / "result.json").write_text(json.dumps(report, indent=2) + "\n")
        return report
    finally:
        store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--authority", type=Path)
    parser.add_argument("--disputes", type=Path)
    parser.add_argument("--revalidations", type=Path)
    parser.add_argument("--metric", action="append", required=True)
    parser.add_argument("--min-games", type=int)
    parser.add_argument("--min-attempts", type=float)
    parser.add_argument("--min-minutes", type=float)
    args = parser.parse_args()
    defs = metric_catalog()["payload"]["metrics"]
    require(all(k in defs for k in args.metric), "Unsupported requested metric")
    requests = [
        {
            "metric": k,
            "aggregation": "total"
            if k == "gp"
            else "ratio"
            if defs[k]["denominator"]
            else "average",
            "min_games": args.min_games,
            "min_attempts": args.min_attempts,
            "min_minutes": args.min_minutes,
        }
        for k in args.metric
    ]
    report = run(
        args.source,
        args.selection,
        args.ledger,
        args.output,
        requests,
        args.authority,
        args.disputes,
        args.revalidations,
    )
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
