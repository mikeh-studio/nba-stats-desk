import copy
import hashlib
import json
import socket

import pytest
from app.data.unified.identity import ContractError, canonical
from app.data.unified.store import ObservationStore
from scripts import normalize_historical_observations as cli
from tests.support.historical import payload


def bundle(tmp_path, values):
    root = tmp_path / "retained"
    root.mkdir()
    captures = []
    for n, value in enumerate(values):
        body = canonical(value).encode()
        name = f"game-{n}.json"
        (root / name).write_bytes(body)
        captures.append(
            {
                "path": name,
                "sha256": hashlib.sha256(body).hexdigest(),
                "captured_at": None,
                "kind": "synthetic_fixture",
            }
        )
    (root / "manifest.json").write_text(
        json.dumps({"schema_version": "nba_archive_bundle/1", "captures": captures})
    )
    return root


def test_offline_cli_replay_has_stable_ids_without_network(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "ROOT", tmp_path)

    def deny(*args, **kwargs):
        raise AssertionError("Network access in offline adapter")

    monkeypatch.setattr(socket.socket, "connect", deny)
    source = bundle(tmp_path, [payload()])
    store = tmp_path / "reports/state.sqlite"
    first = cli.run(source, store, tmp_path / "reports/first")
    second = cli.run(source, store, tmp_path / "reports/second")
    assert first["recorded"] == 1 and second["noops"] == 1
    assert first["results"][0]["game_uid"] == second["results"][0]["game_uid"]
    assert first["appearances"] == 3 and first["observed_sample_games"] == 1
    assert not first["runtime_eligible"]


def test_batch_failure_rolls_back_every_game_and_preserves_sources(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    bad = payload("0022500002", "2025-10-23")
    bad["appearances"].append(copy.deepcopy(bad["appearances"][0]))
    source = bundle(tmp_path, [payload(), bad])
    before = {p.name: p.read_bytes() for p in source.iterdir()}
    path = tmp_path / "reports/state.sqlite"
    with pytest.raises(ContractError):
        cli.run(source, path, tmp_path / "reports/failed")
    store = ObservationStore(path)
    assert store.connection.execute("SELECT COUNT(*) FROM games").fetchone()[0] == 0
    assert store.current_semantics() == []
    assert before == {p.name: p.read_bytes() for p in source.iterdir()}
    assert (
        json.loads((tmp_path / "reports/failed/result.json").read_text())["status"]
        == "rejected"
    )


@pytest.mark.parametrize("mode", ["checksum", "traversal", "duplicate"])
def test_bad_manifest_cannot_write_a_store(tmp_path, monkeypatch, mode):
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    source = bundle(tmp_path, [payload()])
    path = source / "manifest.json"
    manifest = json.loads(path.read_text())
    if mode == "checksum":
        manifest["captures"][0]["sha256"] = "0" * 64
    elif mode == "traversal":
        manifest["captures"][0]["path"] = "../outside.json"
    else:
        manifest["captures"].append(manifest["captures"][0])
    path.write_text(json.dumps(manifest))
    store = tmp_path / "reports/state.sqlite"
    with pytest.raises(ContractError):
        cli.run(source, store, tmp_path / "reports/rejected")
    assert not store.exists()


def test_private_output_boundary_is_required(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    source = bundle(tmp_path, [payload()])
    with pytest.raises(ContractError):
        cli.run(source, tmp_path / "public.sqlite", tmp_path / "reports/output")
