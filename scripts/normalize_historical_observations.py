"""Normalize retained NBA game bundles offline. No fetchers or runtime settings."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.data.unified.identity import require  # noqa: E402
from app.data.unified.store import ObservationStore, parse_json  # noqa: E402


def run(bundle, store_path, output, *, allow_held=False):
    bundle = Path(bundle).resolve()
    store_path = Path(store_path).resolve()
    output = Path(output).resolve()
    require(
        store_path.is_relative_to(ROOT / "reports")
        and output.is_relative_to(ROOT / "reports"),
        "Offline state and reports must remain in private reports/",
    )
    require(
        store_path.suffix == ".sqlite" and not output.exists(),
        "Use an explicit SQLite store and a new output directory",
    )
    require(
        not store_path.is_relative_to(bundle),
        "Store must not modify the retained input directory",
    )
    require(
        not store_path.is_relative_to(output),
        "Store must live outside the new output directory",
    )
    manifest_path = bundle / "manifest.json"
    require(
        manifest_path.resolve().is_relative_to(bundle)
        and manifest_path.resolve().suffix == ".json"
        and manifest_path.is_file()
        and manifest_path.stat().st_size <= 1024 * 1024,
        "Invalid input manifest",
    )
    manifest = parse_json(manifest_path.read_bytes())
    require(
        set(manifest) == {"schema_version", "captures"}
        and manifest["schema_version"]
        in ("nba_archive_bundle/1", "unified_retained_bundle/1"),
        "Input manifest version/fields differ",
    )
    captures = manifest["captures"]
    require(
        isinstance(captures, list) and 0 < len(captures) <= 1500,
        "Invalid offline game batch bound",
    )
    inputs = []
    total = 0
    seen = set()
    for capture in captures:
        require(
            set(capture)
            in (
                {"path", "sha256", "captured_at", "kind"},
                {"path", "sha256", "captured_at", "kind", "expected_revision"},
            ),
            "Capture fields differ",
        )
        path = (bundle / capture["path"]).resolve()
        require(
            not Path(capture["path"]).is_absolute()
            and path.suffix == ".json"
            and path.is_relative_to(bundle)
            and path.is_file()
            and path.stat().st_size <= 1024 * 1024,
            "Invalid local capture path/size",
        )
        require(path not in seen, "Duplicate input capture")
        seen.add(path)
        body = path.read_bytes()
        total += len(body)
        require(total <= 100_000_000, "Offline input byte limit exceeded")
        payload = parse_json(body)
        receipt = {k: capture[k] for k in ("sha256", "captured_at", "kind")}
        receipt["reference"] = capture["path"]
        require(
            hashlib.sha256(body).hexdigest() == receipt["sha256"],
            "Retained input checksum differs",
        )
        inputs.append(
            (
                body,
                receipt,
                capture.get("expected_revision"),
                (
                    payload["provider"],
                    payload["game"].get(
                        "game_id", payload["game"].get("source_game_id")
                    ),
                ),
            )
        )
    require(
        len({item[3] for item in inputs}) == len(inputs),
        "Duplicate game in offline batch",
    )
    output.mkdir(parents=True, mode=0o700)
    store_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    store = ObservationStore(store_path)
    try:
        with store.atomic():
            results, held = [], []
            for body, receipt, expected, key in inputs:
                try:
                    results.append(
                        store.ingest_bytes(body, receipt, expected_revision=expected)
                    )
                except (ValueError, KeyError, TypeError) as exc:
                    if not allow_held:
                        raise
                    # Only a complete rejected game is retained; ingest's savepoint
                    # has already rolled back any partial identity/fact mutation.
                    reason = str(exc)[:512] or type(exc).__name__
                    qid = store.quarantine_bytes(body, receipt, reason)
                    held.append(
                        {
                            "provider": key[0],
                            "source_game_id": key[1],
                            "input_id": qid,
                            "reason": reason,
                        }
                    )
        observations = [
            store.read_observation(row["observation_id"]) for row in results
        ]
        (output / "identity-catalog.json").write_text(
            json.dumps(store.catalog(), indent=2) + "\n"
        )
        (output / "observations.jsonl").write_text(
            "".join(
                json.dumps(row, sort_keys=True, allow_nan=False) + "\n"
                for row in observations
            )
        )
        report = {
            "schema_version": "offline_historical_report/1",
            "mode": "offline",
            "games": len(results),
            "input_games": len(inputs),
            "held_games": len(held),
            "held": held,
            "appearances": sum(
                len(row["semantic"]["appearances"]) for row in observations
            ),
            "recorded": sum(row["status"] == "recorded" for row in results),
            "replaced": sum(row["status"] == "replaced" for row in results),
            "noops": sum(row["status"] == "noop" for row in results),
            "observed_sample_games": sum(
                row["membership_level"] == "observed_sample" for row in results
            ),
            "runtime_eligible": False,
            "results": results,
        }
        (output / "result.json").write_text(json.dumps(report, indent=2) + "\n")
        return report
    except Exception as exc:
        (output / "result.json").write_text(
            json.dumps(
                {
                    "mode": "offline",
                    "status": "rejected",
                    "error_type": type(exc).__name__,
                    "runtime_eligible": False,
                }
            )
            + "\n"
        )
        raise
    finally:
        store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run(args.bundle, args.store, args.output)
    print(json.dumps({k: v for k, v in result.items() if k != "results"}))


if __name__ == "__main__":
    main()
