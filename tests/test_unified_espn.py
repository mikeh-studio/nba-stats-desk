"""Provider observations remain offline, revision-bound and identity-safe."""

import copy
import json
import sqlite3
from pathlib import Path

import pytest
from app.data.unified.identity import ContractError, canonical
from app.data.unified.store import ObservationStore
from jsonschema import Draft202012Validator, FormatChecker
from scripts import espn_corrections as layers
from scripts import espn_sources as source
from scripts import normalize_historical_observations as cli
from tests.support.espn import fixtures
from tests.support.historical import ingest, payload, receipt

from test_unified_historical_cli import bundle


def retained():
    _, raw = fixtures()
    raw["header"]["season"]["year"] = 2027
    raw["header"]["competitions"][0]["date"] = "2026-10-22T23:00Z"
    game = source.event_game(
        raw["header"], "2026-27", __import__("datetime").date(2026, 10, 22)
    )
    raw["boxscore"]["players"][0]["statistics"][0]["athletes"].append(
        {
            "athlete": {"id": "102", "displayName": "Shared Name"},
            "didNotPlay": True,
            "stats": [],
        }
    )
    return wrap(raw, game)


def wrap(raw, game):
    body = canonical(raw).encode()
    return {
        "schema_version": "espn_retained_game/1",
        "provider": "espn",
        "season": "2026-27",
        "phase": "Regular Season",
        "game": game,
        "source_body": body.decode(),
        "source": receipt(body, "2026-10-23T12:00:00+00:00"),
    }


def seed(store):
    for sid, pid in (("100", 10), ("101", 11), ("102", 12), ("200", 20)):
        store.attach_person_alias("espn", sid, pid, receipt())


def test_raw_membership_dnp_zero_null_and_legacy_chain():
    store = ObservationStore()
    seed(store)
    value = retained()
    raw = json.loads(value["source_body"])
    athletes = raw["boxscore"]["players"][0]["statistics"][0]["athletes"]
    athletes[0]["stats"][0] = "0"
    athletes[1]["stats"][0] = "--"
    value = wrap(raw, value["game"])
    first = ingest(store, value)
    obs = store.read_observation(first["observation_id"])
    players = {p["player_id"]: p for p in obs["semantic"]["appearances"]}
    assert set(players) == {10, 11, 20} and 12 not in players
    assert players[10]["minutes"]["zero_origin"] == "reported_zero"
    assert players[11]["minutes"]["value"] is None
    assert store.player_summary(10)["games"] == 1
    assert obs["source_chain"]["box_revision"] == layers.validate(
        layers.normalize(raw, value["game"], "box")
    )
    assert len(obs["membership"]["participant_manifest"]["participants"]) == 4
    assert ingest(store, value)["status"] == "noop"
    assert not any(obs["semantic"]["capabilities"].values())


def test_cross_season_person_identity_and_different_provider_games():
    store = ObservationStore()
    seed(store)
    nba = ingest(store, payload())
    espn = ingest(store, retained())
    assert nba["game_uid"] != espn["game_uid"]
    assert store.player_summary(10)["games"] == 2
    assert len(store.current_semantics("espn")) == 1
    assert len(store.current_semantics("nba_api")) == 1
    assert store.player_summary(10, season="2026-27")["games"] == 1
    assert store.player_summary(10, season="2025-26")["providers"] == ["nba_api"]


def test_unresolved_duplicate_name_cannot_change_identity_or_leave_partial_facts():
    store = ObservationStore()
    value = retained()
    with pytest.raises(ContractError, match="Unresolved ESPN"):
        ingest(store, value)
    assert store.connection.execute("SELECT COUNT(*) FROM games").fetchone()[0] == 0
    seed(store)
    raw = json.loads(value["source_body"])
    for team in raw["boxscore"]["players"]:
        for a in team["statistics"][0]["athletes"]:
            a["athlete"]["displayName"] = "Same Name"
    result = ingest(store, wrap(raw, value["game"]))
    assert {
        p["player_id"]
        for p in store.read_observation(result["observation_id"])["semantic"][
            "appearances"
        ]
    } == {10, 11, 20}


def test_reviewed_alias_correction_holds_old_current_facts_until_complete_replay():
    store = ObservationStore()
    seed(store)
    value = retained()
    first = ingest(store, value)
    assert store.correct_person_alias("espn", "101", 99, 1, receipt()) == 2
    with pytest.raises(ContractError, match="held after identity correction"):
        store.current_semantics()
    assert (
        store.read_observation(first["observation_id"])["semantic"]["appearances"][1][
            "player_id"
        ]
        == 11
    )
    with pytest.raises(ContractError, match="Stale"):
        store.correct_person_alias("espn", "101", 98, 1, receipt())
    corrected = ingest(store, value, expected=first["revision"])
    assert corrected["status"] == "replaced"
    assert (
        store.player_summary(11)["games"] == 0
        and store.player_summary(99)["games"] == 1
    )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        store.connection.execute("DELETE FROM player_alias_corrections")


