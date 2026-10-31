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


def parse_map(data, *, legacy=False):
    if data.get("schema") != "acquisition-development-boundary/1":
        raise ValueError("Unknown boundary schema")
    snapshot = data.get("main_snapshot")
    if (
        not isinstance(snapshot, str)
        or len(snapshot) != 40
        or any(c not in "0123456789abcdef" for c in snapshot)
    ):
        raise ValueError("Boundary requires exact main_snapshot commit")
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
        presence = row.get("presence_at_snapshot")
        if presence not in {"public_main", "absent_from_main_snapshot"} and not (
            legacy
            and row.get("presence") in {"public_main", "disclosed_draft_112_only"}
        ):
            raise ValueError("Invalid snapshot presence")
        result[path] = row
    if not result:
        raise ValueError("Empty boundary map")
    return result


def validate(current, previous, records, changed):
    now = parse_map(current)
    old = parse_map(previous, legacy=True) if previous else {}
    fields = ("maintenance", "forward_development")
    if any(
        path not in now or any(now[path].get(k) != row.get(k) for k in fields)
        for path, row in old.items()
    ):
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
    compatibility = {
        p
        for p, row in {**old, **now}.items()
        if row["maintenance"] == "compatibility_only"
    }
    shared = {
        p for p, row in {**old, **now}.items() if row["maintenance"] == "shared_guard"
    }
    restricted = sorted(set(changed) & compatibility)
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
        "shared_guard_changes": sorted(set(changed) & shared),
        "unused_maintenance_records": sorted(set(dispositions) - set(restricted)),
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


def validate_snapshot(data, *, tracked=None, base=None):
    rows = parse_map(data)
    if base is not None:
        try:
            git("merge-base", "--is-ancestor", data["main_snapshot"], base)
        except subprocess.CalledProcessError as exc:
            raise ValueError(
                "main_snapshot must be an ancestor of the reviewed base"
            ) from exc
    if tracked is None:
        tracked = set(
            git("ls-tree", "-r", "--name-only", data["main_snapshot"])
            .decode()
            .splitlines()
        )
    for path, row in rows.items():
        expected = "public_main" if path in tracked else "absent_from_main_snapshot"
        if row["presence_at_snapshot"] != expected:
            raise ValueError("Snapshot presence disagrees with declared tree: " + path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    parser.add_argument(
        "--summary", type=Path, help="Optional explicit CI summary output"
    )
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
    current = json.loads((ROOT / MAP).read_text())
    validate_snapshot(current, base=base)
    old = baseline_file(base, MAP)
    report = validate(
        current,
        json.loads(old) if old else None,
        json.loads((ROOT / RECORD).read_text()),
        changed,
    )
    print(json.dumps(report, sort_keys=True))
    if args.summary:
        args.summary.write_text(
            "## Acquisition boundary review\n\n"
            + "\n".join(
                [
                    "- Compatibility changes requiring exact records: "
                    + str(len(report["restricted_changes"])),
                    "- Shared public guard changes (ordinary public review): "
                    + str(len(report["shared_guard_changes"])),
                    "- Policy files requiring human review: "
                    + (", ".join(report["policy_review_paths"]) or "none"),
                    "- Snapshot ancestry and membership verified at: `"
                    + current["main_snapshot"]
                    + "`",
                    "- This report does not certify approval or authorize publication.",
                ]
            )
            + "\n"
        )


if __name__ == "__main__":
    main()
