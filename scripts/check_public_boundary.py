"""Check the Git index for private artifacts and recognizable credentials.

Run before committing; CI checks the checked-out commit's index. Diagnostics
contain paths and rule names only, never matching content. This is a targeted
guard, not a comprehensive secret scanner or a review of Git history.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path, PurePosixPath

PRIVATE_DIRS = {
    "local_notes",
    "reports",
    "notebooks",
    "exports",
    "quarantine",
    "private",
    "credentials",
    "logs",
    "airflow_home",
    ".terraform",
    ".context",
    ".gstack",
}
PRIVATE_FILES = {
    "AGENTS.local.md",
    "AGENTS.override.md",
    "CLAUDE.md",
    "DESIGN.md",
    "TODOS.md",
    "airflow.cfg",
    "webserver_config.py",
}
PRIVATE_SUFFIXES = {
    ".ipynb",
    ".jsonl",
    ".parquet",
    ".pkl",
    ".pickle",
    ".joblib",
    ".npy",
    ".npz",
    ".pt",
    ".pth",
    ".onnx",
    ".safetensors",
    ".log",
    ".duckdb",
    ".sqlite",
    ".sqlite3",
    ".db",
    ".pem",
    ".key",
    ".p12",
    ".pfx",
}
SECRET_PATTERNS = {
    "private key": rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----",
    "provider token": rb"\bsk-(?:proj-|svcacct-|ant-)?[A-Za-z0-9_-]{20,}",
    "AWS access key": rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b",
    "GitHub token": rb"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})",
    "Google API key": rb"\bAIza[A-Za-z0-9_-]{35}\b",
}


def private_path(path: str) -> bool:
    p = PurePosixPath(path)
    return (
        bool(set(p.parts[:-1]) & PRIVATE_DIRS)
        or p.name in PRIVATE_FILES
        or (p.name.startswith(".env") and p.name != ".env.example")
        or p.suffix in PRIVATE_SUFFIXES
        or p.name.endswith((".tfvars", ".tfvars.json"))
        or ".tfstate" in p.name
        or path == "dbt/profiles/.user.yml"
        or path.startswith("infra/terraform-aws/env/")
        or path == "infra/terraform-aws/env"
        or path.startswith(".claude/")
    )


def check_index(root: Path) -> list[tuple[str, str]]:
    entries = subprocess.check_output(
        ["git", "ls-files", "--stage", "-z"], cwd=root
    ).split(b"\0")
    failures: list[tuple[str, str]] = []
    for entry in filter(None, entries):
        metadata, raw_path = entry.split(b"\t", 1)
        mode, blob, stage = metadata.split()
        path = raw_path.decode("utf-8", errors="surrogateescape")
        if stage != b"0":
            failures.append((path, "unresolved merge"))
            continue
        if private_path(path):
            failures.append((path, "private artifact path"))
        if mode == b"160000":
            continue  # Submodule content is outside this repository's index.
        content = subprocess.check_output(
            ["git", "cat-file", "blob", blob.decode("ascii")], cwd=root
        )
        for rule, pattern in SECRET_PATTERNS.items():
            if re.search(pattern, content):
                failures.append((path, rule))
    return failures


def main() -> int:
    root = Path(
        subprocess.check_output(["git", "rev-parse", "--show-toplevel"])
        .decode()
        .strip()
    )
    failures = check_index(root)
    for path, rule in failures:
        print(f"{path!r}: {rule}")
    if failures:
        print("Public boundary check failed. Remove private artifacts from the index.")
        return 1
    print("Public boundary check passed (Git index; targeted credential patterns).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
