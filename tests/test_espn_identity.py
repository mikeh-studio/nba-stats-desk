import json

import pytest
from scripts import espn_identity as identity
from scripts.parse_gamebooks import sha


def literal(rows, suffix=""):
    body = ("players = " + repr(rows) + "\n" + suffix).encode()
    return body, {"sha256": sha(body), "method": "synthetic"}


def test_catalog_is_hash_pinned_and_never_executes_downloaded_python(tmp_path):
    marker = tmp_path / "must-not-exist"
    body, pin = literal(
        [[10, "Able", "Avery", "Avery Able", True]],
        f"open({str(marker)!r}, 'w').write('executed')",
    )
    assert identity.parse_catalog(body, pin) == [{"id": 10, "full_name": "Avery Able"}]
    assert not marker.exists()
    with pytest.raises(ValueError, match="hash"):
        identity.parse_catalog(body + b"changed", pin)


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [[True, "A", "A", "A A", True]],
        [[10, "A", "A", "A A", True], [10, "B", "B", "B B", True]],
        [[10, "A", "A", "", True]],
        [[10, "A", "A", "A A", "true"]],
    ],
)
def test_catalog_rejects_bad_or_duplicate_ids(rows):
    body, pin = literal(rows)
    with pytest.raises(ValueError):
        identity.parse_catalog(body, pin)


@pytest.mark.parametrize("body", [b"players = list(range(10))", b"players = ["])
def test_catalog_requires_literal_players_assignment(body):
    with pytest.raises(ValueError):
        identity.parse_catalog(body, {"sha256": sha(body)})


def test_crosswalk_fills_nulls_keeps_namespaces_and_preserves_existing_ids():
    players = [{"source_player_id": "500", "name": "Avery Able", "nba_player_id": None}]
    reference = [{"id": 10, "full_name": "Avery Able"}]
    pin = {"method": "synthetic", "sha256": "synthetic"}
    rows, missing = identity.crosswalk(players * 2, reference, pin)
    assert len(rows) == 1 and not missing
    assert rows[0]["source_player_id"] == "500" and rows[0]["nba_player_id"] == "10"
    assert players[0]["nba_player_id"] is None
    players[0]["nba_player_id"] = "11"
    with pytest.raises(ValueError, match="Existing"):
        identity.crosswalk(players, reference, pin)


def test_crosswalk_never_guesses_ambiguous_or_unknown_names():
    players = [
        {"source_player_id": "500", "name": "Avery Able", "nba_player_id": None},
        {"source_player_id": "600", "name": "New Player", "nba_player_id": None},
    ]
    ref = [{"id": i, "full_name": "Avery Able"} for i in (10, 11)]
    rows, missing = identity.crosswalk(
        players, ref, {"method": "synthetic", "sha256": "x"}
    )
    assert not rows and {r["reason"] for r in missing} == {"missing", "ambiguous"}


def test_crosswalk_rejects_two_provider_ids_for_one_nba_player():
    players = [
        {"source_player_id": str(i), "name": "Avery Able", "nba_player_id": None}
        for i in (500, 600)
    ]
    with pytest.raises(ValueError, match="Multiple ESPN"):
        identity.crosswalk(
            players,
            [{"id": 10, "full_name": "Avery Able"}],
            {"method": "synthetic", "sha256": "x"},
        )


def test_catalog_capture_uses_pinned_allowlisted_url_and_saves_bytes(
    tmp_path, monkeypatch
):
    from test_gamebook_ingestion import Response, Session

    body, pin = literal([[10, "Able", "Avery", "Avery Able", True]])
    pin.update(
        repository="swar/nba_api",
        path="src/nba_api/stats/library/data.py",
        commit="a" * 40,
    )
    file = tmp_path / "pin.json"
    file.write_text(json.dumps(pin))
    monkeypatch.setattr(identity, "PIN_PATH", file)
    result, actual_pin = identity.fetch_catalog(
        tmp_path / "capture", Session(Response(body))
    )
    assert result[0]["id"] == 10 and actual_pin == pin
    meta = json.loads((tmp_path / "capture/attempt.json").read_text())
    assert meta["url"].endswith("a" * 40 + "/" + pin["path"])
    assert meta["status"] == "captured" and meta["sha256"] == pin["sha256"]


