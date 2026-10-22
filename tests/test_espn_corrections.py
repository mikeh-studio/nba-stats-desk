import copy
from datetime import datetime, timezone

import pytest
from scripts.espn_corrections import (
    changes,
    next_check,
    normalize,
    plan,
    revision,
    validate,
)
from scripts.espn_sources import scoreboard

from test_espn_pilot import DAY, SEASON, fixtures


def inputs():
    schedule, payload = fixtures()
    # A third teammate lets us test a genuine assist reassignment, not self-assists.
    athletes = payload["boxscore"]["players"][0]["statistics"][0]["athletes"]
    third = copy.deepcopy(athletes[0])
    third["athlete"] = {"id": "102", "displayName": "Drew Dale"}
    third["stats"][6] = "0"
    athletes.append(third)
    return scoreboard(schedule, SEASON, DAY)[0], payload


def corrected_box(payload):
    result = copy.deepcopy(payload)
    athletes = result["boxscore"]["players"][0]["statistics"][0]["athletes"]
    athletes[0]["stats"][6] = "0"
    athletes[2]["stats"][6] = "1"
    return result


def corrected_pbp(payload):
    result = copy.deepcopy(payload)
    result["plays"][0]["text"] = result["plays"][0]["text"].replace(
        "Avery Able", "Drew Dale"
    )
    result["plays"][0]["participants"][0]["athlete"]["id"] = "102"
    return result


def test_results_survive_missing_box_and_pbp():
    game, payload = inputs()
    expected = normalize(payload, game, "result")
    payload.pop("boxscore")
    payload.pop("plays")
    assert normalize(payload, game, "result") == expected
    with pytest.raises(KeyError):
        normalize(payload, game, "box")


@pytest.mark.parametrize("defect", ["missing", "score", "assist"])
def test_box_survives_pbp_defects(defect):
    game, payload = inputs()
    expected = normalize(payload, game, "box")
    if defect == "missing":
        payload.pop("plays")
    elif defect == "score":
        payload["plays"][-1]["homeScore"] = 99
    else:
        payload = corrected_pbp(payload)
    assert normalize(payload, game, "box") == expected
    with pytest.raises((ValueError, KeyError)):
        normalize(payload, game, "pbp")


def test_invalid_box_does_not_change_result():
    game, payload = inputs()
    result = normalize(payload, game, "result")
    payload["boxscore"]["players"][0]["statistics"][0]["athletes"][0]["stats"][1] = "99"
    assert normalize(payload, game, "result") == result
    with pytest.raises(ValueError, match="arithmetic"):
        normalize(payload, game, "box")


def test_delayed_assist_correction_binds_exact_box_revision():
    game, original = inputs()
    old = normalize(original, game, "pbp")
    box = corrected_box(original)
    assert revision(normalize(box, game, "result")) == old["result_revision"]
    assert revision(normalize(box, game, "box")) != old["box_revision"]
    with pytest.raises(ValueError, match="assist/PBP"):
        normalize(box, game, "pbp")
    final = normalize(corrected_pbp(box), game, "pbp")
    assert final["assists"][0]["assister_source_id"] == "102"
    assert final["box_revision"] == revision(normalize(box, game, "box"))
    assert changes(old, final)


@pytest.mark.parametrize("layer", ["result", "box", "pbp"])
def test_replay_and_forgery(layer):
    game, payload = inputs()
    candidate = normalize(payload, game, layer)
    payload["news"] = "irrelevant"
    assert validate(normalize(payload, game, layer)) == validate(candidate)
    candidate["game"]["home_score"] = 7
    with pytest.raises(ValueError):
        validate(candidate)


def test_policy_milestones_and_dst():
    # October 2025 crosses the November DST transition by the D+14 check.
    assert (
        next_check(DAY, "2025-10-23T15:59:00+00:00", False).isoformat()
        == "2025-10-23T16:00:00+00:00"
    )
    assert (
        next_check(DAY, "2025-10-29T16:00:00+00:00", False).isoformat()
        == "2025-11-05T17:00:00+00:00"
    )
    assert (
        next_check(DAY, "2025-11-05T17:00:00+00:00", True).isoformat()
        == "2025-11-12T17:00:00+00:00"
    )
    assert next_check(DAY, "2025-11-21T17:00:00+00:00", True) is None


def coverage(checked, status="accepted"):
    return {
        "source_game_id": "1234567",
        "season": SEASON,
        "game_date": str(DAY),
        "last_checked_at": checked,
        "result_status": "accepted",
        "box_status": "accepted",
        "pbp_status": status,
    }


