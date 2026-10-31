"""Re-execute retained PDFs into a new local ledger; never fetch or publish."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.data.unified import artifacts as pdf_evidence  # noqa: E402
from app.data.unified.identity import canonical, require  # noqa: E402
from app.data.unified.ledger import LedgerStore, record  # noqa: E402
from app.data.unified.pdf_checks import (  # noqa: E402
    evaluate_pdf,
    pbp_text,
    pdf_bytes,
    pdf_capability,
)
from app.data.unified.snapshot import mapping_snapshot  # noqa: E402
from app.data.unified.store import parse_json  # noqa: E402


def retain_artifacts(data, directory):
    # Validate even an empty bundle before any potential filesystem write.
    directory = Path(directory)
    require(
        not directory.is_symlink()
        and directory.resolve().is_relative_to(pdf_evidence.REPORTS.resolve()),
        "Evidence directory must resolve inside private reports",
    )

    def save(body, receipt, suffix):
        return pdf_evidence.retain(body, receipt, directory, suffix)

    for pbp in data["pbp_sources"]:
        body = pbp_text(pbp).encode()
        pbp["source_path"] = save(body, pbp["receipt"], ".body")
        pbp["source_body"] = None
    for attempt in data["attempts"]:
        if attempt["acquisition"] == "captured":
            body = pdf_bytes(attempt)
            attempt["pdf_path"] = save(body, attempt["receipt"], ".pdf")
            attempt["pdf_base64"] = None
    return data


def run(source, source_ledger, coverage_id, bundle, ledger_path, output):
    source, source_ledger, bundle, ledger_path, output = (
        Path(p).resolve() for p in (source, source_ledger, bundle, ledger_path, output)
    )
    require(
        ledger_path.is_relative_to(ROOT / "reports")
        and output.is_relative_to(ROOT / "reports")
        and ledger_path.suffix == ".sqlite"
        and ledger_path not in (source, source_ledger, bundle)
        and not output.exists(),
        "Use separate private PDF ledger and new output",
    )
    require(
        not any(
            p.is_relative_to(output)
            for p in (source, source_ledger, bundle, ledger_path)
        ),
        "PDF output must not contain inputs or ledger",
    )
    require(
        source_ledger.is_file()
        and source_ledger.stat().st_size <= 1024**3
        and not Path(str(source_ledger) + "-wal").exists(),
        "Retain a checkpointed input ledger",
    )
    require(
        bundle.is_file() and bundle.stat().st_size <= 16 * 1024 * 1024,
        "Invalid PDF bundle size",
    )
    data = parse_json(bundle.read_bytes())
    require(
        set(data) == {"schema_version", "attempts", "pbp_sources"}
        and data["schema_version"] == "offline_pdf_bundle/1",
        "PDF bundle fields/version differ",
    )
    with source_ledger.open("rb") as stream:
        original_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    ledger_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = retain_artifacts(data, ledger_path.parent / "evidence")
    if not ledger_path.exists():
        source_db = sqlite3.connect(source_ledger.as_uri() + "?mode=ro", uri=True)
        dest = sqlite3.connect(ledger_path)
        try:
            source_db.backup(dest)
        finally:
            dest.close()
            source_db.close()
    store = LedgerStore(ledger_path)
    try:
        covered = store.read(coverage_id)
        require(covered["kind"] == "coverage", "PDF checks require a coverage record")
        recs = [store.read(k) for k in covered["payload"]["reconciliation_ids"]]
        selection = store.read(covered["payload"]["selection_id"])
        inventory = store.read(selection["payload"]["inventory_id"])
        mappings = mapping_snapshot(source, inventory)
        ids = {oid for s in selection["payload"]["scopes"] for oid in s["selected"]}
        require(
            all(a["observation_id"] in ids for a in data["attempts"])
            and all(p["observation_id"] in ids for p in data["pbp_sources"]),
            "PDF bundle contains unselected observations",
        )
        records, checks = [mappings], []
        for rec in recs:
            inputs = store.read(rec["payload"]["inputs_id"])
            selected_ids = set(rec["payload"]["selected_observations"])
            pdf_inputs = record(
                "pdf_inputs",
                {
                    "version": "scoped_pdf_inputs/1",
                    "selection_id": selection["record_id"],
                    "evaluation_inputs_id": inputs["record_id"],
                    "mapping_catalog_id": mappings["record_id"],
                    "attempts": [
                        a
                        for a in data["attempts"]
                        if a["observation_id"] in selected_ids
                    ],
                    "pbp_sources": [
                        p
                        for p in data["pbp_sources"]
                        if p["observation_id"] in selected_ids
                    ],
                },
                [selection["record_id"], inputs["record_id"], mappings["record_id"]],
            )
            check = evaluate_pdf(pdf_inputs, mappings, selection, inputs)
            records.extend([pdf_inputs, check])
            checks.append(check)
        capability = pdf_capability(covered, checks)
        records.append(capability)
        with store.atomic():
            statuses = [store.append(v) for v in records]
        require(
            store.read(capability["record_id"]) == capability,
            "PDF capability readback differs",
        )
        output.mkdir(parents=True, mode=0o700)
        (output / "pdf-checks.json").write_text(json.dumps(checks, indent=2) + "\n")
        (output / "pdf-capability.json").write_text(
            json.dumps(capability, indent=2) + "\n"
        )
        (output / "records.jsonl").write_text(
            "".join(canonical(v) + "\n" for v in records)
        )
        report = {
            "mode": "offline_pdf_reexecution",
            "pdf_capability_id": capability["record_id"],
            "recorded": statuses.count("recorded"),
            "noops": statuses.count("noop"),
            "games": sum(len(c["payload"]["games"]) for c in checks),
            "applicable_matches": sum(
                g["supported_fields_matched"]
                for c in checks
                for g in c["payload"]["games"]
            ),
            "outcomes": {
                state: sum(
                    c["outcome"] == state
                    for check in checks
                    for c in check["payload"]["checks"]
                )
                for state in (
                    "matched",
                    "mismatch",
                    "unsupported",
                    "unavailable",
                    "stale",
                    "not_attempted",
                    "not_yet_due",
                )
            },
            "runtime_eligible": False,
        }
        (output / "result.json").write_text(json.dumps(report, indent=2) + "\n")
        return report
    finally:
        store.close()
        with source_ledger.open("rb") as stream:
            require(
                hashlib.file_digest(stream, "sha256").hexdigest() == original_hash,
                "Original coverage ledger changed",
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--source-ledger", type=Path, required=True)
    parser.add_argument("--coverage-id", required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            run(
                args.source,
                args.source_ledger,
                args.coverage_id,
                args.bundle,
                args.ledger,
                args.output,
            )
        )
    )


if __name__ == "__main__":
    main()
