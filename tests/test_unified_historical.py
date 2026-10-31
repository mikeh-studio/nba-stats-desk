"""Synthetic historical evidence; these fixtures do not certify live coverage."""

import copy
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from app.data.unified.identity import ContractError, IdentityRegistry, canonical, digest
from app.data.unified.store import ObservationStore
from jsonschema import Draft202012Validator, FormatChecker
from tests.support.historical import complete, ingest, payload, receipt


def test_observed_baseline_preserves_null_zero_overtime_and_source_time():
    store = ObservationStore()
    result = ingest(store, payload())
    obs = store.read_observation(result["observation_id"])
    zero = next(p for p in obs["semantic"]["appearances"] if p["player_id"] == 12)
    assert result["membership_level"] == "observed_sample"
    assert obs["semantic"]["result"]["is_overtime"] is None
    assert zero["minutes"] == {
        "value": 0.0,
        "precision": "whole_minute_reported",
        "zero_origin": "unverified_legacy_zero",
    }
    assert zero["stats"]["ast"] is None
    assert zero["played"] and store.player_summary(12)["games"] == 1
    assert obs["provenance"]["captured_at"] is None
    assert not any(obs["semantic"]["capabilities"].values())


def test_identical_replay_is_noop_after_reopening(tmp_path):
    path = tmp_path / "registry.sqlite"
    store = ObservationStore(path)
    first = ingest(store, payload())
    store.close()
    store = ObservationStore(path)
    second = ingest(store, payload())
    assert (
        first["game_uid"] == second["game_uid"]
        and first["revision"] == second["revision"]
    )
    assert second["status"] == "noop"
    assert (
        store.connection.execute("SELECT COUNT(*) FROM observation_events").fetchone()[
            0
        ]
        == 1
    )
    assert (
        store.connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 1
    )
    assert store.player_summary(10)["totals"]["pts"] == 4


def test_new_capture_time_changes_evidence_not_facts_or_original_time():
    store = ObservationStore()
    value = complete(payload())
    first = ingest(store, value, captured_at="2025-10-23T10:00:00+00:00")
    second = ingest(store, value, captured_at="2025-10-24T10:00:00+00:00")
    assert second["revision"] == first["revision"] and second["status"] == "noop"
    assert second["observation_id"] != first["observation_id"]
    assert (
        store.read_observation(first["observation_id"])["provenance"]["captured_at"]
        == "2025-10-23T10:00:00+00:00"
    )


def test_row_order_does_not_change_statistical_revision():
    store = ObservationStore()
    value = payload()
    first = ingest(store, value)
    value["appearances"].reverse()
    second = ingest(store, value)
    assert first["revision"] == second["revision"]


def test_load_time_is_not_capture_time_or_statistical_content():
    store = ObservationStore()
    value = payload()
    value["appearances"][0]["ingested_at_utc"] = "2025-10-23T10:00:00Z"
    first = ingest(store, value)
    value["appearances"][0]["ingested_at_utc"] = "2026-10-29T10:00:00Z"
    second = ingest(store, value)
    assert first["revision"] == second["revision"]
    assert (
        store.read_observation(second["observation_id"])["provenance"]["captured_at"]
        is None
    )


def test_unknown_metric_and_minutes_are_not_zero_filled():
    store = ObservationStore()
    value = payload()
    value["appearances"][0].update(dreb=None, min=None)
    result = ingest(store, value)
    player = store.read_observation(result["observation_id"])["semantic"][
        "appearances"
    ][0]
    assert player["stats"]["dreb"] is None and player["minutes"]["value"] is None
    assert "dreb" not in store.player_summary(10)["totals"]


def test_correction_requires_membership_expected_revision_and_preserves_history():
    store = ObservationStore()
    original = complete(payload())
    first = ingest(store, original)
    changed = payload()
    changed["appearances"][0]["fgm"] = 3
    changed["appearances"][0]["fga"] = 3
    changed["appearances"][0]["pts"] = 6
    changed["game"]["home_team_pts"] = 6
    with pytest.raises(ContractError, match="expected prior"):
        ingest(store, complete(changed))
    with pytest.raises(ContractError, match="participant evidence"):
        ingest(store, changed, expected=first["revision"])
    second = ingest(store, complete(changed), expected=first["revision"])
    assert second["status"] == "replaced" and second["game_uid"] == first["game_uid"]
    assert store.player_summary(10)["totals"]["pts"] == 6
    assert (
        store.read_observation(first["observation_id"])["semantic"]["result"][
            "home_score"
        ]
        == 4
    )
    with pytest.raises(ContractError, match="Historical revision replay"):
        ingest(store, original, expected=second["revision"])


