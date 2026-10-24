"""Shared thin-split map validation; no network or application dependencies."""

from __future__ import annotations

import fnmatch
import json
from pathlib import PurePosixPath

MAP_PATH = "docs/repository-ownership.json"
PRESENCE = {"owned", "frozen", "vendored", "exception", "forbidden"}


def valid_path(value: object, *, pattern: bool = False) -> bool:
    if not isinstance(value, str) or not value or "\\" in value:
        return False
    parts = value.split("/")
    return (
        not PurePosixPath(value).is_absolute()
        and all(part not in ("", ".", "..") for part in parts)
        and (pattern or not any(char in value for char in "*?[]"))
    )


def parse_map(content: str) -> dict:
    try:
        policy = json.loads(content)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid ownership map JSON") from exc
    if not isinstance(policy, dict) or policy.get("schema") != "thin-split-ownership/1":
        raise ValueError("Unknown ownership map schema")
    rules = policy.get("rules")
    if not isinstance(rules, list) or not rules:
        raise ValueError("Ownership map requires rules")
    patterns_seen = set()
    for rule in rules:
        if not isinstance(rule, dict):
            raise ValueError("Invalid ownership rule")
        patterns = rule.get("patterns")
        if (
            rule.get("owner") not in {"public", "private"}
            or rule.get("private_presence") not in PRESENCE
            or type(rule.get("priority")) is not int
            or rule["priority"] < 0
            or not isinstance(rule.get("description"), str)
            or not rule["description"].strip()
            or not isinstance(patterns, list)
            or not patterns
        ):
            raise ValueError("Invalid ownership rule fields")
        if (rule["owner"] == "private") != (rule["private_presence"] == "owned"):
            raise ValueError("Private ownership must use owned presence")
        for pattern in patterns:
            if not valid_path(pattern, pattern=True) or pattern in patterns_seen:
                raise ValueError("Invalid or duplicate ownership pattern")
            patterns_seen.add(pattern)
    required = policy.get("required_private_paths")
    if (
        not isinstance(required, list)
        or not required
        or any(not valid_path(path) for path in required)
    ):
        raise ValueError("Invalid required private paths")
    if len(set(required)) != len(required):
        raise ValueError("Duplicate required private path")
    for path in required:
        rule = resolve(policy, path)
        if rule is None or rule["private_presence"] != "owned":
            raise ValueError("Required private path must be privately owned")
    return policy


def resolve(policy: dict, path: str) -> dict | None:
    if not valid_path(path):
        raise ValueError("Ownership requires a repository-relative path")
    matches = [
        rule
        for rule in policy["rules"]
        if any(fnmatch.fnmatchcase(path, pattern) for pattern in rule["patterns"])
    ]
    if not matches:
        return None
    priority = max(rule["priority"] for rule in matches)
    winners = [rule for rule in matches if rule["priority"] == priority]
    if len(winners) != 1:
        raise ValueError(f"{path}: ambiguous ownership patterns")
    return winners[0]
