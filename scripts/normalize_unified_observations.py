"""Replay retained NBA/ESPN bundles locally, optionally retaining held games."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.normalize_historical_observations import run  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--allow-held",
        action="store_true",
        help="Retain rejected whole games in quarantine; report every hold",
    )
    args = parser.parse_args()
    result = run(args.bundle, args.store, args.output, allow_held=args.allow_held)
    print(json.dumps({k: v for k, v in result.items() if k not in ("results", "held")}))
    # A held game is an explicit incomplete run, including when accepted games
    # were committed. Scheduler callers must not interpret quarantine as green.
    return 2 if result["held_games"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
