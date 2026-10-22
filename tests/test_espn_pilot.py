from __future__ import annotations

import copy
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import espn_sources as src
from scripts.espn_store import validate

SEASON = "2025-26"
DAY = date(2025, 10, 22)
REF = [
    {"id": 10, "full_name": "Avery Able"},
    {"id": 11, "full_name": "Blake Baker"},
    {"id": 20, "full_name": "Casey Cole"},
]


def fixtures():
    status = {"completed": True, "state": "post", "name": "STATUS_FINAL"}
    teams = [
        {"homeAway": "home", "team": {"abbreviation": "NY", "id": "18"}, "score": "3"},
        {"homeAway": "away", "team": {"abbreviation": "BOS", "id": "2"}, "score": "0"},
    ]
    event = {
        "id": "1234567",
        "season": {"year": 2026, "type": 2},
        "competitions": [
            {
                "id": "1234567",
                "date": "2025-10-22T23:00Z",
                "status": {"type": status},
                "competitors": teams,
            }
        ],
    }
    labels = [
        "MIN",
        "PTS",
        "FG",
        "3PT",
        "FT",
        "REB",
        "AST",
        "TO",
        "STL",
        "BLK",
        "OREB",
        "DREB",
        "PF",
        "+/-",
    ]

    def athlete(pid, name, points, assist):
        return {
            "athlete": {"id": pid, "displayName": name},
            "didNotPlay": False,
            "stats": [
                "24",
                str(points),
                "1-1" if points else "0-0",
                "1-1" if points else "0-0",
                "0-0",
                "0",
                str(assist),
                "0",
                "0",
                "0",
                "0",
                "0",
                "0",
                "0",
            ],
        }

    players = [
        {"team": t["team"], "statistics": [{"labels": labels, "athletes": athletes}]}
        for t, athletes in zip(
            teams,
            [
                [
                    athlete("100", "Avery Able", 0, 1),
                    athlete("101", "Blake Baker", 3, 0),
                ],
                [athlete("200", "Casey Cole", 0, 0)],
            ],
        )
    ]
    play = {
        "id": "90001",
        "text": "Blake Baker makes 25-foot three point jumper (Avery Able assists)",
        "period": {"number": 1},
        "clock": {"displayValue": "11:00"},
        "scoringPlay": True,
        "scoreValue": 3,
        "team": {"id": "18"},
        "participants": [{"athlete": {"id": "100"}}, {"athlete": {"id": "101"}}],
        "homeScore": 3,
        "awayScore": 0,
    }
    plays = [play] + [
        {
            **play,
            "id": str(90000 + i),
            "period": {"number": i},
            "clock": {"displayValue": "0.0"},
            "text": "End of Game" if i == 4 else "End of Period",
            "scoringPlay": False,
            "scoreValue": 0,
        }
        for i in (2, 3, 4)
    ]
    schedule = {"leagues": [{"season": {"displayName": SEASON}}], "events": [event]}
    summary = {"header": event, "boxscore": {"players": players}, "plays": plays}
    return schedule, summary


def candidate():
    schedule, summary = fixtures()
    game = src.scoreboard(schedule, SEASON, DAY)[0]
    result = src.normalize_summary(summary, game, REF)
    result.update(
        nba_game_id="0022500001",
        pdf_validation={
            "status": "matched",
            "document_sha256": "synthetic",
            "matches": [
                {
                    "source_event_id": "90001",
                    "pdf_event_id": "synthetic-event",
                    "page": 2,
                    "url": "https://example.test/book.pdf#page=2",
                }
            ],
        },
    )
    return result


def test_normalizes_stats_namespaces_and_relationship_roles():
    result = candidate()
    assert len(result["players"]) == 3 and len(result["assists"]) == 1
    edge = result["assists"][0]
    assert edge["scorer_source_id"] == "101" and edge["scorer_nba_id"] == "11"
    assert edge["assister_source_id"] == "100" and edge["assister_nba_id"] == "10"
    assert validate(result) == validate(copy.deepcopy(result))


@pytest.mark.parametrize(
    "defect",
    [
        "wrong_season",
        "wrong_date",
        "duplicate_event",
        "status_conflict",
        "same_team",
        "invalid_id",
    ],
)
def test_discovery_rejects_unsafe_scope(defect):
    p, _ = fixtures()
    e = p["events"][0]
    if defect == "wrong_season":
        e["season"]["year"] = 2027
    elif defect == "wrong_date":
        e["competitions"][0]["date"] = "2025-10-23T23:00Z"
    elif defect == "duplicate_event":
        p["events"].append(copy.deepcopy(e))
    elif defect == "status_conflict":
        e["competitions"][0]["status"]["type"]["state"] = "in"
    elif defect == "same_team":
        e["competitions"][0]["competitors"][0]["team"]["abbreviation"] = "BOS"
    else:
        e["id"] = "abc"
    with pytest.raises(ValueError):
        src.scoreboard(p, SEASON, DAY)