def test_corrected_espn_source_replaces_whole_game_and_old_capture_is_not_rollback():
    store = ObservationStore()
    seed(store)
    original = retained()
    first = ingest(store, original)
    raw = json.loads(original["source_body"])
    raw["header"]["competitions"][0]["competitors"][0]["score"] = "6"
    cells = raw["boxscore"]["players"][0]["statistics"][0]["athletes"][1]["stats"]
    cells[1], cells[2], cells[3] = "6", "2-2", "2-2"
    game = source.event_game(
        raw["header"], "2026-27", __import__("datetime").date(2026, 10, 22)
    )
    changed = wrap(raw, game)
    with pytest.raises(ContractError, match="expected prior"):
        ingest(store, changed)
    second = ingest(store, changed, expected=first["revision"])
    assert second["game_uid"] == first["game_uid"] and second["status"] == "replaced"
    assert store.player_summary(11)["totals"]["pts"] == 6
    assert (
        store.read_observation(first["observation_id"])["semantic"]["result"][
            "home_score"
        ]
        == 3
    )
    with pytest.raises(ContractError, match="Historical revision replay"):
        ingest(store, original, expected=second["revision"])


def test_recapture_keeps_fact_revision_and_retains_original_times():
    store = ObservationStore()
    seed(store)
    value = retained()
    first = ingest(store, value)
    value["source"]["captured_at"] = "2026-10-24T12:00:00+00:00"
    second = ingest(store, value)
    assert second["revision"] == first["revision"] and second["status"] == "noop"
    assert second["observation_id"] != first["observation_id"]
    assert (
        store.read_observation(first["observation_id"])["source_chain"]["source"][
            "captured_at"
        ]
        == "2026-10-23T12:00:00+00:00"
    )


def test_overlapping_provider_pointers_require_selection_and_check_provider_binding():
    store = ObservationStore()
    first = ingest(store, payload())
    store.connection.execute(
        "INSERT INTO current_espn_games VALUES(?,?,?)",
        (first["game_uid"], first["revision"], first["observation_id"]),
    )
    with pytest.raises(ContractError, match="Provider selection"):
        store.current_semantics()
    assert len(store.current_semantics("nba_api")) == 1
    with pytest.raises(ContractError, match="identity binding"):
        store.current_semantics("espn")


def test_scope_correction_holds_stale_current_without_rewriting_history():
    store = ObservationStore()
    seed(store)
    first = ingest(store, retained())
    scope, version = store.game_scope(first["game_uid"])
    scope["date"] = "2026-10-23"
    store.revise_scope(first["game_uid"], scope, version, receipt())
    with pytest.raises(ContractError, match="held after scope correction"):
        store.current_semantics()
    assert (
        store.read_observation(first["observation_id"])["semantic"]["scope"]["date"]
        == "2026-10-22"
    )


@pytest.mark.parametrize(
    "mode",
    ["source_hash", "phase", "didNotPlay", "minutes", "scores", "partial", "duplicate"],
)
def test_bad_espn_capture_does_not_replace_current(mode):
    store = ObservationStore()
    seed(store)
    value = retained()
    first = ingest(store, value)
    raw = json.loads(value["source_body"])
    group = raw["boxscore"]["players"][0]["statistics"][0]
    if mode == "phase":
        raw["header"]["season"]["type"] = 3
    elif mode == "didNotPlay":
        group["athletes"][0]["didNotPlay"] = "false"
    elif mode == "minutes":
        group["athletes"][0]["stats"][0] = "24:01"
    elif mode == "scores":
        group["athletes"][0]["stats"][1] = "10"
    elif mode == "partial":
        raw["boxscore"]["players"].pop()
    elif mode == "duplicate":
        group["athletes"].append(copy.deepcopy(group["athletes"][0]))
    value = wrap(raw, value["game"])
    if mode == "source_hash":
        value["source"]["sha256"] = "0" * 64
    with pytest.raises(ValueError):
        ingest(store, value, expected=first["revision"])
    assert store.current_revision(first["game_uid"]) == first["revision"]


def test_held_batch_retains_input_and_reports_without_partial_promotions(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    source_path = bundle(tmp_path, [payload(), retained()])
    path = tmp_path / "reports/state.sqlite"
    report = cli.run(source_path, path, tmp_path / "reports/result", allow_held=True)
    assert report["games"] == 1 and report["held_games"] == 1
    assert report["held"][0]["provider"] == "espn"
    store = ObservationStore(path)
    assert len(store.current_semantics()) == 1
    row = store.connection.execute(
        "SELECT payload,reason FROM quarantined_inputs"
    ).fetchone()
    assert json.loads(row[0])["schema_version"] == "espn_retained_game/1"
    assert "Unresolved" in row[1]


def test_espn_contract_schema_and_observation():
    store = ObservationStore()
    seed(store)
    value = retained()
    result = ingest(store, value)
    root = Path(__file__).resolve().parents[1] / "contracts/unified_data"
    for file, document in [
        ("espn-retained-game.schema.json", value),
        ("observation.schema.json", store.read_observation(result["observation_id"])),
    ]:
        schema = json.loads((root / file).read_text())
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(document)
