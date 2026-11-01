"""Bounded append-only offline ledgers, deliberately separate from observations."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from .identity import canonical, digest, require
from .store import parse_json

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts/unified_data"


def validator():
    schema = parse_json((CONTRACTS / "ledger.schema.json").read_bytes())
    return Draft202012Validator(schema, format_checker=FormatChecker())


def record(kind, payload, depends_on=()):
    value = {
        "schema_version": "offline_unified_ledger/1",
        "kind": kind,
        "depends_on": sorted(depends_on),
        "payload": payload,
    }
    value["record_id"] = digest(value)
    validate(value)
    return json.loads(canonical(value))


def validate(value):
    try:
        validator().validate(value)
    except ValidationError:
        # Raw observations and document bytes must never enter error output.
        raise ValidationError("Offline ledger schema validation failed") from None
    content = {k: v for k, v in value.items() if k != "record_id"}
    require(value["record_id"] == digest(content), "Ledger record integrity failed")
    require(
        value["depends_on"] == sorted(set(value["depends_on"])),
        "Duplicate/unsorted ledger dependency",
    )
    if value["kind"] in ("policy", "inventory", "expected_inventory", "metric_catalog"):
        require(
            not value["depends_on"], "Root ledger must not depend on another record"
        )
    elif value["kind"] == "selection":
        p = value["payload"]
        require(
            value["depends_on"] == sorted([p["policy_id"], p["inventory_id"]]),
            "Selection dependency binding differs",
        )
    elif value["kind"] == "pending_check":
        require(
            value["depends_on"] == [value["payload"]["selection_id"]],
            "Check dependency binding differs",
        )
    elif value["kind"] in ("evaluation_inputs", "mapping_catalog"):
        require(
            value["depends_on"] == [value["payload"]["inventory_id"]],
            "Evaluation inventory dependency differs",
        )
    elif value["kind"] == "reconciliation":
        p = value["payload"]
        require(
            value["depends_on"]
            == sorted([p["selection_id"], p["inputs_id"], p["authority_id"]]),
            "Reconciliation dependency binding differs",
        )
    elif value["kind"] == "coverage":
        p = value["payload"]
        require(
            value["depends_on"]
            == sorted([*p["reconciliation_ids"], p["metric_catalog_id"]]),
            "Coverage dependency binding differs",
        )
    elif value["kind"] == "pdf_inputs":
        p = value["payload"]
        require(
            value["depends_on"]
            == sorted(
                [p["selection_id"], p["evaluation_inputs_id"], p["mapping_catalog_id"]]
            ),
            "PDF input dependency binding differs",
        )
    elif value["kind"] == "pdf_checks":
        require(
            value["depends_on"] == [value["payload"]["inputs_id"]],
            "PDF check dependency binding differs",
        )
    else:
        p = value["payload"]
        require(
            value["depends_on"] == sorted([p["coverage_id"], *p["check_ids"]]),
            "PDF capability dependency binding differs",
        )


class LedgerStore:
    def __init__(self, path=":memory:"):
        self.connection = sqlite3.connect(str(path), isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        tables = {
            r[0]
            for r in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        require(
            not tables
            or tables == {"ledger_metadata", "ledger_records", "ledger_dependencies"},
            "Refusing a database owned by another application",
        )
        if tables:
            require(
                [
                    r[0]
                    for r in self.connection.execute(
                        "SELECT version FROM ledger_metadata"
                    )
                ]
                == ["offline_unified_ledger/1"],
                "Ledger version differs",
            )
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript("""
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS ledger_metadata(version TEXT PRIMARY KEY);
INSERT OR IGNORE INTO ledger_metadata VALUES('offline_unified_ledger/1');
CREATE TABLE IF NOT EXISTS ledger_records(record_id TEXT PRIMARY KEY,kind TEXT NOT NULL,payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ledger_dependencies(record_id TEXT REFERENCES ledger_records,depends_on TEXT REFERENCES ledger_records,PRIMARY KEY(record_id,depends_on));
COMMIT;
""")
        for table in ("ledger_metadata", "ledger_records", "ledger_dependencies"):
            for op in ("UPDATE", "DELETE"):
                self.connection.execute(
                    f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{op} BEFORE {op} ON {table} BEGIN SELECT RAISE(ABORT,'immutable ledger'); END"
                )
        if str(path) != ":memory:":
            Path(path).chmod(0o600)

    @contextmanager
    def atomic(self):
        require(
            not self.connection.in_transaction, "Nested ledger transaction unsupported"
        )
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def append(self, value):
        validate(value)
        body = canonical(value)
        require(len(body.encode()) <= 16 * 1024 * 1024, "Ledger record exceeds bound")
        dependencies = {dep: self.read(dep) for dep in value["depends_on"]}
        self._bindings(value, dependencies)
        old = self.connection.execute(
            "SELECT payload FROM ledger_records WHERE record_id=?",
            (value["record_id"],),
        ).fetchone()
        require(old is None or old[0] == body, "Ledger content collision")
        # One transaction in callers can atomically append inventory + decision.
        nested = self.connection.in_transaction
        self.connection.execute("SAVEPOINT append_record")
        try:
            self.connection.execute(
                "INSERT OR IGNORE INTO ledger_records VALUES(?,?,?)",
                (value["record_id"], value["kind"], body),
            )
            for dep in value["depends_on"]:
                self.connection.execute(
                    "INSERT OR IGNORE INTO ledger_dependencies VALUES(?,?)",
                    (value["record_id"], dep),
                )
            self.connection.execute("RELEASE append_record")
        except BaseException:
            self.connection.execute("ROLLBACK TO append_record")
            self.connection.execute("RELEASE append_record")
            raise
        require(
            self.connection.in_transaction == nested, "Ledger transaction state changed"
        )
        return "noop" if old else "recorded"

    def read(self, record_id, depth=0):
        return self._read(record_id, depth, {})

    def _read(self, record_id, depth, memo):
        require(depth <= 4, "Ledger dependency cycle/depth exceeded")
        if record_id in memo:
            return memo[record_id]
        row = self.connection.execute(
            "SELECT kind,payload FROM ledger_records WHERE record_id=?", (record_id,)
        ).fetchone()
        require(row is not None, "Unknown ledger dependency")
        require(
            len(row["payload"].encode()) <= 16 * 1024 * 1024,
            "Stored ledger record exceeds bound",
        )
        value = parse_json(row["payload"])
        validate(value)
        require(
            value["record_id"] == record_id and value["kind"] == row["kind"],
            "Ledger identity binding differs",
        )
        edges = sorted(
            r[0]
            for r in self.connection.execute(
                "SELECT depends_on FROM ledger_dependencies WHERE record_id=?",
                (record_id,),
            )
        )
        require(edges == value["depends_on"], "Ledger dependency integrity failed")
        dependencies = {dep: self._read(dep, depth + 1, memo) for dep in edges}
        self._bindings(value, dependencies)
        memo[record_id] = value
        return value

    def _bindings(self, value, dependencies):
        from .selection import pending_check, select, validate_inventory

        if value["kind"] == "inventory":
            validate_inventory(value)
        elif value["kind"] == "selection":
            payload = value["payload"]
            expected = select(
                dependencies[payload["inventory_id"]],
                dependencies[payload["policy_id"]],
                [s["scope"] for s in payload["scopes"]],
                payload["selection_as_of"],
            )
            require(
                value == expected, "Selection differs from deterministic recomputation"
            )
        elif value["kind"] == "pending_check":
            payload = value["payload"]
            expected = pending_check(
                dependencies[payload["selection_id"]],
                payload["family"],
                payload["scope"],
                payload["reason"],
            )
            require(value == expected, "Check selection/scope binding differs")
        elif value["kind"] in (
            "expected_inventory",
            "evaluation_inputs",
            "reconciliation",
            "coverage",
        ):
            from .evaluation import (
                coverage,
                reconciliation,
                validate_expected,
                validate_inputs,
            )

            p = value["payload"]
            if value["kind"] == "expected_inventory":
                validate_expected(value)
            elif value["kind"] == "evaluation_inputs":
                validate_inputs(value, dependencies[p["inventory_id"]])
            elif value["kind"] == "reconciliation":
                expected = reconciliation(
                    dependencies[p["selection_id"]],
                    dependencies[p["inputs_id"]],
                    dependencies[p["authority_id"]],
                )
                require(
                    value == expected,
                    "Reconciliation differs from deterministic recomputation",
                )
            else:
                expected = coverage(
                    [dependencies[k] for k in p["reconciliation_ids"]],
                    dependencies[p["metric_catalog_id"]],
                    p["requests"],
                )
                require(
                    value == expected,
                    "Coverage differs from deterministic recomputation",
                )
        elif value["kind"] in (
            "mapping_catalog",
            "pdf_inputs",
            "pdf_checks",
            "pdf_capability",
        ):
            from .pdf_checks import (
                evaluate_pdf,
                pdf_capability,
                validate_catalog,
                validate_pdf_inputs,
            )

            p = value["payload"]
            if value["kind"] == "mapping_catalog":
                validate_catalog(value, dependencies[p["inventory_id"]])
            elif value["kind"] == "pdf_inputs":
                validate_pdf_inputs(
                    value,
                    dependencies[p["evaluation_inputs_id"]],
                    dependencies[p["mapping_catalog_id"]],
                    dependencies[p["selection_id"]],
                )
            elif value["kind"] == "pdf_checks":
                pdf_inputs = dependencies[p["inputs_id"]]
                refs = pdf_inputs["payload"]
                expected = evaluate_pdf(
                    pdf_inputs,
                    self.read(refs["mapping_catalog_id"]),
                    self.read(refs["selection_id"]),
                    self.read(refs["evaluation_inputs_id"]),
                )
                require(
                    value == expected,
                    "PDF checks differ from deterministic re-execution",
                )
            else:
                expected = pdf_capability(
                    dependencies[p["coverage_id"]],
                    [dependencies[k] for k in p["check_ids"]],
                )
                require(
                    value == expected,
                    "PDF capability differs from deterministic recomputation",
                )

    def close(self):
        self.connection.close()
