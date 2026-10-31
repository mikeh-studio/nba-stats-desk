"""Exact maintenance records flag restricted changes without claiming approval."""

import copy
import importlib.util
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

# Load this standalone checker independently of other tests' sys.path changes.
_spec = importlib.util.spec_from_file_location(
    "acquisition_boundary_under_test",
    Path(__file__).resolve().parents[1] / "scripts/check_acquisition_boundary.py",
)
_boundary = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_boundary)
digest, validate = _boundary.digest, _boundary.validate


def test_boundary_changes_require_exact_records_and_keep_human_review():
    mapping = {
        "schema": "acquisition-development-boundary/1",
        "main_snapshot": "a" * 40,
        "paths": [
            {
                "path": "scripts/collector.py",
                "maintenance": "compatibility_only",
                "forward_development": "private",
                "presence_at_snapshot": "public_main",
            }
        ],
    }
    empty = {"schema": "acquisition-maintenance/1", "changes": []}
    assert validate(mapping, mapping, empty, {"app/static/ui.js": (b"a", b"b")})[
        "human_review_required"
    ]
    with pytest.raises(ValueError, match="exact reviewed"):
        validate(mapping, mapping, empty, {"scripts/collector.py": (b"a", b"b")})
    records = copy.deepcopy(empty)
    records["changes"].append(
        {
            "path": "scripts/collector.py",
            "kind": "security",
            "reason": "Proposed bounded security repair",
            "before_sha256": digest(b"a"),
            "after_sha256": digest(b"b"),
        }
    )
    report = validate(mapping, mapping, records, {"scripts/collector.py": (b"a", b"b")})
    assert not report["publication_authorized"]
    with pytest.raises(ValueError, match="exact reviewed"):
        validate(mapping, mapping, records, {"scripts/collector.py": (b"a", b"c")})
    removed = copy.deepcopy(mapping)
    removed["paths"].append(
        {
            "path": "contracts/source.yml",
            "maintenance": "shared_guard",
            "forward_development": "public",
            "presence_at_snapshot": "public_main",
        }
    )
    with pytest.raises(ValueError, match="removed or reclassified"):
        validate(mapping, removed, empty, {})
    assert validate(mapping, None, empty, {})["policy_bootstrap"]


def test_shared_guards_are_reported_and_snapshot_metadata_is_separate():
    row = {
        "path": "app/guard.py",
        "maintenance": "shared_guard",
        "forward_development": "public",
        "presence_at_snapshot": "public_main",
    }
    mapping = {
        "schema": "acquisition-development-boundary/1",
        "main_snapshot": "a" * 40,
        "paths": [row],
    }
    unused = {
        "schema": "acquisition-maintenance/1",
        "changes": [
            {
                "path": "scripts/old.py",
                "kind": "security",
                "reason": "Historical repair",
                "before_sha256": digest(b"a"),
                "after_sha256": digest(b"b"),
            }
        ],
    }
    report = validate(
        mapping, mapping, unused, {"app/guard.py": (b"a", b"new feature")}
    )
    assert report["restricted_changes"] == []
    assert report["shared_guard_changes"] == ["app/guard.py"]
    assert report["unused_maintenance_records"] == ["scripts/old.py"]
    _boundary.validate_snapshot(mapping, tracked={"app/guard.py"})
    updated = copy.deepcopy(mapping)
    updated["main_snapshot"] = "b" * 40
    updated["paths"][0]["presence_at_snapshot"] = "absent_from_main_snapshot"
    validate(
        updated, mapping, {"schema": "acquisition-maintenance/1", "changes": []}, {}
    )
    _boundary.validate_snapshot(updated, tracked=set())
    with pytest.raises(ValueError, match="declared tree"):
        _boundary.validate_snapshot(updated, tracked={"app/guard.py"})
    updated["paths"][0]["presence_at_snapshot"] = "invalid"
    with pytest.raises(ValueError, match="presence"):
        _boundary.parse_map(updated)


def test_snapshot_must_belong_to_reviewed_base_history():
    mapping = {
        "schema": "acquisition-development-boundary/1",
        "main_snapshot": "a" * 40,
        "paths": [
            {
                "path": "app/guard.py",
                "maintenance": "shared_guard",
                "forward_development": "public",
                "presence_at_snapshot": "absent_from_main_snapshot",
            }
        ],
    }
    base = "b" * 40
    with patch.object(_boundary, "git", return_value=b"") as git:
        _boundary.validate_snapshot(mapping, tracked=set(), base=base)
        git.assert_called_once_with("merge-base", "--is-ancestor", "a" * 40, base)
    with patch.object(
        _boundary,
        "git",
        side_effect=subprocess.CalledProcessError(1, ["git", "merge-base"]),
    ):
        with pytest.raises(ValueError, match="ancestor of the reviewed base"):
            _boundary.validate_snapshot(mapping, tracked=set(), base=base)