def test_in_progress_and_empty_are_explicit_not_finals():
    p, _ = fixtures()
    p["events"][0]["competitions"][0]["status"]["type"] = {
        "completed": False,
        "state": "in",
        "name": "STATUS_IN_PROGRESS",
    }
    assert not src.scoreboard(p, SEASON, DAY)[0]["final"]
    p["events"] = []
    assert src.scoreboard(p, SEASON, DAY) == []


@pytest.mark.parametrize(
    "defect",
    [
        "duplicate_player",
        "stat_cells",
        "missing_label",
        "points",
        "rebounds",
        "assists",
        "participants",
        "self_assist",
        "duplicate_play",
        "incomplete_periods",
        "end_score",
        "unknown_team",
        "bad_clock",
    ],
)
def test_summary_validation_rejects_corrupt_inputs(defect):
    schedule, p = fixtures()
    game = src.scoreboard(schedule, SEASON, DAY)[0]
    group = p["boxscore"]["players"][0]["statistics"][0]
    if defect == "duplicate_player":
        group["athletes"].append(copy.deepcopy(group["athletes"][0]))
    elif defect == "stat_cells":
        group["athletes"][0]["stats"].pop()
    elif defect == "missing_label":
        group["labels"][1] = "UNKNOWN"
    elif defect == "points":
        group["athletes"][0]["stats"][1] = "9"
    elif defect == "rebounds":
        group["athletes"][0]["stats"][5] = "3"
    elif defect == "assists":
        group["athletes"][0]["stats"][6] = "2"
    elif defect == "participants":
        p["plays"][0]["participants"] = []
    elif defect == "self_assist":
        p["plays"][0]["text"] = "Blake Baker makes jumper (Blake Baker assists)"
    elif defect == "duplicate_play":
        p["plays"].append(p["plays"][0])
    elif defect == "incomplete_periods":
        p["plays"].pop(1)
    elif defect == "end_score":
        p["plays"][-1]["homeScore"] = 4
    elif defect == "unknown_team":
        p["plays"][0]["team"]["id"] = "999"
    else:
        p["plays"][0]["clock"]["displayValue"] = "12:99"
    with pytest.raises((ValueError, KeyError)):
        src.normalize_summary(p, game, REF)


def test_player_mapping_never_uses_espn_id_as_nba_id():
    schedule, p = fixtures()
    game = src.scoreboard(schedule, SEASON, DAY)[0]
    result = src.normalize_summary(p, game, [])
    assert all(row["nba_player_id"] is None for row in result["players"])
    assert result["assists"][0]["scorer_nba_id"] is None
    result = src.normalize_summary(
        p, game, REF + [{"id": 999, "full_name": "Avery Able"}]
    )
    assert (
        next(p for p in result["players"] if p["name"] == "Avery Able")["nba_player_id"]
        is None
    )


@pytest.mark.parametrize("defect", ["duplicate", "wrong_year", "score_conflict"])
def test_game_mapping_rejects_conflicts(defect):
    g = candidate()["game"]
    row = {
        "GameId": "0022500001",
        "Date": g["date"],
        "HomeTeamAbbreviation": "NYK",
        "AwayTeamAbbreviation": "BOS",
        "HomePoints": 3,
        "AwayPoints": 0,
    }
    assert src.game_mapping(g, []) is None
    assert src.game_mapping(g, [row]) == "0022500001"
    rows = [row]
    if defect == "duplicate":
        rows.append(copy.deepcopy(row))
    elif defect == "wrong_year":
        row["GameId"] = "0022600001"
    else:
        row["HomePoints"] = 4
    with pytest.raises(ValueError):
        src.game_mapping(g, rows)


def parsed(candidate):
    return {
        "status": "reconciled",
        "errors": [],
        "game": src.pdf_game(candidate),
        "player_validation": [
            {
                "player_id": p["nba_player_id"],
                "team": p["team"],
                "stats": {
                    "PTS": p["stats"]["PTS"],
                    "FG": p["stats"]["FGM"],
                    "3P": p["stats"]["FG3M"],
                    "FT": p["stats"]["FTM"],
                    "A": p["stats"]["AST"],
                },
            }
            for p in candidate["players"]
            if p["played"]
        ],
        "assists": [
            {
                "period": 1,
                "clock": "11:00",
                "team": "NYK",
                "points": 3,
                "scorer_id": "11",
                "assister_id": "10",
                "event_id": "pdf-event",
                "page": 2,
            }
        ],
        "source_url": "https://example.test/book.pdf",
    }


def test_pdf_comparison_matches_roles_clock_points_and_page_links():
    c = candidate()
    p = parsed(c)
    assert src.compare_pdf(c, p)[0]["url"].endswith("#page=2")
    p["assists"][0]["assister_id"] = "20"
    with pytest.raises(ValueError, match="event mismatch"):
        src.compare_pdf(c, p)


def test_same_assist_total_does_not_hide_changed_clock_or_player():
    c = candidate()
    p = parsed(c)
    p["assists"][0]["clock"] = "10:59"
    with pytest.raises(ValueError, match="event mismatch"):
        src.compare_pdf(c, p)
    p = parsed(c)
    p["player_validation"][0]["stats"]["A"] = 0
    with pytest.raises(ValueError, match="player A"):
        src.compare_pdf(c, p)


