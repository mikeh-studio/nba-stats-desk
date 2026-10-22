from __future__ import annotations

import copy
import io
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.api_core.exceptions import NotFound, PreconditionFailed
from pypdf import PdfReader

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import gamebook_ingestion as worker
from scripts import gamebook_sources as sources
from scripts.gamebook_store import GamebookStore

from gamebook_storage_scenarios import candidate
from test_gamebook_assists import GAME, fragment, pdf_from_pages

NOW = datetime(2025, 10, 23, 12, tzinfo=timezone.utc)
AUTO_GAME = {**GAME, "game_id": "0022500001"}


class Response:
    def __init__(self, body, status=200):
        self.body, self.status_code = body, status
        self.headers = {"Content-Type": "application/json"}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def iter_content(self, chunk_size):
        yield self.body


class Session:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = []

    def get(self, url, **kwargs):
        assert kwargs["allow_redirects"] is False
        self.calls.append(url)
        return next(self.responses)


def schedule(games=None):
    return {
        "leagueSchedule": {
            "seasonYear": "2025-26",
            "gameDates": [
                {
                    "gameDate": "10/22/2025 00:00:00",
                    "games": games
                    if games is not None
                    else [
                        {
                            "gameId": AUTO_GAME["game_id"],
                            "gameStatus": 3,
                            "homeTeam": {
                                "teamTricode": "BBB",
                                "teamCity": "Beta",
                                "teamName": "Hosts",
                            },
                            "awayTeam": {
                                "teamTricode": "AAA",
                                "teamCity": "Alpha",
                                "teamName": "Visitors",
                            },
                        }
                    ],
                }
            ],
        }
    }


def test_discovery_final_regular_season_local_date_and_duplicate_guard():
    payload = schedule()
    game = payload["leagueSchedule"]["gameDates"][0]["games"][0]
    game["gameDateTimeUTC"] = "2025-10-23T02:00:00Z"
    assert sources.schedule_games(payload, "2025-26", date(2025, 10, 22)) == [AUTO_GAME]
    assert sources.schedule_games(payload, "2025-26", date(2025, 10, 21)) == []
    for status in (1, 2):
        game["gameStatus"] = status
        assert sources.schedule_games(payload, "2025-26", NOW.date()) == []
    game["gameStatus"] = 3
    for prefix in ("001", "004", "005", "006"):
        game["gameId"] = prefix + "2500001"
        assert sources.schedule_games(payload, "2025-26", NOW.date()) == []
    game["gameId"] = AUTO_GAME["game_id"]
    payload["leagueSchedule"]["gameDates"][0]["games"].append(dict(game))
    with pytest.raises(ValueError, match="duplicate"):
        sources.schedule_games(payload, "2025-26", NOW.date())


@pytest.mark.parametrize("mutation", ["season", "empty", "wrong_id", "date"])
def test_invalid_or_stale_schedule_is_not_successful_empty(mutation):
    payload = schedule()
    league = payload["leagueSchedule"]
    if mutation == "season":
        league["seasonYear"] = "2024-25"
    elif mutation == "empty":
        league["gameDates"] = []
    elif mutation == "wrong_id":
        league["gameDates"][0]["games"][0]["gameId"] = "0022400001"
    else:
        league["gameDates"][0]["gameDate"] = "10/22/2024"
    with pytest.raises(ValueError):
        sources.schedule_games(payload, "2025-26", NOW.date())


def test_source_fallback_and_both_blocked_keep_evidence(tmp_path):
    session = Session(
        Response(b"blocked", 403), Response(json.dumps(schedule()).encode())
    )
    assert sources.discover("2025-26", NOW.date(), tmp_path / "ok", session) == [
        AUTO_GAME
    ]
    assert (tmp_path / "ok/0/response.body").read_bytes() == b"blocked"
    with pytest.raises(sources.SourceUnavailable, match="not an empty"):
        sources.discover(
            "2025-26",
            NOW.date(),
            tmp_path / "failed",
            Session(Response(b"no", 403), Response(b"rate", 429)),
        )
    assert len(json.loads((tmp_path / "failed/failure.json").read_text())) == 2


def test_request_caps_and_invalid_json_are_failures(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "MAX_JSON_BYTES", 2)
    with pytest.raises(sources.SourceUnavailable):
        sources.capture_json(
            "https://example.test", tmp_path / "cap", Session(Response(b"123"))
        )
    assert json.loads((tmp_path / "cap/attempt.json").read_text())["status"] == "failed"


