"""Structured assertions and comparable-run checks; no keyword grading."""

import math


def read_path(value, path):
    for part in path.split("."):
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


def grade(payload, checks):
    results = []
    for check in checks:
        try:
            actual = read_path(payload, check["path"])
            target = check["equals"]
            if type(target) in (int, float):
                passed = (
                    type(actual) in (int, float)
                    and math.isfinite(actual)
                    and math.isclose(
                        actual,
                        target,
                        rel_tol=0,
                        abs_tol=check.get("absolute_tolerance", 0),
                    )
                )
            else:
                passed = type(actual) is type(target) and actual == target
            results.append({**check, "actual": actual, "passed": passed})
        except (KeyError, IndexError, ValueError, TypeError):
            results.append({**check, "missing": True, "passed": False})
    return results


def canonical(payload):
    if isinstance(payload, dict):
        return {
            k: canonical(v)
            for k, v in payload.items()
            if k
            not in {"request_id", "conversation_id", "duration_ms", "answer_streamed"}
        }
    if isinstance(payload, list):
        return [canonical(v) for v in payload]
    return payload


def comparable(a, b):
    return all(
        a.get(k) == b.get(k)
        for k in (
            "case_sha256",
            "source_sha256",
            "execution_mode",
            "grader_sha256",
            "fixture_adapter_sha256",
            "provider",
            "model",
            "clock",
            "selected_cases_sha256",
            "schema_sha256",
            "cassette_sha256",
        )
    )


def expected_failure_payload(expected, mode, status, payload, events):
    """Normalize only an explicitly declared, fully sanitized terminal failure.

    Keep raw transport frames in the runner's report. Unexpected errors and any
    partial/final statistical output must still fail the evaluation.
    """
    if expected is None:
        return None
    target = {"detail": expected["detail"]}
    if mode == "json":
        return (
            target if status == expected["http_status"] and payload == target else None
        )
    errors = [event for event in events if event.get("type") == "error"]
    if (
        status == 200
        and len(errors) == 1
        and errors[0].get("detail") == expected["detail"]
        and set(errors[0]) <= {"type", "detail", "request_id"}
        and events[-1] is errors[0]
        and all(event.get("type") in (None, "plan", "error") for event in events)
    ):
        return target
    return None