def test_publisher_recounts_candidate_before_replacement():
    c = candidate()
    revision = validate(c)
    corrected = copy.deepcopy(c)
    corrected["players"][0]["stats"]["STL"] += 1
    assert validate(corrected) != revision
    corrected["players"][0]["stats"]["AST"] += 1
    with pytest.raises(ValueError, match="Assist recount"):
        validate(corrected)
    c["pdf_validation"]["matches"] = []
    with pytest.raises(ValueError, match="incomplete"):
        validate(c)


def test_pdf_and_espn_subminute_clocks_compare_without_rounding_to_minutes():
    assert src.clock(":39.8", 4) == src.clock("39.8", 4) == 39.8
    assert src.clock("0.0", 4) == 0


@pytest.mark.parametrize(
    "status,health,pdf,code",
    [
        ("published", "healthy", "matched", 0),
        ("noop", "healthy", "matched", 0),
        ("published", "pending_mapping", "pending_mapping", 2),
        ("noop", "failed", "matched", 2),
        ("rejected", "failed", None, 2),
    ],
)
def test_dag_exit_does_not_hide_pending_mapping_or_prior_failure(
    status, health, pdf, code
):
    from scripts.espn_ingestion import outcome_exit_code

    assert (
        outcome_exit_code({"status": status, "health": health, "pdf_validation": pdf})
        == code
    )


def test_pdf_identity_is_explicitly_name_mapped_and_requires_all_active_ids():
    c = candidate()
    identity = src.pdf_identity(c)
    assert identity["reference_kind"].startswith("identity_only")
    assert identity["stats"]["Home"]["FullGame"][0]["EntityId"] == "10"
    c["players"][0]["nba_player_id"] = None
    with pytest.raises(ValueError, match="Unmapped"):
        src.pdf_identity(c)


def test_frozen_pdf_checks_scope_and_hash_and_retains_original_timestamp(tmp_path):
    import hashlib
    import json

    from scripts.espn_ingestion import captured_pdf

    game = src.pdf_game(candidate())
    body = b"synthetic-pdf-bytes"
    digest = hashlib.sha256(body).hexdigest()
    capture = tmp_path / "source"
    capture.mkdir()
    meta = {
        "status": "available",
        "http_status": 200,
        "game": game,
        "sha256": digest,
        "artifact": digest + ".pdf",
        "url": "https://statsdmz.nba.com/pdfs/20251022/20251022_BOSNYK_book.pdf",
        "finished_at": "2025-10-23T04:00:00Z",
    }
    (capture / "attempt.json").write_text(json.dumps(meta))
    (capture / meta["artifact"]).write_bytes(body)
    result, path = captured_pdf(capture, tmp_path / "copy", game)
    assert result == meta and path.read_bytes() == body
    with pytest.raises(ValueError, match="scope"):
        captured_pdf(capture, tmp_path / "wrong", {**game, "date": "2025-10-23"})
    (capture / meta["artifact"]).write_bytes(b"changed")
    with pytest.raises(ValueError, match="hash"):
        captured_pdf(capture, tmp_path / "corrupt", game)


@pytest.mark.parametrize("missing_final", [False, True])
def test_discovery_caches_verified_mapping_but_rejects_lost_final(
    tmp_path, monkeypatch, missing_final
):
    import json
    from types import SimpleNamespace

    from scripts import espn_ingestion as worker
    from scripts.gamebook_sources import SourceUnavailable

    schedule, _ = fixtures()
    game = src.scoreboard(schedule, SEASON, DAY)[0]
    record = {
        "GameId": "0022500001",
        "Date": str(DAY),
        "HomeTeamAbbreviation": "NYK",
        "AwayTeamAbbreviation": "BOS",
        "HomePoints": 3,
        "AwayPoints": 0,
    }
    registered = []
    cloud = SimpleNamespace(
        parameter=lambda *a: a,
        query=lambda *a: [
            {
                "source_game_id": game["source_game_id"],
                "game_date": DAY,
                "finished_at": None,
            }
        ],
        upload=lambda *a: "gs://synthetic/cache",
    )
    store = SimpleNamespace(
        cloud=cloud,
        prefix="synthetic.dev",
        register_games=lambda games: registered.extend(games),
        mappings=lambda season: [
            {"source_game_id": game["source_game_id"], "record": json.dumps(record)}
        ],
    )

    def fetch(url, *args):
        if "pbpstats" in url:
            raise SourceUnavailable("synthetic lookup outage")
        return {**schedule, "events": []} if missing_final else schedule

    monkeypatch.setattr(worker, "capture_json", fetch)
    monkeypatch.setattr(worker, "archive_sources", lambda *a: "gs://synthetic/evidence")
    if missing_final:
        with pytest.raises(ValueError, match="lost previously"):
            worker.discover(tmp_path, SEASON, DAY, DAY, store, None)
        assert not registered
    else:
        entries = worker.discover(tmp_path, SEASON, DAY, DAY, store, None)
        assert entries[0]["nba_game_id"] == record["GameId"]
        assert entries[0]["mapping_source_status"] == "synthetic lookup outage"
        assert registered == [game]
