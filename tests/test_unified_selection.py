"""Offline ledger selection never implies completeness or provider permission."""

import copy
import hashlib
import json
import socket
import sqlite3
from pathlib import Path

import pytest
from app.data.unified.identity import ContractError, digest
from app.data.unified.ledger import LedgerStore, record
from app.data.unified.selection import pending_check, policy_record, select
from app.data.unified.snapshot import snapshot
from app.data.unified.store import ObservationStore
from jsonschema import ValidationError
from scripts import select_unified_sources as cli
from tests.support.historical import ingest, payload, receipt

from test_unified_date_review import reviewed

AS_OF = "2026-10-30T06:00:00Z"


def requested(season="2026-27"):
    return {
        "season": season,
        "phase": "Regular Season",
        "first": season[:4] + "-10-01",
        "last": season[:4] + "-10-29",
    }


def ref(provider="espn", season="2026-27", number=1, uid=None, day=None):
    return {
        "game_uid": uid or "g_" + format(int(season[:4]) * 1000000 + number, "032x"),
        "source_game_id": str(401000000 + number)
        if provider == "espn"
        else "002" + season[2:4] + format(number, "05d"),
        "provider": provider,
        "scope": {
            "league": "nba",
            "season": season,
            "phase": "Regular Season",
            "date": day or season[:4] + "-10-22",
            "home": "NYK",
            "away": "BOS",
        },
        "fact_revision": digest([provider, season, number]),
        "observation_id": digest([provider, season, number, "observation"]),
        "membership": "observed_sample",
        "appearances": 3,
        "capture_times": [],
    }


def inventory(refs, holds=()):
    return record(
        "inventory",
        {
            "version": "observed_source_inventory/1",
            "source_snapshot": receipt(),
            "identity_catalog_version": "a" * 64,
            "observations": sorted(
                refs, key=lambda r: (r["scope"]["season"], r["provider"], r["game_uid"])
            ),
            "unresolved_inputs": list(holds),
            "expected_universe": "not_established",
            "runtime_eligible": False,
        },
    )


def test_policy_is_one_provider_per_season_with_no_override():
    policy = policy_record()
    value = inventory([ref("nba_api", "2025-26"), ref()])
    one = select(value, policy, [requested(), requested("2025-26")], AS_OF)
    two = select(
        value, policy, [requested("2025-26"), requested()], "2026-10-30T06:00:00+00:00"
    )
    assert one == two
    assert [s["provider"] for s in one["payload"]["scopes"]] == ["nba_api", "espn"]
    assert one["payload"]["status"] == "selected_observed"
    assert not any(one["payload"]["capabilities"].values())
    assert all(s["validated_cutoff"] is None for s in one["payload"]["scopes"])
    changed = copy.deepcopy(policy["payload"])
    changed["allow_fallback"] = True
    with pytest.raises(ValidationError):
        record("policy", changed)


def test_more_complete_alternate_source_cannot_fill_primary_gaps():
    primary = ref()
    # Same UID is an explicit identity link; select one provider once.
    alternate = ref("nba_api", number=2, uid=primary["game_uid"])
    extra = ref("nba_api", number=3, day="2026-10-24")
    result = select(
        inventory([primary, alternate, extra]), policy_record(), [requested()], AS_OF
    )["payload"]
    assert result["status"] == "selected_observed"
    assert result["scopes"][0]["selected"] == [primary["observation_id"]]
    assert len(result["scopes"][0]["excluded_other_provider"]) == 2
    assert result["scopes"][0]["observed_games"] == 1


def test_missing_primary_refuses_mixed_request_without_fallback():
    value = inventory([ref("nba_api", "2025-26"), ref("nba_api", "2026-27", 2)])
    result = select(value, policy_record(), [requested("2025-26"), requested()], AS_OF)[
        "payload"
    ]
    assert result["status"] == "refused"
    current = result["scopes"][1]
    assert current["selected"] == [] and "missing_primary_source" in current["reasons"]
    assert len(current["excluded_other_provider"]) == 1


def test_unlinked_provider_overlap_is_held_without_merging():
    value = inventory([ref(), ref("nba_api", number=2)])
    result = select(value, policy_record(), [requested()], AS_OF)["payload"]
    assert result["status"] == "held"
    assert "unlinked_provider_overlap" in result["scopes"][0]["reasons"]


def test_unresolved_date_is_not_used_as_exclusion_evidence():
    hold = {
        "input_id": "a" * 64,
        "provider": "espn",
        "source_game_id": "400000001",
        "season": "2026-27",
        "phase": "Regular Season",
        "date": "2027-04-01",
        "reason": "Date conflict",
    }
    result = select(inventory([ref()], [hold]), policy_record(), [requested()], AS_OF)[
        "payload"
    ]
    assert (
        result["status"] == "held"
        and "unresolved_input" in result["scopes"][0]["reasons"]
    )


