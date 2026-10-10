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
        )
    )
