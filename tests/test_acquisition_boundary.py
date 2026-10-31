"""Exact maintenance records flag restricted changes without claiming approval."""

import copy
import importlib.util
from pathlib import Path

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
        "paths": [
            {
                "path": "scripts/collector.py",
                "maintenance": "compatibility_only",
                "forward_development": "private",
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
        }
    )
    with pytest.raises(ValueError, match="removed or reclassified"):
        validate(mapping, removed, empty, {})
    assert validate(mapping, None, empty, {})["policy_bootstrap"]
