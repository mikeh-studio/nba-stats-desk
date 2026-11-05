import asyncio
import copy
import socket

import pytest
from app.serving.core_application import Ask, create_core_application
from app.serving.core_models import build_models
from app.serving.publication_contract import digest
from fastapi.testclient import TestClient
from scripts.build_synthetic_core_replay import synthetic_bundle


@pytest.fixture(autouse=True)
def no_external_network(monkeypatch):
    def denied(*a, **kw):
        raise AssertionError("no external network")

    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    original_connect = socket.socket.connect

    def connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            denied()
        return original_connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", connect)


@pytest.fixture
def bundle():
    return synthetic_bundle()


def metric(model, key, aggregation):
    return next(
        m
        for m in model["metrics"]
        if (m["metric"], m["aggregation"]) == (key, aggregation)
    )


def test_independent_expected_math_trade_dnp_and_zero(bundle):
    m = bundle["models"]
    a = next(r for r in m["core_player_summary"] if r["player_id"] == 101)
    b = next(r for r in m["core_player_summary"] if r["player_id"] == 202)
    assert a["observed_games"] == 2
    assert metric(a, "pts", "total")["value"] == 10
    assert metric(a, "pts", "per_game")["value"] == 5
    assert metric(a, "fg_pct", "ratio")["value"] == pytest.approx(4 / 20)
    assert metric(b, "fg_pct", "ratio")["value"] == pytest.approx(13 / 20)
    assert metric(a, "min", "total")["valid_games"] == 1
    assert metric(a, "pts_per36", "per36")["value"] == 12
    assert len(m["participant_manifest"]) == 6 and len(m["fct_player_game_stats"]) == 4
    assert {
        r["team_abbr"] for r in m["fct_player_game_stats"] if r["player_id"] == 101
    } == {"LAL", "BOS"}
    assert sorted(r["team_pts"] for r in m["fct_team_game_results"]) == [0, 10, 12, 20]
    assert len(m["core_player_windows"]) == 8 and len(m["core_player_trends"]) == 38
    assert all(r["change"] is None for r in m["core_player_trends"])
    empty = next(r for r in m["core_player_windows"] if r["window_key"] == "prior_5")
    assert metric(empty, "gp", "total")["value"] == 0
    assert metric(empty, "pts", "total")["value"] is None
    assert (
        build_models(
            bundle["accepted"], materialized_at=bundle["manifest"]["materialized_at"]
        )
        == m
    )


def test_nba_seconds_survive_modeling_and_are_not_called_whole_minutes(bundle):
    accepted = copy.deepcopy(bundle["accepted"])
    accepted["scope"]["provider"] = "nba_api"
    for row in accepted["appearances"]:
        if row["min"] is not None:
            row["minutes_precision"] = "second_reported"
    row = next(row for row in accepted["appearances"] if row["min"] is not None)
    row["min"] = 733 / 60
    models = build_models(
        accepted,
        materialized_at=bundle["manifest"]["materialized_at"],
        release_scope=accepted["scope"],
    )
    fact = next(
        r
        for r in models["fct_player_game_stats"]
        if r["player_id"] == row["player_id"] and r["game_id"] == row["game_id"]
    )
    assert fact["min"] == pytest.approx(733 / 60)
    performance = next(
        r
        for r in models["core_performance"]
        if r["player_id"] == row["player_id"] and r["game_id"] == row["game_id"]
    )
    assert metric(performance, "min", "total")["precision"] == "second_reported"
    row["min"] = 12.001
    with pytest.raises(ValueError, match="minute precision"):
        build_models(
            accepted,
            materialized_at=bundle["manifest"]["materialized_at"],
            release_scope=accepted["scope"],
        )


