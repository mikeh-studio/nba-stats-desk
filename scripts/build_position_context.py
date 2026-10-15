"""Build a private reviewed position bundle from retained source files.

Run with --manifest and --output. Manifest paths resolve beside the manifest.
Never writes to the warehouse or downloads data. See docs/overview-insights.md.
"""

import argparse
import gzip
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.agent.historical_positions import make_context, roster_records  # noqa: E402
from app.agent.overview_positions import validated_context  # noqa: E402


def build(manifest, root):
    records = []
    seasons = set()
    for source in manifest["archives"]:
        data = (root / source["path"]).read_bytes()
        if hashlib.sha256(data).hexdigest() != source["sha256"]:
            raise ValueError("Source checksum mismatch")
        season = source["season"]
        if season in seasons:
            raise ValueError("Duplicate season archive")
        seasons.add(season)
        records.extend(
            roster_records(
                json.loads(gzip.decompress(data)),
                season,
                source["url"],
                source["sha256"],
            )
        )
    if manifest.get("supplements"):
        source = manifest["supplements"]
        data = (root / source["path"]).read_bytes()
        if hashlib.sha256(data).hexdigest() != source["sha256"]:
            raise ValueError("Supplement checksum mismatch")
        records.extend(json.loads(data))
    if set(manifest["reviewed_seasons"]) != seasons or any(
        r["season"] not in seasons for r in records
    ):
        raise ValueError("Review does not match captured seasons")
    context = make_context(records, seasons)
    positions, approved = validated_context(context)
    if (
        len(positions) != len(records)
        or any(v is None for v in positions.values())
        or approved != seasons
    ):
        raise ValueError("Invalid, duplicate or unreviewed position evidence")
    return context


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = build(json.loads(args.manifest.read_text()), args.manifest.parent)
    payload = json.dumps(result, separators=(",", ":"))
    if len(payload.encode()) > 1_000_000:
        raise ValueError("Position bundle exceeds serving size limit")
    # Create-only: failed refreshes never replace an approved serving bundle.
    with args.output.open("x") as output:
        output.write(payload + "\n")
    args.output.chmod(0o600)
    print(f"Validated {len(result['records'])} season/player records")


if __name__ == "__main__":
    main()