def test_queue_recovers_old_failures_rotates_corrections_and_respects_budget():
    games = [
        {**AUTO_GAME, "game_id": f"002250000{i}", "date": "2025-10-01"}
        for i in range(1, 5)
    ]
    states = [
        {
            "game_id": g["game_id"],
            "current_revision": "rev",
            "last_status": "published",
            "last_checked": NOW - timedelta(days=8),
        }
        for g in games
    ]
    states[0].update(last_status="rejected", last_checked=NOW - timedelta(days=2))
    states[1]["last_checked"] = NOW - timedelta(hours=1)
    states[2]["last_checked"] = None
    selected, due = worker.choose_games(games, states, NOW, limit=2)
    assert due == 3
    assert [g["game_id"] for g in selected] == [
        games[2]["game_id"],
        games[3]["game_id"],
    ]
    selected, _ = worker.choose_games(games, states, NOW, limit=10)
    assert games[0] in selected  # failed game remains eligible beyond correction window
    assert games[1] not in selected
    with pytest.raises(ValueError):
        worker.choose_games(games, states, NOW, limit=51)


def synthetic_sources(tmp_path, **kwargs):
    _, body, identity = candidate(tmp_path, **kwargs)
    pages = [
        worker.parse_document.__globals__["page_fragments"](p)
        for p in PdfReader(io.BytesIO(body)).pages
    ]
    pages[0] += [fragment(10, 700, "October 22 2025 Alpha Visitors Beta Hosts")]
    body = pdf_from_pages(pages)
    reference = json.loads(identity)
    reference["season_type"] = "Regular Season"
    return body, reference


class LocalCloud:
    """Real local validation/transactions; explicitly NOT a live BigQuery test."""

    def __init__(self, root):
        self.local = GamebookStore(root)
        self.uploads = []

    def register(self, games):
        self.local.register_games(games)

    def upload(self, body, category, suffix):
        self.uploads.append((body, category))
        return "gs://synthetic/" + sources.digest(body)

    def ingest(self, *args, **kwargs):
        return self.local.ingest(*args, **kwargs)


def game_session(body, identity, *, pdf_status=200):
    return Session(
        Response(body, pdf_status),
        Response(b"CDN blocked", 403),
        Response(json.dumps(identity).encode()),
    )


def test_full_worker_replay_correction_failed_replacement_and_recovery(tmp_path):
    store = LocalCloud(tmp_path / "store")
    original = synthetic_sources(tmp_path / "original")
    corrected = synthetic_sources(tmp_path / "corrected", assisted=False)
    invalid = synthetic_sources(tmp_path / "invalid", box_assists=2)
    steps = [
        (original, "published", 1),
        (original, "noop", 1),
        (corrected, "published", 0),
        (invalid, "rejected", 0),
    ]
    for i, (data, status, assists) in enumerate(steps):
        output = tmp_path / str(i)
        output.mkdir()
        outcome = worker.process_game(AUTO_GAME, output, store, game_session(*data))
        assert outcome["status"] == status
        assert outcome["retryable"] is False
        assert (
            store.local.rows("SELECT COUNT(*) n FROM current_event_relationships")[0][
                "n"
            ]
            == assists
        )
    assert len(store.local.rows("SELECT * FROM gamebook_publication_history")) == 2
    assert store.local.coverage(AUTO_GAME["game_id"])["health"] == "failed"
    assert any(b"CDN blocked" == b for b, _ in store.uploads)
    store.local.close()


def test_delayed_pdf_and_identity_outage_are_audited_and_retryable(tmp_path):
    store = LocalCloud(tmp_path / "store")
    body, reference = synthetic_sources(tmp_path / "source")
    for i, session in enumerate(
        (
            Session(Response(b"not yet", 404)),
            Session(Response(body), Response(b"blocked", 403), Response(b"down", 500)),
            game_session(body, reference),
        )
    ):
        output = tmp_path / str(i)
        output.mkdir()
        outcome = worker.process_game(AUTO_GAME, output, store, session)
        assert outcome["status"] == ("published" if i == 2 else "rejected")
        assert outcome["retryable"] == (i != 2)
    assert len(store.local.rows("SELECT * FROM gamebook_extraction_runs")) == 3
    assert store.local.coverage(AUTO_GAME["game_id"])["health"] == "healthy"
    store.local.close()


def test_identity_normalization_stable_across_provider_stats_and_order(tmp_path):
    _, reference = synthetic_sources(tmp_path)
    original = sources.normalize_identity(reference, AUTO_GAME)
    changed = copy.deepcopy(reference)
    changed["stats"]["Home"]["FullGame"].reverse()
    changed["stats"]["Home"]["FullGame"][0]["Assists"] = 999
    changed["last_updated"] = "later"
    assert original == sources.normalize_identity(changed, AUTO_GAME)
    changed["stats"]["Home"]["FullGame"][0]["EntityId"] = "20"
    with pytest.raises(ValueError, match="duplicate"):
        sources.normalize_identity(changed, AUTO_GAME)


class Blob:
    def __init__(self, job_id=None):
        self.generation = 1
        self.body = json.dumps({"job_id": job_id, "location": "US"}).encode()

    def reload(self, **kwargs):
        pass

    def download_as_bytes(self, **kwargs):
        return self.body

    def upload_from_string(self, body, if_generation_match, **kwargs):
        if if_generation_match != self.generation:
            raise PreconditionFailed("changed")
        self.body = body.encode()
        self.generation += 1