def test_reused_scope_metrics_remain_independent_of_player_summary(bundle):
    models = bundle["models"]
    summary = next(
        row for row in models["core_player_summary"] if row["player_id"] == 101
    )
    window = next(
        row
        for row in models["core_player_windows"]
        if row["player_id"] == 101 and row["window_key"] == "scope"
    )
    assert window["metrics"] == summary["metrics"]
    metric(window, "pts", "total")["value"] = -1
    assert metric(summary, "pts", "total")["value"] == 10


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate",
        "dnp",
        "unknown",
        "count_null",
        "minute_fraction",
        "wrong_side",
        "score",
        "phase",
        "alias",
        "missing_participant",
        "duplicate_game",
        "held",
    ],
)
def test_invalid_inputs_do_not_build(bundle, mutation):
    a = copy.deepcopy(bundle["accepted"])
    if mutation == "duplicate":
        a["appearances"].append(copy.deepcopy(a["appearances"][0]))
    elif mutation == "dnp":
        a["participants"][0]["played"] = False
    elif mutation == "unknown":
        a["participants"][0]["played"] = None
    elif mutation == "count_null":
        a["appearances"][0]["ast"] = None
    elif mutation == "minute_fraction":
        a["appearances"][0]["min"] = 2.5
    elif mutation == "wrong_side":
        a["appearances"][0]["home_away"] = "AWAY"
    elif mutation == "score":
        a["games"][0]["home_score"] = 99
    elif mutation == "phase":
        a["games"][0]["season_type"] = "Playoffs"
    elif mutation == "alias":
        a["games"][0]["game_id"] = "synthetic_uid"
    elif mutation == "missing_participant":
        a["participants"].pop(0)
    elif mutation == "duplicate_game":
        a["games"].append(copy.deepcopy(a["games"][0]))
    elif mutation == "held":
        a["status"] = "held"
    with pytest.raises((ValueError, KeyError)):
        build_models(a, materialized_at=bundle["manifest"]["materialized_at"])


def application(bundle, state=None):
    state = (
        state
        if state is not None
        else {"allowed": True, "clock": "2026-11-01T16:00:00Z"}
    )
    return create_core_application(
        bundle,
        expected_digest=digest(bundle),
        clock=lambda: state["clock"],
        authorize=lambda *_: state["allowed"],
    )


def test_stream_authority_error_becomes_error_event(bundle):
    def unavailable(*_):
        raise ValueError("warehouse dependency revoked after request started")

    app = create_core_application(
        bundle,
        expected_digest=digest(bundle),
        clock=lambda: "2026-11-01T16:00:00Z",
        authorize=unavailable,
    )
    endpoint = next(r.endpoint for r in app.routes if r.path == "/api/agent/ask/stream")
    response = endpoint(Ask(question="Test Player A total points"))

    async def collect():
        return "".join([chunk async for chunk in response.body_iterator])

    stream = asyncio.run(collect())
    assert "event: error" in stream and "event: final" not in stream


def test_actual_routes_and_scope_limits(bundle):
    with TestClient(
        application(bundle), base_url="http://127.0.0.1", client=("127.0.0.1", 45678)
    ) as c:
        for path in [
            "/ask",
            "/players/101",
            "/players/101/content",
            "/compare?player_a_id=101&player_b_id=202",
            "/performance",
        ]:
            r = c.get(path)
            assert r.status_code == 200, r.text
            assert "Offline synthetic replay" in r.text and "Oct 20" in r.text
        assert len(c.get("/api/players/101/game-log").json()["data"]) == 2
        assert c.get("/api/performance/dates").json()["data"] == [
            "2026-10-21",
            "2026-10-20",
        ]
        assert (
            len(c.get("/api/performance/games?game_date=2026-10-20").json()["data"])
            == 1
        )
        assert (
            len(c.get("/api/performance/players?game_id=0022600001").json()["data"])
            == 2
        )
        assert len(c.get("/api/performance/players/101").json()["data"]) == 2
        assert len(c.get("/api/players/search?q=Test").json()["data"]) == 2
        r = c.post("/api/agent/ask", json={"question": "Test Player A total points"})
        assert r.status_code == 200, r.text
        assert "10.0" in r.text
        r = c.post(
            "/api/agent/ask/stream", json={"question": "Test Player A ratio fg%"}
        )
        assert r.status_code == 200 and "event: final" in r.text and "0.2" in r.text
        fg = next(
            r
            for r in c.get("/api/compare?player_a_id=101&player_b_id=202").json()[
                "data"
            ]["differences"]
            if r["metric"] == "fg_pct"
        )
        assert (
            fg["difference"] == pytest.approx(-45) and fg["unit"] == "percentage_points"
        )
        for q in [
            "Who leads the league in points",
            "Test Player A total points last week",
            "Test Player A total points from 2026-10-01 to 2026-10-31",
            "Lakers total points",
        ]:
            assert c.post("/api/agent/ask", json={"question": q}).status_code == 422
        assert c.get("/api/players/101?season=2025-26").status_code == 422
        assert c.get("/api/players/303").status_code == 404
        assert (
            c.get("/api/health", headers={"X-Publication-ID": "other"}).status_code
            == 409
        )
        assert (
            c.get("/api/health", headers={"X-Forwarded-For": "127.0.0.1"}).status_code
            == 403
        )


