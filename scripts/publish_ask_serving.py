"""Explicit isolated BigQuery dev publication; never invoked by Ask requests.

Promote the private reference only after read-back validation. Existing reference
files require an expected digest, avoiding lost updates between operators.
"""

from __future__ import annotations

import argparse
import json
import os
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from app.agent.semantic_source import load_snapshot
from app.agent.semantics import load_contract
from app.agent.serving_bigquery import DevelopmentBigQueryPublisher
from app.agent.serving_publication import digest
from app.agent.serving_runtime import PilotPublication


def main():
    import fcntl

    from google.cloud import bigquery

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--expected-source", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument(
        "--dataset", required=True, help="Must be an isolated existing _dev dataset"
    )
    parser.add_argument("--reference-output", type=Path, required=True)
    parser.add_argument("--expected-reference-digest")
    parser.add_argument("--min-remaining-seconds", type=float, default=60.0)
    parser.add_argument(
        "--expanded",
        action="store_true",
        help="Certify all catalog metrics plus bound game detail",
    )
    args = parser.parse_args()
    import math

    if (
        not math.isfinite(args.min_remaining_seconds)
        or not 0 < args.min_remaining_seconds <= 300
    ):
        parser.error("--min-remaining-seconds must be in (0, 300]")
    if args.snapshot.stat().st_size > 50_000_000:
        parser.error("Source exceeds private capture size bound")
    snapshot, _ = load_snapshot(args.snapshot)
    now = datetime.now(timezone.utc)
    publication = PilotPublication.build(
        snapshot,
        expected_snapshot_id=args.expected_source,
        publication_id=uuid.uuid4().hex,
        relation_version=uuid.uuid4().hex,
        now=now,
        metrics=list(load_contract().metrics) if args.expanded else ["pts"],
        include_detail=args.expanded,
    )
    publisher = DevelopmentBigQueryPublisher(
        bigquery.Client(project=args.project),
        project=args.project,
        dataset=args.dataset,
    )
    reference = publisher.publish(publication, now=now)
    # Recheck freshness after the external load/readback, before promotion.
    remaining = promotion_lifetime(
        publication, datetime.now(timezone.utc), args.min_remaining_seconds
    )
    args.reference_output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with args.reference_output.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        previous = (
            json.loads(args.reference_output.read_text())
            if args.reference_output.exists()
            else None
        )
        if (digest(previous) if previous else None) != args.expected_reference_digest:
            raise ValueError("Reference changed; candidate retained without promotion")
        remaining = promotion_lifetime(
            publication, datetime.now(timezone.utc), args.min_remaining_seconds
        )
        history = args.reference_output.with_name(
            args.reference_output.name + ".history"
        )
        history.mkdir(exist_ok=True, mode=0o700)
        os.chmod(history, 0o700)
        # Bound only our local receipts; immutable warehouse tables use their TTL.
        prior_receipts = sorted(
            (
                p
                for p in history.glob("*.json")
                if not p.is_symlink()
                and p.is_file()
                and len(p.stem) == 32
                and all(c in "0123456789abcdef" for c in p.stem)
            ),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for stale in prior_receipts[19:]:
            stale.unlink()
        receipt_path = history / (uuid.uuid4().hex + ".json")
        receipt = dict(
            status="prepared",
            previous_reference=previous,
            candidate_reference=asdict(reference),
            remaining_seconds=remaining,
            built_at=publication.approval.built_at.isoformat(),
            expires_at=publication.approval.expires_at.isoformat(),
            cleanup="publisher-owned development table; automatic one-day table TTL",
        )
        with receipt_path.open("x") as stream:
            os.chmod(receipt_path, 0o600)
            json.dump(receipt, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temporary = args.reference_output.with_name(
            "." + uuid.uuid4().hex + ".reference"
        )
        with temporary.open("x") as stream:
            os.chmod(temporary, 0o600)
            json.dump(asdict(reference), stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        remaining = promotion_lifetime(
            publication, datetime.now(timezone.utc), args.min_remaining_seconds
        )
        os.replace(temporary, args.reference_output)
    print(
        json.dumps(
            dict(
                status="validated_development_reference",
                reference_file=str(args.reference_output),
                receipt_file=str(receipt_path),
                remaining_seconds=remaining,
            )
        )
    )
    return 0


def promotion_lifetime(publication, now, minimum=60.0):
    import math

    from app.agent.semantics import SemanticError

    if not math.isfinite(minimum) or not 0 < minimum <= 300:
        raise ValueError("Minimum publication lifetime must be in (0, 300]")
    publication.expanded_metadata(now)
    remaining = (publication.approval.expires_at - now).total_seconds()
    if remaining < minimum:
        raise SemanticError(
            "invalid_evidence", "Insufficient publication lifetime for promotion"
        )
    return remaining


if __name__ == "__main__":
    raise SystemExit(main())