def test_future_capture_refuses_and_unknown_archive_clock_does_not_become_fresh():
    future = ref()
    future["capture_times"] = ["2026-10-31T00:00:00+00:00"]
    result = select(inventory([future]), policy_record(), [requested()], AS_OF)[
        "payload"
    ]
    assert result["status"] == "refused" and result["scopes"][0]["selected"] == []
    assert "capture_after_selection_as_of" in result["scopes"][0]["reasons"]
    historical = inventory([ref("nba_api", "2025-26")])
    chosen = select(historical, policy_record(), [requested("2025-26")], AS_OF)
    assert chosen["payload"]["coverage_evaluation"] == "not_evaluated"
    assert historical["payload"]["source_snapshot"]["captured_at"] is None


@pytest.mark.parametrize(
    "mode",
    [
        "duplicate",
        "native_alias",
        "naive_clock",
        "bad_scope",
        "duplicate_scope",
        "future_range",
    ],
)
def test_invalid_inventory_or_request_cannot_issue_decision(mode):
    refs = [ref()]
    scopes = [requested()]
    clock = AS_OF
    if mode == "duplicate":
        refs.append(copy.deepcopy(refs[0]))
    elif mode == "native_alias":
        refs[0]["source_game_id"] = "not-a-game"
    elif mode == "naive_clock":
        clock = "2026-10-30T06:00:00"
    elif mode == "bad_scope":
        scopes[0]["phase"] = "Playoffs"
    elif mode == "duplicate_scope":
        scopes.append(copy.deepcopy(scopes[0]))
    elif mode == "future_range":
        scopes[0]["last"] = "2027-04-01"
    with pytest.raises(ValueError):
        select(inventory(refs), policy_record(), scopes, clock)


def test_append_only_dependency_bound_ledger_and_no_false_check_passes(tmp_path):
    store = LedgerStore(tmp_path / "ledger.sqlite")
    inv, policy = inventory([ref()]), policy_record()
    decision = select(inv, policy, [requested()], AS_OF)
    check = pending_check(decision, "pdf", requested(), "PDF evaluator pending")
    with store.atomic():
        assert [store.append(v) for v in (policy, inv, decision, check)] == [
            "recorded"
        ] * 4
    assert [store.append(v) for v in (policy, inv, decision, check)] == ["noop"] * 4
    assert store.read(check["record_id"]) == check
    assert (
        store.connection.execute("SELECT COUNT(*) FROM ledger_records").fetchone()[0]
        == 4
    )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        store.connection.execute("DELETE FROM ledger_records")
    forged = copy.deepcopy(decision["payload"])
    forged["scopes"][0]["provider"] = "nba_api"
    value = record("selection", forged, decision["depends_on"])
    with pytest.raises(ContractError, match="recomputation"):
        store.append(value)
    forged_check = copy.deepcopy(check["payload"])
    forged_check["state"] = "matched"
    with pytest.raises(ValidationError):
        record("pending_check", forged_check, check["depends_on"])
    other = LedgerStore()
    with pytest.raises(ContractError, match="Unknown ledger dependency"):
        other.append(decision)
    other.close()
    store.close()


def test_corrupted_dependency_is_detected_on_read():
    store = LedgerStore()
    inv, policy = inventory([ref()]), policy_record()
    decision = select(inv, policy, [requested()], AS_OF)
    for v in (policy, inv, decision):
        store.append(v)
    store.connection.execute("DROP TRIGGER immutable_ledger_records_UPDATE")
    store.connection.execute(
        "UPDATE ledger_records SET payload='{}' WHERE record_id=?", (inv["record_id"],)
    )
    with pytest.raises((ContractError, ValidationError)):
        store.read(decision["record_id"])


def source_store(tmp_path):
    path = tmp_path / "source.sqlite"
    store = ObservationStore(path)
    value = reviewed()
    qid = store.quarantine_bytes(
        value["original_body"].encode(),
        value["original_receipt"],
        "Appearance scope differs",
    )
    result = ingest(store, value)
    store.resolve_date_quarantine(qid, result["observation_id"])
    store.close()
    return path


def test_readonly_snapshot_preserves_bytes_mode_and_does_not_create_sidecars(
    tmp_path, monkeypatch
):
    source = source_store(tmp_path)
    body = source.read_bytes()
    source.chmod(0o400)
    before = set(tmp_path.iterdir())
    monkeypatch.setattr(
        socket.socket,
        "connect",
        lambda *a, **k: pytest.fail("network in offline selection"),
    )
    value = snapshot(source)
    assert value["payload"]["unresolved_inputs"] == []
    assert len(value["payload"]["observations"]) == 1
    assert (
        value["payload"]["source_snapshot"]["sha256"]
        == hashlib.sha256(body).hexdigest()
    )
    assert source.read_bytes() == body and source.stat().st_mode & 0o777 == 0o400
    assert set(tmp_path.iterdir()) == before
    assert snapshot(source) == value


