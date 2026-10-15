"""Summarize a private service JSONL export without retaining raw log content."""

import argparse
import json
from collections import Counter
from pathlib import Path


def summarize(records):
    requests, overview, counts, seen = set(), set(), Counter(), set()
    for event in records:
        if event.get("event_name") != "overview_capability_tracking":
            continue
        request = event.get("request_id")
        if not request:
            continue
        requests.add(request)
        key = (request, event.get("kind"), event.get("capability"), event.get("reason"))
        if key in seen:
            continue
        seen.add(key)
        if event.get("kind") == "overview_assessed":
            overview.add(request)
        else:
            counts[key[1:]] += 1
    return {
        "tracked_requests": len(requests),
        "overview_requests": len(overview),
        "counts": [
            {"kind": k[0], "capability": k[1], "reason": k[2], "requests": v}
            for k, v in sorted(counts.items())
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    args = parser.parse_args()
    with args.input.open() as stream:
        print(
            json.dumps(
                summarize(json.loads(line) for line in stream if line.strip()), indent=2
            )
        )