def test_overdue_checks_catch_up_and_replays_do_not_postpone():
    row = coverage("2025-10-29T17:00:00+00:00")
    now = datetime(2025, 11, 6, 20, tzinfo=timezone.utc)
    result = plan(SEASON, now, [row])
    assert str(DAY) in result["dates"] and result["due_games"] == ["1234567"]
    # Replaying an old capture leaves its timestamp; a fresh fetch fulfills the check.
    assert plan(SEASON, now, [row]) == result
    row["last_checked_at"] = now
    assert str(DAY) not in plan(SEASON, now, [row])["dates"]


def test_unresolved_games_exit_to_exception_queue():
    now = datetime(2025, 11, 24, 20, tzinfo=timezone.utc)
    work = plan(
        SEASON, now, [coverage("2025-11-21T17:00:00+00:00", "pending_correction")]
    )
    assert str(DAY) not in work["dates"]
    assert work["exceptions"][0]["source_game_id"] == "1234567"
    assert not plan(SEASON, now, [coverage("2025-11-21T17:00:00+00:00")])["exceptions"]


def test_correction_window_can_extend_past_season_boundary():
    row = {**coverage("2026-06-30T23:00:00+00:00"), "game_date": "2026-06-30"}
    work = plan(SEASON, datetime(2026, 7, 14, 20, tzinfo=timezone.utc), [row])
    assert work["dates"] == ["2026-06-30"]


def test_failed_layer_validation_is_independent(tmp_path):
    from scripts.espn_layer_ingestion import ingest_game

    class Store:
        def publish(self, value, *args, **kwargs):
            validate(value)
            return {"layer": value["layer"], "status": "published"}

        def failure(self, game, layer, *args):
            return {"layer": layer, "status": "rejected"}

    game, payload = inputs()
    payload.pop("plays")
    result = ingest_game(
        Store(), payload, game, "synthetic", "2025-10-23T04:00:00+00:00", tmp_path
    )
    assert [r["status"] for r in result["layers"]] == [
        "published",
        "published",
        "rejected",
    ]


@pytest.mark.parametrize("mode", ["fresh", "replay", "missing_final", "empty"])
def test_worker_discovery_capture_and_failure_visibility(tmp_path, monkeypatch, mode):
    import json
    from types import SimpleNamespace

    from scripts import espn_layer_ingestion as worker
    from scripts.fetch_gamebooks import digest

    schedule, payload = fixtures()
    game = scoreboard(schedule, SEASON, DAY)[0]
    observed = "2025-10-23T04:00:00+00:00"
    if mode in ("missing_final", "empty"):
        schedule["events"] = []
    capture = tmp_path / "capture"

    def capture_json(url, directory, session):
        directory.mkdir(parents=True)
        data = schedule if "/scoreboard?" in url else payload
        body = json.dumps(data).encode()
        (directory / "response.body").write_bytes(body)
        (directory / "attempt.json").write_text(
            json.dumps(
                {
                    "url": url,
                    "status": "captured",
                    "http_status": 200,
                    "sha256": digest(body),
                    "finished_at": observed,
                }
            )
        )
        return data

    monkeypatch.setattr(worker, "capture_json", capture_json)
    monkeypatch.setattr(worker, "archive_sources", lambda *args: "synthetic-evidence")
    seen, rejected = [], []

    class Store:
        prefix = "synthetic"
        cloud = SimpleNamespace()

        def coverage(self, season):
            return (
                [{"source_game_id": game["source_game_id"], "game_date": DAY}]
                if mode == "missing_final"
                else []
            )

        def register_games(self, games):
            pass

        def publish(self, candidate, evidence, captured_at, **kwargs):
            validate(candidate)
            seen.append(captured_at.isoformat())
            return {"layer": candidate["layer"], "status": "published"}

        def failure(self, game, layer, error, evidence, *args):
            rejected.append(layer)
            return {"layer": layer, "status": "rejected"}

    output = tmp_path / "output"
    output.mkdir()
    if mode == "replay":
        capture_json(
            worker.source.BASE + "/scoreboard?dates=" + DAY.strftime("%Y%m%d"),
            capture / "sources" / str(DAY),
            None,
        )
        capture_json(
            worker.source.BASE + "/summary?event=" + game["source_game_id"],
            capture / "games" / game["source_game_id"] / "source",
            None,
        )

        def no_fetch(*args):
            pytest.fail("Replay must not fetch or replace capture timestamps")

        monkeypatch.setattr(worker, "capture_json", no_fetch)
    result = worker.run(
        Store(), SEASON, [DAY], output, None, capture if mode == "replay" else None
    )
    if mode in ("fresh", "replay"):
        assert seen == [observed] * 3 and not rejected
    elif mode == "missing_final":
        assert rejected == ["result", "box", "pbp"]
        assert result["discovery"][0]["status"] == "failed"
    else:
        assert not seen and not rejected and result["discovery"][0]["finals"] == 0