def test_cli_replay_uses_separate_ledger_and_marks_pending_evaluators(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    source = source_store(tmp_path)
    before = source.read_bytes()
    path = tmp_path / "reports/ledger.sqlite"
    first = cli.run(
        source, path, tmp_path / "reports/first", [requested("2025-26")], AS_OF
    )
    second = cli.run(
        source, path, tmp_path / "reports/second", [requested("2025-26")], AS_OF
    )
    assert first["recorded"] == 6 and second["noops"] == 6
    assert first["selection_id"] == second["selection_id"]
    assert (
        not first["runtime_eligible"]
        and first["coverage_evaluation"] == "not_evaluated"
    )
    assert source.read_bytes() == before
    with pytest.raises(ContractError):
        cli.run(
            source, source, tmp_path / "reports/wrong", [requested("2025-26")], AS_OF
        )


def test_integrity_failure_never_creates_a_ledger_or_selects_an_alternate(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    path = tmp_path / "source.sqlite"
    store = ObservationStore(path)
    ingest(store, payload())
    store.connection.execute("UPDATE current_games SET revision='missing'")
    store.close()
    ledger = tmp_path / "reports/ledger.sqlite"
    with pytest.raises(ContractError, match="missing facts"):
        cli.run(
            path, ledger, tmp_path / "reports/result", [requested("2025-26")], AS_OF
        )
    assert not ledger.exists() and not (tmp_path / "reports/result").exists()


def test_original_observation_db_is_not_a_ledger_destination(tmp_path):
    path = source_store(tmp_path)
    before = path.read_bytes()
    with pytest.raises(ContractError, match="another application"):
        LedgerStore(path)
    assert path.read_bytes() == before


def test_forged_pending_check_cannot_claim_another_observation():
    store = LedgerStore()
    inv, policy = inventory([ref()]), policy_record()
    decision = select(inv, policy, [requested()], AS_OF)
    for value in (policy, inv, decision):
        store.append(value)
    check = pending_check(decision, "coverage", requested(), "Pending")
    changed = copy.deepcopy(check["payload"])
    changed["observation_ids"] = ["f" * 64]
    with pytest.raises(ContractError, match="scope binding"):
        store.append(record("pending_check", changed, check["depends_on"]))


def test_failed_batch_rolls_back_policy_and_inventory():
    store = LedgerStore()
    inv, policy = inventory([ref()]), policy_record()
    decision = select(inv, policy, [requested()], AS_OF)
    changed = copy.deepcopy(decision["payload"])
    changed["scopes"][0]["observed_games"] = 999
    with pytest.raises(ContractError, match="recomputation"):
        with store.atomic():
            store.append(policy)
            store.append(inv)
            store.append(record("selection", changed, decision["depends_on"]))
    assert (
        store.connection.execute("SELECT COUNT(*) FROM ledger_records").fetchone()[0]
        == 0
    )


def test_unresolved_source_inputs_remain_visible_in_readonly_inventory(tmp_path):
    path = source_store(tmp_path)
    store = ObservationStore(path)
    value = payload("0022500002", "2025-10-24")
    body = json.dumps(value).encode()
    store.quarantine_bytes(body, receipt(body), "Identity scope not resolved")
    store.close()
    result = select(snapshot(path), policy_record(), [requested("2025-26")], AS_OF)[
        "payload"
    ]
    assert (
        result["status"] == "held" and len(result["scopes"][0]["relevant_holds"]) == 1
    )


def test_corrupt_resolution_cannot_hide_a_quarantine(tmp_path):
    path = source_store(tmp_path)
    store = ObservationStore(path)
    store.connection.execute("DROP TRIGGER immutable_quarantine_resolutions_UPDATE")
    store.connection.execute(
        "UPDATE quarantine_resolutions SET review_sha256=?", ("f" * 64,)
    )
    store.close()
    with pytest.raises(ContractError, match="resolution binding"):
        snapshot(path)


def test_live_wal_input_is_refused_without_reading_or_mutating_source(tmp_path):
    path = source_store(tmp_path)
    before = path.read_bytes()
    Path(str(path) + "-wal").write_bytes(b"not a retained checkpoint")
    with pytest.raises(ContractError, match="Checkpoint"):
        snapshot(path)
    assert path.read_bytes() == before


@pytest.mark.parametrize("mode", ["conflicting_uid_scope", "duplicate_provider_scope"])
def test_canonical_scope_conflicts_cannot_duplicate_contributions(mode):
    first = ref()
    if mode == "conflicting_uid_scope":
        second = ref("nba_api", "2025-26", uid=first["game_uid"])
    else:
        second = ref(number=2)
    with pytest.raises(ContractError):
        select(inventory([first, second]), policy_record(), [requested()], AS_OF)