def test_zero_point_omission_fails_despite_unchanged_score_totals():
    store = ObservationStore()
    original = complete(payload())
    first = ingest(store, original)
    bad = copy.deepcopy(original)
    bad["appearances"] = [p for p in bad["appearances"] if p["player_id"] != 12]
    bad["membership"]["input_game_hash"] = digest(
        {k: v for k, v in bad.items() if k != "membership"}
    )
    with pytest.raises(ContractError, match="participant membership"):
        ingest(store, bad, expected=first["revision"])
    assert (
        store.player_summary(12)["games"] == 1
        and store.current_revision(first["game_uid"]) == first["revision"]
    )


def test_reviewed_complete_replacement_can_retract_an_appearance():
    store = ObservationStore()
    first = ingest(store, complete(payload()))
    changed = payload()
    changed["appearances"] = [p for p in changed["appearances"] if p["player_id"] != 12]
    result = ingest(store, complete(changed), expected=first["revision"])
    assert result["status"] == "replaced" and store.player_summary(12)["games"] == 0


@pytest.mark.parametrize(
    "key,value",
    [
        ("pts", float("nan")),
        ("fgm", -1),
        ("ast", 0.5),
        ("reb", True),
        ("dreb", 10**400),
        ("min", 0.1),
        ("min", float("inf")),
    ],
)
def test_invalid_numbers_reject_entire_game(key, value):
    store = ObservationStore()
    bad = payload()
    bad["appearances"][0][key] = value
    body = json.dumps(bad).encode()
    with pytest.raises(ValueError):
        store.ingest_bytes(body, receipt(body))
    assert store.connection.execute("SELECT COUNT(*) FROM games").fetchone()[0] == 0


@pytest.mark.parametrize(
    "mutation",
    [
        lambda p: p["appearances"].append(copy.deepcopy(p["appearances"][0])),
        lambda p: p["appearances"][0].update(game_id="0022509999"),
        lambda p: p["appearances"][0].update(home_away="AWAY"),
        lambda p: p["appearances"][0].update(reb=2),
        lambda p: p.update(provider="espn"),
        lambda p: p.update(phase="Playoffs"),
        lambda p: p.update(season="2026-27"),
    ],
)
def test_invalid_scope_membership_or_arithmetic_is_atomic(mutation):
    store = ObservationStore()
    bad = payload()
    mutation(bad)
    with pytest.raises(ValueError):
        ingest(store, bad)
    assert (
        store.connection.execute("SELECT COUNT(*) FROM game_aliases").fetchone()[0] == 0
    )


def test_same_name_and_trade_do_not_rekey_people_or_duplicate_games():
    store = ObservationStore()
    first = ingest(store, payload())
    second = payload("0022500002", "2025-11-01")
    second["appearances"][0].update(player_id=11)
    second["appearances"][1].update(player_id=10)
    ingest(store, second)
    log = store.player_log(10)
    assert len(log) == 2 and [p["team_at_game"] for p in log] == ["NYK", "BOS"]
    assert first["game_uid"] != log[1]["game_uid"]


def test_provider_overlap_requires_reviewed_alias_and_does_not_rekey_facts():
    store = ObservationStore()
    first = ingest(store, payload())
    game_scope = store.game_scope(first["game_uid"])[0]
    with pytest.raises(ContractError, match="overlapping"):
        store.register_game("espn", "123456789", game_scope, receipt())
    store.attach_game_alias(
        first["game_uid"], "espn", "123456789", game_scope, receipt()
    )
    assert store.lookup("espn", "123456789") == first["game_uid"]
    assert store.player_summary(10)["games"] == 1
    with pytest.raises(ContractError, match="requires reviewed"):
        store.attach_game_alias(
            first["game_uid"],
            "espn",
            "123456788",
            game_scope,
            receipt(kind="archive_snapshot"),
        )


