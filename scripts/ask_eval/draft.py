"""Create private, pending regression drafts; never infer an oracle from an answer.

Run with python -m scripts.ask_eval.draft --help. No model or warehouse calls.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import re
import stat
import subprocess
from pathlib import Path
from uuid import UUID

from app.agent.private_traces import SECRET, code_digest
from scripts.ask_eval.fixtures import FrozenWarehouse, validate_cases

ROOT = Path(__file__).resolve().parents[2]
MAX_BYTES = 8 * 1024 * 1024
SOURCE_FIELDS = {
    "schema_version",
    "kind",
    "description",
    "rows",
    "neighbors",
    "availability",
    "study",
    "source_kind",
    "snapshot",
    "season",
    "details",
}
REVIEW_FIELDS = {
    "schema",
    "status",
    "reviewer_kind",
    "reviewer",
    "failure_explanation",
    "expectations_independent_of_observed_answer",
    "mode",
    "trace_sha256",
    "source_sha256",
    "cases_sha256",
    "original_source_sha256",
    "reconstruction_note",
    "separately_authored_fictional_inputs",
}


class DraftError(ValueError):
    """Safe, content-free input errors suitable for CLI output."""


def reject_constant(_):
    raise ValueError("Nonfinite JSON")


def read_input(path):
    # Reject links at every component, nonregular files, and bounded reads.
    path = Path(os.path.abspath(path))
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:-1]:
            child = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=directory,
            )
            os.close(directory)
            directory = child
        fd = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
    except OSError as exc:
        raise DraftError("Input unavailable or unsafe (symlinks unsupported)") from exc
    finally:
        os.close(directory)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise DraftError("Input must be a regular file")
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise DraftError("Input exceeds size limit")
    try:
        value = json.loads(raw, parse_constant=reject_constant)
        json.dumps(value, allow_nan=False)
        if not isinstance(value, dict):
            raise ValueError()
    except (ValueError, UnicodeError) as exc:
        raise DraftError("Input must be a JSON object") from exc
    return value, hashlib.sha256(raw).hexdigest()


def output_parent(path):
    """Descriptor walk: preserve safe existing parent modes; new folders are private."""
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:]:
            try:
                os.mkdir(component, mode=0o700, dir_fd=directory)
            except FileExistsError:
                pass
            child = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=directory,
            )
            os.close(directory)
            directory = child
        info = os.fstat(directory)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022:
            raise DraftError(
                "Output parent must be owned by the current user and not writable by others"
            )
        return directory
    except BaseException:
        os.close(directory)
        raise


def required_text(value):
    return isinstance(value, str) and bool(value.strip())


def check_authored_content(value):
    """Limited pattern safeguard; never a claim of complete privacy review."""
    if isinstance(value, str):
        if SECRET.search(value):
            raise DraftError(
                "Potential credential or private contact/path in authored input"
            )
    elif isinstance(value, dict):
        for key, item in value.items():
            check_authored_content(key)
            if SECRET.search(
                f"{key}: {item if isinstance(item, (str, int, float)) else '[structured]'}"
            ):
                raise DraftError(
                    "Potential credential or private contact/path in authored input"
                )
            check_authored_content(item)
    elif isinstance(value, list):
        for item in value:
            check_authored_content(item)


def validate_source(source):
    if set(source) - SOURCE_FIELDS:
        raise DraftError("Unsupported frozen source metadata")
    if source.get("source_kind") == "production_snapshot":
        from scripts.ask_eval.production import validate_production_source

        validate_production_source(source)
        return source["snapshot"]["rows"]
    if source.get("kind") != "synthetic" or not isinstance(
        source.get("neighbors"), dict
    ):
        raise DraftError("Frozen source must use a supported source format")
    if not isinstance(source.get("rows"), list) or not source["rows"]:
        raise DraftError("Frozen source has no rows")
    try:
        _, evidence = FrozenWarehouse(source, "complete").load(["2024-25"])
        for row in evidence.rows:
            if type(row["player_id"]) is not int or not isinstance(
                row["player_name"], str
            ):
                raise ValueError()
    except (KeyError, TypeError, ValueError) as exc:
        raise DraftError("Malformed frozen source rows or coverage") from exc
    return evidence.rows


def validate_plan_scope(plan, identities, seasons, names):
    if not isinstance(plan, dict):
        raise DraftError("Controlled plan scope must be an object")
    for field, allowed in (("season", seasons), ("player_name", names)):
        value = plan.get(field)
        if value is not None and (not isinstance(value, str) or value not in allowed):
            raise DraftError(
                "Controlled plan season or identity is absent from frozen source"
            )
    ids = plan.get("player_ids", [])
    if not isinstance(ids, list):
        raise DraftError("Controlled plan identities must be an array")
    ids = ids + [
        plan[key]
        for key in ("player_id", "focal_player_id", "teammate_id")
        if plan.get(key) is not None
    ]
    if any(type(identity) is not int or identity not in identities for identity in ids):
        raise DraftError("Controlled plan identity is absent from frozen source")


def study_scope(source):
    scope = source.get("study", {}).get("scope", {})
    if not isinstance(scope, dict):
        raise DraftError("Study attachment scope must be an object")
    identities = {
        scope[key]
        for key in ("focal_player_id", "teammate_id")
        if type(scope.get(key)) is int
    }
    names = {
        scope[key]
        for key in ("focal_player_name", "teammate_name")
        if isinstance(scope.get(key), str)
    }
    seasons = {scope["season"]} if isinstance(scope.get("season"), str) else set()
    return identities, seasons, names


def valid_trace_identity(value):
    try:
        UUID(value)
        return True
    except (ValueError, TypeError, AttributeError):
        return False


def draft(*, trace, source, cases, review, output, original_source=None):
    """Inputs are separately authored files; observed trace values never feed checks."""
    trace_value, trace_hash = read_input(trace)
    source_value, source_hash = read_input(source)
    document, cases_hash = read_input(cases)
    reviewed, review_hash = read_input(review)
    if (
        trace_value.get("schema") != "ask_private_trace/1"
        or trace_value.get("transport") not in ("json", "sse")
        or not isinstance(trace_value.get("code"), dict)
        or not valid_trace_identity(trace_value.get("request_id"))
        or type(trace_value.get("time")) not in (int, float)
        or not math.isfinite(trace_value["time"])
        or trace_value["time"] < 0
        or not isinstance(trace_value.get("outcome"), str)
        or not re.fullmatch(r"[a-z_]{1,64}", trace_value["outcome"])
        or not isinstance(trace_value.get("error_type"), str)
        or not re.fullmatch(r"[A-Za-z0-9_]{0,100}", trace_value["error_type"])
        or not re.fullmatch(
            r"[a-f0-9]{64}", str(trace_value.get("code", {}).get("source_sha256", ""))
        )
        or not re.fullmatch(r"[a-f0-9]{64}", str(trace_value.get("config_sha256", "")))
        or not isinstance(trace_value.get("incomplete_reasons", []), list)
        or not all(
            isinstance(reason, str) and re.fullmatch(r"[a-z_]{1,100}", reason)
            for reason in trace_value.get("incomplete_reasons", [])
        )
    ):
        raise DraftError("Unsupported or malformed private trace")
    if (
        reviewed.get("schema") != "ask_failure_review/1"
        or reviewed.get("status") != "confirmed_failure"
        or reviewed.get("reviewer_kind") not in ("human", "agent")
        or not required_text(reviewed.get("reviewer"))
        or not required_text(reviewed.get("failure_explanation"))
        or reviewed.get("expectations_independent_of_observed_answer") is not True
        or reviewed.get("trace_sha256") != trace_hash
        or reviewed.get("cases_sha256") != cases_hash
        or reviewed.get("source_sha256") != source_hash
    ):
        raise DraftError(
            "A hash-bound confirmed review and independent oracle attestation are required"
        )
    if set(reviewed) - REVIEW_FIELDS:
        raise DraftError("Unsupported failure review metadata")
    mode = reviewed.get("mode")
    original_hash = source_hash
    original = source_value
    if mode == "fictional_reconstruction":
        if not original_source or not required_text(
            reviewed.get("reconstruction_note")
        ):
            raise DraftError(
                "Reconstruction requires original frozen source and authorship note"
            )
        original, original_hash = read_input(original_source)
        validate_source(original)
        if original == source_value:
            raise DraftError(
                "Fictional reconstruction must not copy the original source unchanged"
            )
        if reviewed.get("original_source_sha256") != original_hash:
            raise DraftError("Original frozen source hash does not match review")
        if (
            source_value.get("kind") != "synthetic"
            or reviewed.get("separately_authored_fictional_inputs") is not True
        ):
            raise DraftError(
                "Reconstruction requires separately authored fictional source, questions, plans and checks"
            )
    elif mode != "private_reproduction":
        raise DraftError("Choose private_reproduction or fictional_reconstruction")
    for value in (
        source_value,
        original,
        document,
        reviewed,
        trace_value.get("code", {}),
    ):
        check_authored_content(value)
    rows = validate_source(source_value)
    document = copy.deepcopy(document)
    document["human_reviewed"] = False
    for case in (
        document.get("cases", []) if isinstance(document.get("cases"), list) else []
    ):
        if isinstance(case, dict):
            case["review"] = {"status": "pending", "reviewer": None}
    schema_path = ROOT / "tests/fixtures/ask/schema.json"
    validate_cases(document, json.loads(schema_path.read_text()))
    deficiencies = [
        "human_review_pending",
        "trace_projection_is_not_complete_replay",
        "oracle_independence_is_author_attestation_not_machine_verified",
    ]
    identities = {r["player_id"] for r in rows}
    seasons = {r["season"] for r in rows}
    names = {r["player_name"] for r in rows} | {
        f"resolved_player_{identity}" for identity in identities
    }
    for case in document["cases"]:
        study_ids, study_seasons, study_names = (
            study_scope(source_value)
            if "study" in case.get("attachments", [])
            else (set(), set(), set())
        )
        if not required_text(case.get("oracle_note")):
            raise DraftError("Each case requires an independently authored oracle_note")
        for turn in case.get("turns", []):
            if not any(
                turn.get(key)
                for key in ("planner_response", "research_response", "study_response")
            ):
                deficiencies.append("controlled_planner_response_missing:" + case["id"])
            planner = turn.get("planner_response", {})
            plans = planner.get("queries", [])
            if not isinstance(plans, list):
                raise DraftError("Controlled semantic queries must be an array")
            plans = list(plans)
            # Actual semantic planner nests queries; each query uses flat scope.
            if planner and "queries" not in planner:
                deficiencies.append("controlled_semantic_queries_missing:" + case["id"])
            validate_plan_scope(planner, identities, seasons, names)
            if turn.get("research_response", {}).get("query") is not None:
                plans.append(turn["research_response"]["query"])
            if turn.get("study_response"):
                if not study_ids:
                    deficiencies.append("frozen_study_scope_missing:" + case["id"])
                else:
                    validate_plan_scope(
                        turn["study_response"], study_ids, study_seasons, study_names
                    )
            for plan in plans:
                validate_plan_scope(plan, identities, seasons, names)
            for check in turn.get("checks", []):
                if (
                    check.get("path", "").endswith("player_id")
                    and check.get("equals") is not None
                    and (
                        type(check["equals"]) is not int
                        or check["equals"] not in identities | study_ids
                    )
                ):
                    raise DraftError("Expected identity is absent from frozen source")
        for attachment in case.get("attachments", []):
            if (
                source_value.get("source_kind") == "production_snapshot"
                or attachment not in source_value
            ):
                deficiencies.append("frozen_attachment_missing:" + attachment)
    current_code = code_digest()
    if current_code != trace_value["code"]["source_sha256"]:
        deficiencies.append("current_application_fingerprint_differs_from_trace")
    provenance = {
        "schema": "ask_regression_draft/1",
        "status": "pending_human_review",
        "trace": {
            **{
                key: trace_value[key]
                for key in ("request_id", "time", "transport", "outcome", "error_type")
            },
            "path": str(Path(trace).absolute()),
            "sha256": trace_hash,
            "code": {
                "source_sha256": trace_value["code"]["source_sha256"],
                "static_version": str(trace_value["code"].get("static_version", ""))[
                    :100
                ],
            },
            "config_sha256": trace_value.get("config_sha256"),
        },
        "original_source_path": str(Path(original_source or source).absolute()),
        "input_hashes": {
            "source": source_hash,
            "original_source": original_hash,
            "cases": cases_hash,
            "review": review_hash,
        },
        "fingerprint_meanings": {
            "trace_source_sha256": "First-capture on-disk Python fingerprint; not executed code, dependencies, assets or replay compatibility",
            "trace_config_sha256": "Hash of D allowlisted effective diagnostic settings; not full environment or replay configuration",
        },
        "drafting": {
            "current_application_fingerprint": current_code,
            "tool_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "case_schema_sha256": hashlib.sha256(schema_path.read_bytes()).hexdigest(),
        },
        "failure_review": reviewed,
        "deficiencies": sorted(
            set(
                deficiencies
                + [
                    "trace:" + reason
                    for reason in trace_value.get("incomplete_reasons", [])
                ]
            )
        ),
        "publication_authorized": False,
    }
    frozen = {
        "source.json": source_value,
        "cases.json": document,
    }
    encoded = {
        name: (json.dumps(value, indent=2, allow_nan=False) + "\n").encode()
        for name, value in frozen.items()
    }
    provenance["frozen_file_sha256"] = {
        name: hashlib.sha256(data).hexdigest() for name, data in encoded.items()
    }
    encoded["provenance.json"] = (
        json.dumps(provenance, indent=2, allow_nan=False) + "\n"
    ).encode()
    output = Path(os.path.abspath(output))
    try:
        relative = output.relative_to(ROOT / "reports")
        if not relative.parts:
            raise ValueError()
    except ValueError as exc:
        raise DraftError(
            "Output must be a fresh directory under repository reports/"
        ) from exc
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", "--", str(output / "cases.json")],
        cwd=ROOT,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if ignored.returncode:
        raise DraftError("Output is not Git-ignored")
    parent_fd = output_parent(output.parent)
    created = False
    completed = False
    out_fd = None
    written = []
    try:
        os.mkdir(output.name, mode=0o700, dir_fd=parent_fd)
        created = True
        out_fd = os.open(
            output.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd
        )
        try:
            for name in ("provenance.json", "source.json", "cases.json"):
                data = encoded[name]
                fd = os.open(
                    name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=out_fd,
                )
                written.append(name)
                try:
                    stream = os.fdopen(fd, "wb")
                except BaseException:
                    os.close(fd)
                    raise
                with stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
            os.fsync(out_fd)
            os.fsync(parent_fd)
            completed = True
        finally:
            try:
                if not completed:
                    # Remove the evaluator entry point first even if another unlink fails.
                    cleanup_failed = False
                    for name in reversed(written):
                        try:
                            os.unlink(name, dir_fd=out_fd)
                        except OSError:
                            cleanup_failed = True
                    if cleanup_failed:
                        raise DraftError(
                            "Draft write failed; incomplete private directory retained; do not evaluate it"
                        )
            finally:
                os.close(out_fd)
    finally:
        try:
            if created and not completed:
                try:
                    os.rmdir(output.name, dir_fd=parent_fd)
                except OSError:
                    raise DraftError(
                        "Draft write failed; incomplete private directory retained; do not evaluate it"
                    ) from None
        finally:
            os.close(parent_fd)
    return provenance


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("trace", "source", "cases", "review", "output"):
        parser.add_argument("--" + key, type=Path, required=True)
    parser.add_argument("--original-source", type=Path)
    args = parser.parse_args()
    try:
        result = draft(**vars(args))
    except DraftError as exc:
        parser.exit(2, f"Draft rejected: {exc}\n")
    except Exception:
        parser.exit(
            2,
            "Draft rejected: invalid input, unavailable dependency, or unsafe/existing output; no input content logged.\n",
        )
    print(
        f"Private draft created; pending human review; {len(result['deficiencies'])} reproduction/review limitations. No publication authorized."
    )


if __name__ == "__main__":
    main()
