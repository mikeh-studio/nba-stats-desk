"""Check exact restricted-path changes; human review still authorizes publication."""

import argparse
import hashlib
import json
import subprocess
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
MAP = "docs/acquisition-development-boundary.json"
RECORD = "docs/acquisition-maintenance.json"
POLICY = {
    MAP,
    RECORD,
    "AGENTS.md",
    "docs/public-private-boundary.md",
    "scripts/check_acquisition_boundary.py",
    ".github/workflows/ci.yml",
}


def digest(raw):
    return hashlib.sha256(raw).hexdigest() if raw is not None else None


def parse_map(data):
    if data.get("schema") != "acquisition-development-boundary/1":
        raise ValueError("Unknown boundary schema")
    result = {}
    for row in data["paths"]:
        path = row["path"]
        if (
            not path
            or PurePosixPath(path).is_absolute()
            or any(p in {"", ".", ".."} for p in path.split("/"))
            or any(c in path for c in "\\*?[]")
            or path in result
        ):
            raise ValueError("Boundary requires unique exact source paths")
        if (row["maintenance"], row["forward_development"]) not in {
            ("shared_guard", "public"),
            ("compatibility_only", "private"),
        }:
            raise ValueError("Invalid maintenance classification")
        result[path] = row
    if not result:
        raise ValueError("Empty boundary map")
    return result


def validate(current, previous, records, changed):
    now = parse_map(current)
    old = parse_map(previous) if previous else {}
    if any(now.get(path) != row for path, row in old.items()):
        raise ValueError("Existing boundary cannot be removed or reclassified silently")
    if records.get("schema") != "acquisition-maintenance/1":
        raise ValueError("Unknown maintenance schema")
    dispositions = {}
    for row in records["changes"]:
        path = row["path"]
        if (
            path in dispositions
            or row["kind"] not in {"correctness", "security", "public_promotion"}
            or not row["reason"].strip()
        ):
            raise ValueError("Invalid exact maintenance disposition")
        dispositions[path] = row
    restricted = sorted(set(changed) & (set(now) | set(old)))
    for path in restricted:
        row = dispositions.get(path, {})
        before, after = changed[path]
        if row.get("before_sha256", "missing") != digest(before) or row.get(
            "after_sha256", "missing"
        ) != digest(after):
            raise ValueError(
                "Restricted change requires exact reviewed maintenance disposition: "
                + path
            )
    return {
        "restricted_changes": restricted,
        "policy_bootstrap": previous is None,
        "policy_review_paths": sorted(set(changed) & POLICY),
        "human_review_required": True,
        "publication_authorized": False,
    }


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT)


def baseline_file(base, path):
    if not git("ls-tree", "-r", "--name-only", base, "--", path).strip():
        return None
    return git("show", base + ":" + path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    args = parser.parse_args()
    base = git("rev-parse", "--verify", args.base + "^{commit}").decode().strip()
    names = set(git("diff", "--name-only", base, "--").decode().splitlines())
    names.update(
        git("ls-files", "--others", "--exclude-standard").decode().splitlines()
    )
    changed = {
        p: (
            baseline_file(base, p),
            (ROOT / p).read_bytes() if (ROOT / p).is_file() else None,
        )
        for p in names
    }
    old = baseline_file(base, MAP)
    print(
        json.dumps(
            validate(
                json.loads((ROOT / MAP).read_text()),
                json.loads(old) if old else None,
                json.loads((ROOT / RECORD).read_text()),
                changed,
            ),
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