def guard(blob, job):
    def get_job(*args, **kwargs):
        if isinstance(job, Exception):
            raise job
        return job

    store = SimpleNamespace(
        bucket=SimpleNamespace(
            blob=lambda path: blob,
            iam_configuration=SimpleNamespace(
                uniform_bucket_level_access_enabled=False
            ),
        ),
        prefix="dev",
        location="US",
        bq=SimpleNamespace(get_job=get_job),
    )
    return worker.QueryCheckpoint(store, "game")


@pytest.mark.parametrize("state", ["RUNNING", "PENDING", "missing"])
def test_unknown_cloud_commit_blocks_a_new_query(state):
    blob = Blob("old_job")
    checkpoint = guard(
        blob,
        NotFound("missing") if state == "missing" else SimpleNamespace(state=state),
    )
    with pytest.raises(worker.UncertainJob):
        checkpoint.recover()
    assert json.loads(blob.body)["job_id"] == "old_job"


def test_terminal_query_recovery_and_generation_guard():
    blob = Blob("finished_job")
    checkpoint = guard(blob, SimpleNamespace(state="DONE"))
    checkpoint.recover()
    assert json.loads(blob.body)["job_id"] is None
    checkpoint("new_job")
    assert json.loads(blob.body)["job_id"] == "new_job"
    blob.generation += 1
    with pytest.raises(PreconditionFailed):
        checkpoint(None)


def test_discovery_registers_full_denominator_and_returns_bounded_work(
    tmp_path, monkeypatch
):
    games = [{**AUTO_GAME, "game_id": f"002250000{i}"} for i in range(1, 4)]
    monkeypatch.setattr(sources, "discover", lambda *args: games)
    monkeypatch.setattr(worker, "coverage", lambda *args: [])
    registered = []
    monkeypatch.setattr(
        worker, "register_completed", lambda store, rows: registered.extend(rows)
    )
    store = LocalCloud(tmp_path / "store")
    output = tmp_path / "discovery"
    output.mkdir()
    selected = worker.run_discovery(
        output, "2025-26", NOW, store, None, limit=1, discovery_source="schedule"
    )
    assert registered == games and selected == games[:1]
    report = json.loads((output / "discovery.json").read_text())
    assert report["discovered"] == 3 and report["deferred"] == 2
    store.local.close()


def test_discovery_cannot_silently_drop_known_games(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "discover", lambda *args: [])
    monkeypatch.setattr(
        worker, "coverage", lambda *args: [{"game_id": AUTO_GAME["game_id"]}]
    )
    monkeypatch.setattr(
        worker, "register_completed", lambda *args: pytest.fail("must not register")
    )
    store = LocalCloud(tmp_path / "store")
    output = tmp_path / "discovery"
    output.mkdir()
    with pytest.raises(ValueError, match="lost previously"):
        worker.run_discovery(
            output, "2025-26", NOW, store, None, discovery_source="schedule"
        )
    assert store.uploads  # observation evidence survives rejection
    store.local.close()


def test_official_boxscore_identity_requires_matching_game_date_teams_and_final(
    tmp_path,
):
    _, reference = synthetic_sources(tmp_path)
    game = {
        "gameId": AUTO_GAME["game_id"],
        "gameStatus": 3,
        "gameTimeLocal": "2025-10-22T19:00:00",
    }
    for side in ("home", "away"):
        game[f"{side}Team"] = {
            "teamTricode": AUTO_GAME[side],
            "players": [
                {"personId": p["EntityId"], "name": p["Name"]}
                for p in reference["stats"][side.title()]["FullGame"]
            ],
        }
    assert sources.boxscore_identity(
        {"game": game}, AUTO_GAME
    ) == sources.normalize_identity(reference, AUTO_GAME)
    for key, value in (
        ("gameId", "0022500099"),
        ("gameStatus", 2),
        ("gameTimeLocal", "2025-10-23"),
    ):
        bad = copy.deepcopy(game)
        bad[key] = value
        with pytest.raises(ValueError):
            sources.boxscore_identity({"game": bad}, AUTO_GAME)
    game["homeTeam"]["teamTricode"] = "CCC"
    with pytest.raises(ValueError, match="team"):
        sources.boxscore_identity({"game": game}, AUTO_GAME)


def test_provider_team_aggregate_is_not_a_player_or_duplicate_id(tmp_path):
    _, reference = synthetic_sources(tmp_path)
    expected = sources.normalize_identity(reference, AUTO_GAME)
    for side in ("Home", "Away"):
        reference["stats"][side]["FullGame"].append({"Name": "Team", "EntityId": "0"})
    assert sources.normalize_identity(reference, AUTO_GAME) == expected
    reference["stats"]["Home"]["FullGame"][-1]["Name"] = "Unknown Player"
    with pytest.raises(ValueError, match="Invalid"):
        sources.normalize_identity(reference, AUTO_GAME)