def test_reschedule_preserves_uid_and_old_observation_scope():
    store = ObservationStore()
    first = ingest(store, payload())
    uid = first["game_uid"]
    old, version = store.game_scope(uid)
    revised = dict(old, date="2025-10-23")
    store.revise_scope(uid, revised, version, receipt())
    assert store.register_game("nba_api", "0022500001", revised, receipt()) == uid
    assert (
        store.read_observation(first["observation_id"])["semantic"]["scope"]["date"]
        == "2025-10-22"
    )
    with pytest.raises(ContractError, match="Stale scope"):
        store.revise_scope(uid, revised, version, receipt())
    with pytest.raises(ContractError, match="overlapping"):
        store.register_game("espn", "123456789", old, receipt())


def test_player_alias_conflict_never_uses_names():
    registry = IdentityRegistry()
    registry.attach_person_alias("espn", "123", 10, receipt())
    with pytest.raises(ContractError, match="Conflicting player alias"):
        registry.attach_person_alias("espn", "123", 11, receipt())
    with pytest.raises(ContractError, match="NBA person identity differs"):
        registry.attach_person_alias("nba_api", "10", 11, receipt())


def test_same_native_game_registration_is_unique_across_connections(tmp_path):
    path = tmp_path / "ids.sqlite"
    registry = IdentityRegistry(path)
    registry.close()
    game_scope = {
        "league": "nba",
        "season": "2025-26",
        "phase": "Regular Season",
        "date": "2025-10-22",
        "home": "NYK",
        "away": "BOS",
    }

    def register(_):
        local = IdentityRegistry(path)
        try:
            return local.register_game("nba_api", "0022500001", game_scope, receipt())
        finally:
            local.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(register, range(2)))
    assert ids[0] == ids[1] and ids[0].startswith("g_") and len(ids[0]) == 34


def test_revision_and_alias_history_are_immutable():
    store = ObservationStore()
    result = ingest(store, payload())
    with pytest.raises(sqlite3.IntegrityError):
        store.connection.execute("UPDATE observations SET payload='{}'")
    with pytest.raises(sqlite3.IntegrityError):
        store.connection.execute("DELETE FROM game_aliases")
    assert store.read_observation(result["observation_id"])


def test_corrupt_current_pointer_does_not_silently_drop_a_game():
    store = ObservationStore()
    ingest(store, payload())
    store.connection.execute("UPDATE current_games SET revision='missing'")
    with pytest.raises(ContractError, match="missing facts"):
        store.current_semantics()


@pytest.mark.parametrize("day", ["2025-01-22", "2026-12-01"])
def test_date_outside_source_season_window_is_rejected(day):
    store = ObservationStore()
    value = payload(day=day)
    with pytest.raises(ContractError, match="Invalid scoped date"):
        ingest(store, value)
    assert not store.current_semantics()


def test_receipt_hash_and_duplicate_json_keys_fail_before_registration():
    store = ObservationStore()
    with pytest.raises(ContractError, match="checksum|hash"):
        store.ingest_bytes(canonical(payload()).encode(), receipt())
    body = b'{"provider":"nba_api","provider":"espn"}'
    with pytest.raises(ContractError, match="Duplicate JSON"):
        store.ingest_bytes(body, receipt(body))
    assert not store.current_semantics()


def test_identity_evidence_cannot_authorize_statistical_observations():
    store = ObservationStore()
    body = canonical(payload()).encode()
    with pytest.raises(ContractError, match="Identity-only evidence"):
        store.ingest_bytes(body, receipt(body, kind="reviewed_identity"))
    assert not store.current_semantics()


def test_foreign_database_is_not_modified(tmp_path):
    path = tmp_path / "foreign.sqlite"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE unrelated(value TEXT)")
    connection.commit()
    connection.close()
    with pytest.raises(ContractError, match="another application"):
        ObservationStore(path)
    connection = sqlite3.connect(path)
    assert [
        r[0]
        for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    ] == ["unrelated"]


def test_schemas_validate_generated_observations_and_inputs():
    root = Path(__file__).resolve().parents[1] / "contracts/unified_data"
    store = ObservationStore()
    value = complete(payload())
    result = ingest(store, value)
    for filename, item in [
        ("nba-archive-game.schema.json", value),
        ("observation.schema.json", store.read_observation(result["observation_id"])),
    ]:
        schema = json.loads((root / filename).read_text())
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(item)
