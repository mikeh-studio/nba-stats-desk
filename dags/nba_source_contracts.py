"""Source contract validation for NBA landing data.

Contracts are intentionally lightweight YAML files. This module validates
pre-landing pandas frames before they are uploaded to GCS and loaded into
BigQuery staging.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

logger = logging.getLogger("nba_source_contracts")

CONTRACT_DIR = Path(__file__).resolve().parents[1] / "contracts"
SEVERITIES = {"fatal", "quarantine", "warning"}
SUPPORTED_CHECKS = {
    "between",
    "date_between",
    "equals",
    "group_count",
    "in_set",
    "not_blank",
    "not_null",
    "sum_equals",
    "unique_key",
}


class SourceContractError(ValueError):
    """Raised when a source contract has fatal violations."""

    def __init__(
        self,
        result: dict[str, Any],
        *,
        quarantine_frame: pd.DataFrame | None = None,
    ):
        self.result = result
        self.quarantine_frame = quarantine_frame if quarantine_frame is not None else pd.DataFrame()
        source = result.get("source_name", "unknown_source")
        summary = (
            f"{source} source contract failed: "
            f"{result.get('fatal_count', 0)} fatal rule(s), "
            f"{result.get('rows_failed', 0)} failed row(s)"
        )
        super().__init__(summary)


@dataclass(frozen=True)
class SourceContractValidation:
    frame: pd.DataFrame
    quarantine_frame: pd.DataFrame
    result: dict[str, Any]


def load_contract(contract_name: str, contract_dir: Path | None = None) -> dict[str, Any]:
    """Load a source contract YAML file by name."""
    base_dir = contract_dir or CONTRACT_DIR
    contract_path = base_dir / f"{contract_name}.yml"
    with contract_path.open("r", encoding="utf-8") as handle:
        contract = yaml.safe_load(handle) or {}

    if not isinstance(contract, dict):
        raise ValueError(f"Contract {contract_path} must contain a mapping")
    for required_key in ("source", "version", "domain", "business_key", "columns"):
        if required_key not in contract:
            raise ValueError(f"Contract {contract_path} is missing {required_key!r}")
    if not isinstance(contract["columns"], dict) or not contract["columns"]:
        raise ValueError(f"Contract {contract_path} must define columns")
    if not isinstance(contract["business_key"], list) or not contract["business_key"]:
        raise ValueError(f"Contract {contract_path} must define a business_key list")

    contract_columns = set(contract["columns"])
    missing_key_columns = [
        str(column)
        for column in contract["business_key"]
        if str(column) not in contract_columns
    ]
    if missing_key_columns:
        raise ValueError(
            f"Contract {contract_path} has business_key columns not defined in "
            f"columns: {', '.join(missing_key_columns)}"
        )

    for rule in contract.get("rules", []):
        check = str(rule.get("check") or "")
        if check not in SUPPORTED_CHECKS:
            raise ValueError(f"Contract {contract_path} has unsupported check {check!r}")
        _normalize_severity(rule.get("severity", "fatal"))
        referenced_columns = _rule_columns(rule)
        target_column = rule.get("target_column")
        if target_column:
            referenced_columns.append(str(target_column))
        missing_rule_columns = [
            column for column in referenced_columns if column not in contract_columns
        ]
        if missing_rule_columns:
            raise ValueError(
                f"Contract {contract_path} rule {rule.get('name', check)!r} "
                f"references columns not defined in columns: "
                f"{', '.join(sorted(set(missing_rule_columns)))}"
            )
    return contract


def skipped_contract_result(contract_name: str, *, reason: str) -> dict[str, Any]:
    """Build a serializable contract result for intentional no-row paths."""
    contract = load_contract(contract_name)
    return {
        "domain": contract["domain"],
        "source_name": contract["source"],
        "contract_version": str(contract["version"]),
        "status": "skipped",
        "reason": reason,
        "rows_checked": 0,
        "rows_failed": 0,
        "rows_quarantined": 0,
        "fatal_count": 0,
        "warning_count": 0,
        "quarantine_count": 0,
        "violations": [],
    }


def validate_source_contract(
    contract_name: str,
    frame: pd.DataFrame,
    *,
    contract_dir: Path | None = None,
) -> SourceContractValidation:
    """Validate a dataframe against a source contract.

    Fatal violations raise SourceContractError. Quarantine violations remove the
    affected rows from the returned frame. Warning violations are recorded only.
    """
    contract = load_contract(contract_name, contract_dir=contract_dir)
    working = frame.copy()
    rows_checked = int(len(working))
    violations: list[dict[str, Any]] = []
    quarantine_indices: set[Any] = set()
    failed_indices: set[Any] = set()

    missing_columns = [
        column for column in contract["columns"].keys() if column not in working.columns
    ]
    if missing_columns:
        violations.append(
            _violation(
                rule_name="required_columns_present",
                check="required_columns",
                severity="fatal",
                failed_rows=rows_checked,
                message=f"Missing required columns: {', '.join(missing_columns)}",
                columns=missing_columns,
            )
        )

    if not missing_columns:
        type_severity = _normalize_severity(
            contract.get("type_validation_severity", "quarantine")
        )
        for column, expected_type in contract["columns"].items():
            invalid_mask = _invalid_type_mask(working[column], str(expected_type))
            failed_indices.update(
                _record_mask_violation(
                    violations,
                    invalid_mask,
                    rule_name=f"{column}_type",
                    check="type",
                    severity=type_severity,
                    columns=[column],
                    message=f"{column} must be coercible to {expected_type}",
                )
            )
            if type_severity == "quarantine":
                quarantine_indices.update(working.index[invalid_mask].tolist())

        for rule in contract.get("rules", []):
            rule_name = str(rule.get("name") or rule.get("check") or "unnamed_rule")
            check = str(rule.get("check") or "")
            severity = _normalize_severity(rule.get("severity", "fatal"))
            columns = _rule_columns(rule)
            missing_rule_columns = [
                column for column in columns if column not in working.columns
            ]
            target_column = rule.get("target_column")
            if target_column and target_column not in working.columns:
                missing_rule_columns.append(str(target_column))
            if missing_rule_columns:
                violations.append(
                    _violation(
                        rule_name=rule_name,
                        check=check,
                        severity="fatal",
                        failed_rows=rows_checked,
                        message=(
                            f"Rule references missing columns: "
                            f"{', '.join(sorted(set(missing_rule_columns)))}"
                        ),
                        columns=sorted(set(missing_rule_columns)),
                    )
                )
                continue

            invalid_mask = _evaluate_rule(working, rule)
            failed_indices.update(
                _record_mask_violation(
                    violations,
                    invalid_mask,
                    rule_name=rule_name,
                    check=check,
                    severity=severity,
                    columns=columns,
                    message=str(rule.get("message") or _default_rule_message(rule)),
                )
            )
            if severity == "quarantine":
                quarantine_indices.update(working.index[invalid_mask].tolist())

    post_quarantine_frame = working.drop(index=list(quarantine_indices))
    if not missing_columns and quarantine_indices and not post_quarantine_frame.empty:
        for rule in contract.get("rules", []):
            if _normalize_severity(rule.get("severity", "fatal")) != "fatal":
                continue
            rule_name = str(rule.get("name") or rule.get("check") or "unnamed_rule")
            check = str(rule.get("check") or "")
            invalid_mask = _evaluate_rule(post_quarantine_frame, rule)
            failed_indices.update(
                _record_mask_violation(
                    violations,
                    invalid_mask,
                    rule_name=f"post_quarantine_{rule_name}",
                    check=check,
                    severity="fatal",
                    columns=_rule_columns(rule),
                    message=(
                        "Fatal rule failed after quarantine filtering: "
                        f"{_default_rule_message(rule)}"
                    ),
                )
            )

    quarantine_frame = _build_quarantine_frame(working, quarantine_indices)
    fatal_count = _count_violations(violations, "fatal")
    warning_count = _count_violations(violations, "warning")
    quarantine_count = _count_violations(violations, "quarantine")

    result = {
        "domain": contract["domain"],
        "source_name": contract["source"],
        "contract_version": str(contract["version"]),
        "status": "passed",
        "rows_checked": rows_checked,
        "rows_failed": int(len(failed_indices))
        if failed_indices
        else int(sum(v["failed_rows"] for v in violations if v["severity"] == "fatal")),
        "rows_quarantined": int(len(quarantine_indices)),
        "fatal_count": fatal_count,
        "warning_count": warning_count,
        "quarantine_count": quarantine_count,
        "violations": violations,
    }

    if fatal_count:
        result["status"] = "fatal"
        logger.error("Source contract failed: %s", result)
        raise SourceContractError(result, quarantine_frame=quarantine_frame)

    filtered = working.drop(index=list(quarantine_indices)).reset_index(drop=True)
    if rows_checked > 0 and filtered.empty:
        result["status"] = "fatal"
        result["fatal_count"] = result["fatal_count"] + 1
        result["violations"].append(
            _violation(
                rule_name="quarantine_exhausted_frame",
                check="quarantine",
                severity="fatal",
                failed_rows=rows_checked,
                message="All extracted rows were quarantined by source contract rules",
                columns=[],
            )
        )
        result["rows_failed"] = max(result["rows_failed"], rows_checked)
        logger.error("Source contract quarantined every row: %s", result)
        raise SourceContractError(result, quarantine_frame=quarantine_frame)

    if quarantine_indices:
        result["status"] = "quarantine"
    elif warning_count:
        result["status"] = "warning"

    logger.info(
        "Source contract %s status=%s rows_checked=%s rows_quarantined=%s",
        contract["source"],
        result["status"],
        rows_checked,
        result["rows_quarantined"],
    )
    return SourceContractValidation(
        frame=filtered,
        quarantine_frame=quarantine_frame,
        result=result,
    )


def validate_contract_files(contract_dir: Path | None = None) -> list[dict[str, Any]]:
    """Load all contract files and return their parsed contents."""
    base_dir = contract_dir or CONTRACT_DIR
    return [load_contract(path.stem, contract_dir=base_dir) for path in base_dir.glob("*.yml")]


def _normalize_severity(value: Any) -> str:
    severity = str(value or "fatal").lower()
    if severity not in SEVERITIES:
        raise ValueError(f"Unsupported source contract severity: {value!r}")
    return severity


def _rule_columns(rule: dict[str, Any]) -> list[str]:
    if "columns" in rule:
        return [str(column) for column in rule["columns"]]
    if "column" in rule:
        return [str(rule["column"])]
    return []


def _invalid_type_mask(series: pd.Series, expected_type: str) -> pd.Series:
    present = series.notna()
    lowered = expected_type.lower()
    if lowered in {"string", "str"}:
        return pd.Series(False, index=series.index)
    if lowered in {"int", "int64", "integer"}:
        numeric = pd.to_numeric(series, errors="coerce")
        invalid = present & numeric.isna()
        fractional = numeric.notna() & ((numeric % 1).abs() > 1e-9)
        return invalid | fractional
    if lowered in {"float", "float64", "double", "number"}:
        numeric = pd.to_numeric(series, errors="coerce")
        return present & numeric.isna()
    if lowered in {"date", "timestamp", "datetime"}:
        parsed = pd.to_datetime(series, errors="coerce", utc=lowered != "date")
        return present & parsed.isna()
    if lowered in {"bool", "boolean"}:
        valid_values = {"true", "false", "1", "0", "yes", "no", "y", "n"}
        normalized = series[present].astype(str).str.strip().str.lower()
        invalid_values = ~normalized.isin(valid_values)
        mask = pd.Series(False, index=series.index)
        mask.loc[normalized.index] = invalid_values
        return mask
    raise ValueError(f"Unsupported source contract type: {expected_type!r}")


def _evaluate_rule(frame: pd.DataFrame, rule: dict[str, Any]) -> pd.Series:
    check = str(rule.get("check") or "")
    columns = _rule_columns(rule)
    allow_null = bool(rule.get("allow_null", False))

    if check == "not_null":
        mask = pd.Series(False, index=frame.index)
        for column in columns:
            blank = frame[column].astype(str).str.strip() == ""
            mask = mask | frame[column].isna() | blank
        return mask
    if check == "not_blank":
        mask = pd.Series(False, index=frame.index)
        for column in columns:
            mask = mask | frame[column].isna() | (frame[column].astype(str).str.strip() == "")
        return mask
    if check == "unique_key":
        return frame.duplicated(subset=columns, keep=False)
    if check == "equals":
        column = columns[0]
        expected = rule.get("value")
        present = frame[column].notna()
        mismatch = frame[column].astype(str) != str(expected)
        return mismatch & (present | (not allow_null))
    if check == "in_set":
        allowed = {str(value) for value in rule.get("values", [])}
        mask = pd.Series(False, index=frame.index)
        for column in columns:
            present = frame[column].notna()
            invalid = ~frame[column].astype(str).isin(allowed)
            mask = mask | (invalid & (present | (not allow_null)))
        return mask
    if check == "between":
        min_value = float(rule["min"])
        max_value = float(rule["max"])
        mask = pd.Series(False, index=frame.index)
        for column in columns:
            numeric = pd.to_numeric(frame[column], errors="coerce")
            present = frame[column].notna()
            invalid = numeric.isna() | (numeric < min_value) | (numeric > max_value)
            mask = mask | (invalid & (present | (not allow_null)))
        return mask
    if check == "date_between":
        column = columns[0]
        parsed = pd.to_datetime(frame[column], errors="coerce").dt.normalize()
        present = frame[column].notna()
        min_date = pd.Timestamp(str(rule["min"]))
        max_date = pd.Timestamp(str(rule["max"]))
        invalid = parsed.isna() | (parsed < min_date) | (parsed > max_date)
        return invalid & (present | (not allow_null))
    if check == "group_count":
        expected = int(rule["expected"])
        group_sizes = frame.groupby(columns, dropna=False)[columns[0]].transform("size")
        return group_sizes != expected
    if check == "sum_equals":
        target_column = str(rule["target_column"])
        target = pd.to_numeric(frame[target_column], errors="coerce")
        components = frame[columns].apply(pd.to_numeric, errors="coerce").fillna(0)
        component_sum = components.sum(axis=1)
        return target.isna() | ((target - component_sum).abs() > 1e-6)

    raise ValueError(f"Unsupported source contract check: {check!r}")


def _record_mask_violation(
    violations: list[dict[str, Any]],
    invalid_mask: pd.Series,
    *,
    rule_name: str,
    check: str,
    severity: str,
    columns: list[str],
    message: str,
) -> set[Any]:
    failed_rows = int(invalid_mask.sum())
    if failed_rows == 0:
        return set()
    failed_index_values = set(invalid_mask[invalid_mask].index.tolist())
    violations.append(
        _violation(
            rule_name=rule_name,
            check=check,
            severity=severity,
            failed_rows=failed_rows,
            message=message,
            columns=columns,
            sample_indices=[str(value) for value in invalid_mask[invalid_mask].index[:10]],
        )
    )
    return failed_index_values


def _build_quarantine_frame(
    frame: pd.DataFrame, quarantine_indices: set[Any]
) -> pd.DataFrame:
    if not quarantine_indices:
        return pd.DataFrame(columns=["_source_row_index", *frame.columns])
    return (
        frame.loc[list(quarantine_indices)]
        .copy()
        .reset_index(drop=False)
        .rename(columns={"index": "_source_row_index"})
    )


def _violation(
    *,
    rule_name: str,
    check: str,
    severity: str,
    failed_rows: int,
    message: str,
    columns: list[str],
    sample_indices: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "rule": rule_name,
        "check": check,
        "severity": severity,
        "failed_rows": int(failed_rows),
        "message": message,
        "columns": columns,
        "sample_indices": sample_indices or [],
    }


def _default_rule_message(rule: dict[str, Any]) -> str:
    check = str(rule.get("check") or "unknown")
    columns = ", ".join(_rule_columns(rule))
    return f"{check} failed for {columns}".strip()


def _count_violations(violations: list[dict[str, Any]], severity: str) -> int:
    return sum(
        1
        for violation in violations
        if violation["severity"] == severity and violation["failed_rows"] > 0
    )
