"""Opt-in real BigQuery transactions with synthetic delayed ESPN corrections."""

import argparse
import copy
import json
import sys
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
from dotenv import load_dotenv  # noqa: E402
from scripts.espn_corrections import normalize, revision  # noqa: E402
from scripts.espn_layer_ingestion import ingest_game, make_store  # noqa: E402

from test_espn_corrections import corrected_box, corrected_pbp, inputs  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    assert (
        args.output.resolve().is_relative_to(ROOT / "reports")
        and not args.output.exists()
    )
    args.output.mkdir(parents=True)
    load_dotenv(ROOT / ".env")
    store = make_store(
        args.output / "cloud", "nba_gamebook_dev_espn_test_" + uuid4().hex[:12]
    )
    game, original = inputs()
    store.register_games([game])
    evidence = store.cloud.upload(b'{"synthetic":true}', "espn_layer_test", "json")
    results = []

    def state():
        return store.coverage(game["season"])[0]

    def count_edges():
        return store.cloud.query(
            f"SELECT COUNT(*) n FROM `{store.prefix}.espn_layer_validated_assists`"
        )[0]["n"]

    def check(name, payload, observed, expected, game_override=None):
        result = ingest_game(
            store,
            payload,
            game_override or game,
            evidence,
            observed,
            args.output / name,
        )
        assert [r["status"] for r in result["layers"]] == expected, (name, result)
        snapshot = state()
        results.append({"name": name, "result": result, "state": snapshot})
        (args.output / f"progress-{len(results)}.json").write_text(
            json.dumps(results[-1], indent=2, default=str)
        )
        print(
            "PASS",
            name,
            snapshot["result_status"],
            snapshot["box_status"],
            snapshot["pbp_status"],
            flush=True,
        )
        return snapshot

    first = check("initial", original, "2025-10-23T04:00:00+00:00", ["published"] * 3)
    assert count_edges() == 1
    replay = check(
        "identical_replay", original, "2025-10-23T04:00:00+00:00", ["noop"] * 3
    )
    assert first["last_checked_at"] == replay["last_checked_at"]
    assert first["last_changed_at"] == replay["last_changed_at"]
    box = corrected_box(original)
    pending = check(
        "box_corrected_before_pbp",
        box,
        "2025-10-24T16:00:00+00:00",
        ["noop", "published", "rejected"],
    )
    assert pending["result_status"] == pending["box_status"] == "accepted"
    assert pending["pbp_status"] == "stale" and count_edges() == 0
    assert pending["pbp_revision"] == first["pbp_revision"]
    final = corrected_pbp(box)
    corrected = check(
        "delayed_pbp_catches_up",
        final,
        "2025-10-25T16:00:00+00:00",
        ["noop", "noop", "published"],
    )
    assert corrected["pbp_status"] == "accepted" and count_edges() == 1
    edges = store.cloud.query(
        f"SELECT assister_source_id FROM `{store.prefix}.espn_layer_validated_assists`"
    )
    assert edges == [{"assister_source_id": "102"}]
    invalid = copy.deepcopy(final)
    invalid["boxscore"]["players"][0]["statistics"][0]["athletes"][0]["stats"][1] = "99"
    failed = check(
        "invalid_box_replacement",
        invalid,
        "2025-10-26T16:00:00+00:00",
        ["noop", "rejected", "rejected"],
    )
    assert (
        failed["result_status"] == "accepted"
        and failed["box_revision"] == corrected["box_revision"]
    )
    assert failed["pbp_revision"] == corrected["pbp_revision"] and count_edges() == 0
    retained = check(
        "old_replay_does_not_clear_failure",
        final,
        "2025-10-25T16:00:00+00:00",
        ["noop", "noop", "rejected"],
    )
    assert retained["box_health"] == "failed"
    recovered = check(
        "fresh_unchanged_capture_recovers",
        final,
        "2025-10-27T16:00:00+00:00",
        ["noop"] * 3,
    )
    assert recovered["pbp_status"] == "accepted" and count_edges() == 1
    edited = copy.deepcopy(final)
    edited["plays"][0]["clock"]["displayValue"] = "10:58"
    candidate = normalize(edited, game, "pbp")
    rollback = store.publish(
        candidate, evidence, "2025-10-28T16:00:00+00:00", inject_failure=True
    )
    assert rollback["status"] == "rejected"
    assert state()["pbp_revision"] == corrected["pbp_revision"]
    assert not store.cloud.query(
        f"SELECT revision FROM `{store.prefix}.espn_layer_revisions` WHERE revision=@revision",
        [store.cloud.parameter("revision", revision(candidate))],
    )
    results.append({"name": "transaction_rollback", "result": rollback})
    # A retry of the exact capture after a confirmed transaction failure is safe.
    retry = store.publish(candidate, evidence, "2025-10-28T16:00:00+00:00")
    assert retry["status"] == "published" and state()["pbp_status"] == "accepted"
    results.append({"name": "retry_failed_transaction", "result": retry})
    stale = store.publish(
        normalize(final, game, "pbp"), evidence, "2025-10-25T16:00:00+00:00"
    )
    assert stale["status"] == "rejected" and state()["pbp_revision"] == revision(
        candidate
    )
    assert state()["pbp_status"] == "accepted"
    results.append({"name": "old_capture_cannot_roll_back", "result": stale})
    # A genuine newer provider reversion is distinguishable from frozen replay.
    reverted = store.publish(
        normalize(final, game, "pbp"), evidence, "2025-10-29T16:00:00+00:00"
    )
    assert (
        reverted["status"] == "published"
        and state()["pbp_revision"] == corrected["pbp_revision"]
    )
    results.append({"name": "fresh_provider_reversion", "result": reverted})
    # A changed final is accepted even while player rows have not caught up.
    score = copy.deepcopy(final)
    score["header"]["competitions"][0]["competitors"][0]["score"] = "4"
    newer_game = {**game, "home_score": 4}
    changed_score = check(
        "result_changes_before_box",
        score,
        "2025-10-30T16:00:00+00:00",
        ["published", "rejected", "rejected"],
        newer_game,
    )
    assert (
        changed_score["home_score"] == 4
        and changed_score["box_status"] == "stale"
        and count_edges() == 0
    )
    report = {
        "dataset": store.prefix,
        "scope": "Synthetic source changes; actual GCS evidence and BigQuery publication transactions",
        "scenarios": results,
        "final_state": state(),
    }
    (args.output / "results.json").write_text(
        json.dumps(report, indent=2, default=str) + "\n"
    )
    print("PASS", len(results), "cloud scenarios", store.prefix)


if __name__ == "__main__":
    main()