def test_aliases_require_same_catalog_id_and_do_not_hide_name_ambiguity():
    current = [
        {"id": 10, "full_name": "Avery Able Jr."},
        {"id": 20, "full_name": "Avery Able"},
    ]
    old = [
        {"id": 10, "full_name": "Avery Able"},
        {"id": 99, "full_name": "Unverified Player"},
    ]
    combined, aliases = identity.with_installed_aliases(current, old)
    assert aliases == [old[0]]
    rows, missing = identity.crosswalk(
        [{"source_player_id": "500", "name": "Avery Able", "nba_player_id": None}],
        combined,
        {"method": "synthetic", "sha256": "x"},
    )
    assert not rows and missing[0]["reason"] == "ambiguous"


@pytest.mark.parametrize("bad_summary", [False, True])
def test_worker_retains_resolved_identity_on_pdf_failure_but_not_bad_summary(
    tmp_path, monkeypatch, bad_summary
):
    from types import SimpleNamespace

    from scripts import espn_ingestion as worker
    from scripts import espn_sources as source

    from test_espn_pilot import DAY, REF, SEASON, fixtures
    from test_gamebook_ingestion import Response, Session

    schedule, summary = fixtures()
    game = source.scoreboard(schedule, SEASON, DAY)[0]
    if bad_summary:
        summary["plays"][-1]["homeScore"] = 99
    calls = []
    store = SimpleNamespace(
        cloud=object(),
        register=lambda g: None,
        remember_players=lambda rows, evidence: (
            calls.append(rows) or {"status": "published"}
        ),
        failure=lambda g, error, evidence: {"status": "rejected", "error": error},
    )
    monkeypatch.setattr(
        worker.identity,
        "fetch_catalog",
        lambda *a: (REF, {"method": "synthetic", "sha256": "synthetic"}),
    )
    monkeypatch.setattr(
        worker, "fetch_pdf", lambda *a: {"attempts": [{"status": "network_error"}]}
    )
    monkeypatch.setattr(worker, "archive_sources", lambda *a: "gs://synthetic/evidence")
    result = worker.process(
        {"game": game, "nba_game_id": "0022500001"},
        tmp_path,
        store,
        Session(Response(json.dumps(summary).encode())),
    )
    assert result["status"] == "rejected"
    assert len(calls) == (0 if bad_summary else 1)
    assert result["retryable"] is (not bad_summary)


def test_refresh_cli_writes_command_report_and_result_once(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from scripts import espn_ingestion as worker

    from test_espn_pilot import REF, candidate

    (tmp_path / "reports").mkdir()
    output = tmp_path / "reports/refresh"
    cloud = SimpleNamespace(
        parameter=lambda *a: a,
        query=lambda *a: [
            {
                "source_game_id": "1234567",
                "revision": "synthetic",
                "payload": json.dumps(candidate()),
            }
        ],
    )
    store = SimpleNamespace(
        cloud=cloud,
        prefix="synthetic.dev",
        remember_players=lambda *a: {"status": "noop", "inserted": 0},
    )
    monkeypatch.setattr(worker, "ROOT", tmp_path)
    monkeypatch.setattr(worker, "make_store", lambda *a: store)
    monkeypatch.setattr(
        identity,
        "fetch_catalog",
        lambda *a: (REF, {"method": "synthetic", "sha256": "synthetic"}),
    )
    monkeypatch.setattr(
        "scripts.gamebook_ingestion.archive_sources",
        lambda *a: "gs://synthetic/evidence",
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "espn_ingestion.py",
            "refresh-player-mappings",
            "--season",
            "2025-26",
            "--output",
            str(output),
        ],
    )
    assert worker.main() == 0
    assert json.loads((output / "result.json").read_text()) == json.loads(
        (output / "identity_result.json").read_text()
    )
    assert not (output / "failure.json").exists()
