"""Build a private local development publication from a captured source.

No warehouse writes, model calls, deployment, scheduler or route activation.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from app.agent.serving_store import DevelopmentPublications


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--expected-source", required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--expected-generation")
    args = parser.parse_args()
    store = DevelopmentPublications(args.directory)
    # Bounded read uses the same local artifact byte ceiling.
    snapshot = store._read(args.snapshot)
    generation = store.publish(
        snapshot,
        expected_snapshot_id=args.expected_source,
        expected_generation=args.expected_generation,
        now=datetime.now(timezone.utc),
    )
    print(json.dumps(dict(generation=generation, mode="development_only")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
