"""Independently compare a proposed release with its physical warehouse tables.

This verifies content only. Source evidence and final release acceptance must
be checked separately; this function never changes eligibility or the pointer.
"""

import re

from scripts.validate_publication_contract import digest, timestamp, validate_binding

from app.serving.core_models import build_models
from app.serving.publication import equal_values, ordered


def verify_candidate(client, envelope, accepted, *, project, namespace, now):
    if not re.fullmatch(r"[a-z][a-z0-9-]{4,61}[a-z0-9]", project) or not re.fullmatch(
        r"nba_core_[a-z0-9_]{3,64}", namespace
    ):
        raise ValueError("invalid production target")
    manifest, binding = envelope["manifest"], envelope["binding"]
    validate_binding(binding, manifest)
    if (
        manifest["version"] != "publication-manifest/2"
        or manifest["purpose"] != "production"
        or manifest["evidence_kind"] != "retained"
        or accepted["scope"] != manifest["scope"]
    ):
        raise ValueError("production candidate differs")
    if (
        not timestamp(manifest["validation_as_of"])
        <= timestamp(now)
        < timestamp(manifest["freshness"]["expires_at"])
    ):
        raise ValueError("candidate expired")
    expected = build_models(
        accepted,
        materialized_at=manifest["materialized_at"],
        release_scope=manifest["scope"],
    )
    pid = manifest["publication_id"]
    verified = {}
    for asset in manifest["assets"]:
        name = asset["name"]
        relation = f"{project}.{namespace}_release_{pid[4:]}.{name}"
        if asset["relation"] != relation:
            raise ValueError("candidate table outside release")
        table = client.get_table(relation, timeout=30)
        if (
            table.table_type != "SNAPSHOT"
            or table.num_rows != asset["rows"]
            or table.expires is None
            or table.expires < timestamp(manifest["freshness"]["expires_at"])
        ):
            raise ValueError("candidate snapshot metadata differs")
        rows = ordered(name, client.list_rows(table, max_results=asset["rows"] + 1))
        if (
            len(rows) != asset["rows"]
            or digest(rows) != asset["digest"]
            or not equal_values(rows, ordered(name, expected[name]))
        ):
            raise ValueError("candidate snapshot contents differ")
        verified[name] = {
            "digest": asset["digest"],
            "rows": len(rows),
            "etag": table.etag,
        }
    return {
        "version": "candidate-content-verification/1",
        "status": "verified_contents_only",
        "publication_id": pid,
        "manifest_digest": digest(manifest),
        "binding_digest": digest(binding),
        "tables": verified,
        "runtime_eligible": False,
    }