def test_revocation_expiry_and_tamper(bundle):
    bad = copy.deepcopy(bundle)
    bad["models"]["core_player_summary"][0]["observed_games"] = 99
    with pytest.raises(ValueError):
        application(bad)
    state = {"allowed": True, "clock": "2026-11-01T16:00:00Z"}
    with TestClient(
        application(bundle, state),
        base_url="http://127.0.0.1",
        client=("127.0.0.1", 45678),
    ) as c:
        assert c.get("/api/health").status_code == 200
        state["allowed"] = False
        assert c.get("/api/players/101").status_code == 503
        state.update(allowed=True, clock="2026-11-12T00:00:00Z")
        assert c.get("/api/health").status_code == 503


def test_compare_ask_and_date_ranges_do_not_mutate_bundle(bundle):
    with TestClient(
        application(bundle), base_url="http://127.0.0.1", client=("127.0.0.1", 45678)
    ) as c:
        r = c.post(
            "/api/agent/ask",
            json={"question": "compare Test Player A vs Test Player B ratio fg%"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["data"]["differences"][0]["difference"] == pytest.approx(-45)
        assert (
            len(
                c.get("/api/compare?player_a_id=101&player_b_id=202").json()["data"][
                    "differences"
                ]
            )
            == 32
        )
        r = c.get(
            "/api/compare?player_a_id=101&player_b_id=202&first=2026-10-20&last=2026-10-20"
        )
        fg = next(m for m in r.json()["data"]["differences"] if m["metric"] == "fg_pct")
        assert fg["difference"] == pytest.approx(30)
        assert (
            c.get(
                "/api/compare?player_a_id=101&player_b_id=202&first=2026-10-01&last=2026-10-20"
            ).status_code
            == 422
        )
        assert (
            c.post(
                "/api/agent/ask",
                json={
                    "question": "compare Test Player A vs Test Player B ratio fg% from 2026-13-01 to 2026-10-20"
                },
            ).status_code
            == 422
        )


def test_full_windows_and_overtime_minutes(bundle):
    accepted = copy.deepcopy(bundle["accepted"])
    # Ten consecutive synthetic final games; the independent expectation is +10 PPG.
    first = copy.deepcopy(accepted)
    accepted.update(games=[], appearances=[], participants=[])
    for i in range(10):
        gid = f"0022600{i:03}"
        game = copy.deepcopy(first["games"][0])
        game.update(
            game_id=gid, game_uid=f"synthetic-{i}", game_date=f"2026-10-{20 + i:02}"
        )
        for row in first["appearances"][:2]:
            r = copy.deepcopy(row)
            r["game_id"] = gid
            if r["player_id"] == 101:
                r.update(min=53, minutes_precision="whole_minute_reported")
                if i >= 5:
                    r["pts"] += 10
                    r["ftm"] += 10
                    r["fta"] += 10
                    game["home_score"] += 10
            accepted["appearances"].append(r)
        accepted["games"].append(game)
        accepted["participants"] += [
            dict(r, game_id=gid) for r in first["participants"][:3]
        ]
    m = build_models(accepted, materialized_at=bundle["manifest"]["materialized_at"])
    trend = next(
        r
        for r in m["core_player_trends"]
        if r["player_id"] == 101 and r["metric"] == "pts"
    )
    assert (
        trend["change"] == 10
        and trend["recent_valid_games"] == trend["prior_valid_games"] == 5
    )
    assert (
        next(r for r in m["core_player_summary"] if r["player_id"] == 101)[
            "observed_games"
        ]
        == 10
    )


@pytest.mark.parametrize("bad_id", [None, 0, -1, True, "303"])
def test_dnp_identity_required(bundle, bad_id):
    accepted = copy.deepcopy(bundle["accepted"])
    next(p for p in accepted["participants"] if p["played"] is False)["player_id"] = (
        bad_id
    )
    with pytest.raises(ValueError):
        build_models(accepted, materialized_at=bundle["manifest"]["materialized_at"])


@pytest.mark.parametrize(
    "field",
    [
        "game_id",
        "game_uid",
        "observation_id",
        "fact_revision",
        "result_revision",
        "missing_count",
    ],
)
def test_rehashed_manifest_cannot_misdescribe_model_rows(bundle, field):
    from app.serving.publication_contract import allocation_key, fingerprint

    bad = copy.deepcopy(bundle)
    manifest = bad["manifest"]
    if field == "missing_count":
        next(f for f in manifest["fields"] if f["name"] == "min").update(
            missing_count=0, status="complete"
        )
    else:
        manifest["selected_games"][0][field] = (
            "0022699999"
            if field == "game_id"
            else "other_uid"
            if field == "game_uid"
            else "a" * 64
        )
    manifest["allocation_key"] = allocation_key(manifest)
    manifest["input_fingerprint"] = fingerprint(manifest)
    for asset in manifest["assets"]:
        asset["built_from"] = manifest["input_fingerprint"]
    bad["binding"]["manifest_digest"] = digest(manifest)
    with pytest.raises(ValueError):
        application(bad)


def test_distributions_freshness_labels_and_unknown_filters(bundle):
    # A separate synthetic fixture creates a stale-but-unexpired interval.
    # The baseline fixture's earlier source deadline deliberately has none.
    bundle = copy.deepcopy(bundle)
    bundle["manifest"]["freshness"].update(
        source_valid_until="2026-11-10T12:00:00Z", expires_at="2026-11-10T12:00:00Z"
    )
    bundle["binding"]["expires_at"] = "2026-11-10T12:00:00Z"
    bundle["binding"]["manifest_digest"] = digest(bundle["manifest"])
    state = {"allowed": True, "clock": "2026-11-01T16:00:00Z"}
    with TestClient(
        application(bundle, state),
        base_url="http://127.0.0.1",
        client=("127.0.0.1", 45678),
    ) as c:
        url = "/api/compare?player_a_id=101&player_b_id=202"
        d = c.get(url).json()["data"]["distributions"][0]
        assert len(d["games"]) == 2
        zero = d["games"][0]["metrics"]
        assert next(m for m in zero if m["metric"] == "pts")["value"] == 0
        assert next(m for m in zero if m["metric"] == "min")["value"] is None
        assert (
            len(
                c.get(url + "&first=2026-10-20&last=2026-10-20").json()["data"][
                    "distributions"
                ][0]["games"]
            )
            == 1
        )
        assert not c.get(url + "&window=prior_5").json()["data"]["distributions"][0][
            "games"
        ]
        assert c.get(url + "&start_date=2026-10-20").status_code == 422
        for path in [
            "/api/players/101/game-log?start_date=2026-10-20",
            "/api/performance/players/101?game_id=0022600001",
            "/api/performance/players?game_id=0022600001&game_date=2026-10-20",
            "/ask?season_type=Playoffs",
        ]:
            assert c.get(path).status_code == 422
        assert "Freshness: fresh" in c.get("/ask").text
        state["clock"] = "2026-11-09T13:00:00Z"
        assert "Freshness: stale_usable" in c.get("/ask").text
