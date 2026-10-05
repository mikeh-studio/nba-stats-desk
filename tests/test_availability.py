"""Generated unseen-pair questions, independent arithmetic and scope attribution."""

import json
from copy import deepcopy

import pytest
from app import main
from app.agent.availability_ask import parse_request, render_answer, wants_availability
from app.agent.conversation import InMemoryConversationStore
from app.agent.semantic_planner import plan_question
from app.agent.semantic_serving import source_players
from app.agent.semantic_source import snapshot_digest
from app.agent.semantics import COMPONENTS, SemanticError
from app.availability import compare_availability, load_availability
from app.research_snapshots import append_snapshot, digest
from tests.test_api import _test_settings, build_client
from tests.test_teammate_ask import Client


@pytest.fixture
def bundle(tmp_path):
    rows, games, reports = [], [], []
    for day in range(1, 9):
        gid, date = f"g{day}", f"2025-11-{day:02}"
        row = dict.fromkeys(COMPONENTS, 0)
        row.update(
            season="2025-26",
            season_type="Regular Season",
            game_id=gid,
            game_date=date,
            player_id=811,
            player_name="Avery Finch",
            team_abbr="ATL",
            opponent_abbr="BOS",
            home_away="home" if day % 2 else "away",
            min=30,
            pts={1: 20, 2: 40, 3: 10, 4: 30}.get(day, 99),
            ast=day,
            fgm={1: 1, 2: 4, 3: 0, 4: 6}.get(day, 1),
            fga={1: 1, 3: 9}.get(day, 10),
        )
        rows.append(row)
        game = dict(
            season="2025-26",
            season_type="Regular Season",
            game_id=gid,
            game_date=date,
            team_abbr="ATL",
            opponent_abbr="BOS",
            home_away=row["home_away"],
            scheduled_start_utc=date + "T20:00:00Z",
            final=day != 8,
            postponed=False,
        )
        games.append(game)
        if day in (1, 3, 6, 7):
            other = {
                **row,
                "player_id": 822,
                "player_name": "Blake Reed",
                "team_abbr": "BOS" if day == 6 else "ATL",
            }
            rows.append(other)
            if day == 6:
                games.append({**game, "team_abbr": "BOS", "opponent_abbr": "ATL"})
        if day in (2, 4, 6, 7, 8):
            reports.append(
                dict(
                    season="2025-26",
                    game_date=date,
                    team_abbr="ATL",
                    player_id=822,
                    matchup="BOS@ATL" if day % 2 else "ATL@BOS",
                    report_timestamp_utc=date + "T18:00:00Z",
                    injury_status="Out",
                    reason="fixture",
                    source_url=f"https://example.com/reports/{gid}",
                    ingested_at_utc=date + "T18:01:00Z",
                )
            )
    stats = dict(
        rows=rows,
        sources=["synthetic fixture"],
        coverage=[
            dict(
                season="2025-26",
                phase="Regular Season",
                rows=len(rows),
                data_through="2025-11-08",
            )
        ],
    )
    stats["sha256"] = snapshot_digest(stats)
    doc = dict(
        version=1,
        artifact_type="availability_evidence/v1",
        kind="reconstructed",
        as_of_ts="2026-01-01T00:00:00Z",
        captured_at="2026-01-01T00:00:00Z",
        input_hashes={"fixture": "synthetic"},
        rows=[],
        stats=stats,
        games=games,
        reports=reports,
    )
    doc["sha256"] = digest(doc)
    path = tmp_path / "availability.json"
    append_snapshot(path, doc)
    return path, doc


def request(
    loaded, question="How did Avery Finch play while Blake Reed was out?", previous=None
):
    return parse_request(
        question, source_players(loaded[1]), "2025-26", previous, {"ATL", "BOS"}
    )


@pytest.mark.parametrize(
    "template",
    [
        "Tell me how {a} played while {b} was out",
        "How did {a} perform without {b}?",
        "Show {a} points and assists when {b} was absent",
        "When {b} was out, how did {a} play?",
        "Compare {a} stats when {b} was out versus when both played",
    ],
)
def test_generated_unregistered_questions(template, bundle):
    loaded = load_availability(str(bundle[0]))
    q = template.format(a="Avery Finch", b="Blake Reed")
    assert wants_availability(q)
    result = compare_availability(loaded, request(loaded, q))
    assert {k: [g["game_id"] for g in v] for k, v in result["groups"].items()} == {
        "both_played": ["g1", "g3", "g7"],
        "did_not_play": ["g2", "g4"],
    }
    pts = next(m for m in result["metrics"] if m["metric"] == "pts")
    assert pts["groups"]["both_played"]["value"] == 43
    assert pts["groups"]["did_not_play"]["value"] == 35
    assert pts["difference"] == -8
    assert result["excluded"] == {
        "unverified_status_or_membership": 1,
        "not_teammates": 1,
        "unfinished_game": 1,
    }
    payload = render_answer(result, source_players(loaded[1]))
    assert "Avery Finch" in payload["answer"] and "Blake Reed" in payload["answer"]
    assert payload["player_profile"]["player"]["player_id"] == 811
    for group, sample in result["groups"].items():
        for game in sample:
            assert game["player_id"] == 811 and game["teammate_id"] == 822
            if group == "did_not_play":
                assert game["source_urls"]
    assert payload["charts"][0]["series"][0]["points"][1]["y"] == 35


@pytest.mark.parametrize(
    "suffix",
    [
        " in the fourth quarter",
        " over the last five games",
        " excluding the playoffs",
        " not in the playoffs",
        " due to injury",
        " adjusted for opponent strength",
        " and predict tomorrow",
        " per possession",
        " against Boston",
        " at home and on the road",
    ],
)
def test_unknown_conditions_never_disappear(bundle, suffix):
    loaded = load_availability(str(bundle[0]))
    with pytest.raises(SemanticError):
        request(loaded, "How did Avery Finch play without Blake Reed" + suffix)


def test_filters_ratios_totals_and_followup_scope(bundle):
    loaded = load_availability(str(bundle[0]))
    q = request(
        loaded, "Show Avery Finch field goal percentage when Blake Reed was out"
    )
    result = compare_availability(loaded, q)
    metric = result["metrics"][0]
    assert metric["groups"]["both_played"]["display_value"] == 10
    assert metric["groups"]["did_not_play"]["display_value"] == 50
    assert metric["difference"] == 40
    q = request(
        loaded,
        "Show Avery Finch total points without Blake Reed from 2025-11-02 to 2025-11-04 on the road against BOS",
    )
    result = compare_availability(loaded, q)
    assert result["metrics"][0]["groups"]["did_not_play"]["value"] == 70
    followup = request(loaded, "What about assists?", q)
    assert followup == {**q, "metrics": ["ast"]}
    assert (
        compare_availability(loaded, followup)["metrics"][0]["groups"]["did_not_play"][
            "value"
        ]
        == 6
    )
    with pytest.raises(SemanticError):
        compare_availability(loaded, request(loaded, "Only the playoffs", q))


def test_general_planner_cannot_drop_condition():
    class NeverCall:
        @property
        def responses(self):
            pytest.fail("Unsafe scope must be blocked before model planning")

    result = plan_question(
        NeverCall(),
        model="fixture",
        question="Tell me how Steph Curry played while Draymond Green was out",
        selected_season="2025-26",
    )
    assert result["status"] == "unsupported" and not result["queries"]


def test_json_sse_followups_and_no_model_calls(bundle, monkeypatch):
    planner = Client({})
    settings = _test_settings(
        openai_api_key="test-key",
        research_availability_path=str(bundle[0]),
        agent_rate_limit_per_minute=0,
        agent_rate_limit_daily=0,
    )
    client = build_client(settings=settings, agent_client=planner)
    store = InMemoryConversationStore()
    monkeypatch.setattr(main, "_season_conversation_store", lambda season: store)
    try:
        question = "How did Avery Finch play while Blake Reed was out?"
        payload = client.post(
            "/api/agent/ask", json={"question": question, "conversation_id": "dynamic"}
        ).json()
        assert payload["availability_scope"]["player_id"] == 811
        stream = client.post("/api/agent/ask/stream", json={"question": question})
        events = [
            json.loads(line[6:])
            for line in stream.text.splitlines()
            if line.startswith("data: ")
        ]
        final = next(e["payload"] for e in events if e.get("type") == "final")
        assert final["availability_evidence"] == payload["availability_evidence"]
        follow = client.post(
            "/api/agent/ask",
            json={"question": "What about assists?", "conversation_id": "dynamic"},
        ).json()
        assert follow["availability_scope"]["metrics"] == ["ast"]
        assert (
            follow["availability_evidence"]["metrics"][0]["groups"]["did_not_play"][
                "value"
            ]
            == 3
        )
        bad = client.post(
            "/api/agent/ask",
            json={"question": question + " only in the fourth quarter"},
        ).json()
        assert not bad["tables"] and bad["research_error_code"] == "unsupported_scope"
        assert planner.calls == 0
    finally:
        main.app.dependency_overrides.clear()


def test_missing_component_withholds_difference(bundle):
    loaded = load_availability(str(bundle[0]))
    copy = deepcopy(loaded)
    for row in copy[1].rows:
        if row["player_id"] == 811 and row["game_id"] == "g2":
            row["pts"] = None
    result = compare_availability(copy, request(copy))
    m = result["metrics"][0]
    assert m["groups"]["did_not_play"]["valid_games"] == 1
    assert m["groups"]["did_not_play"]["missing_component_games"] == 1
    assert m["difference"] is None
    payload = render_answer(result, source_players(copy[1]))
    assert "missing components" in payload["answer"]
    table = payload["tables"][0]
    assert [c["label"] for c in table["columns"]][-2:] == [
        "Games with data — both played",
        "Games with data — teammate did not play",
    ]
    # Sample completeness must remain per-stat, not the group size copied to every row.
    assert table["rows"][0][-2:] == ["3 of 3", "1 of 2"]
    assert table["rows"][0][3] == "unavailable"
    assert "Counts can differ by stat" in table["description"]


def test_alias_roles_and_ambiguity_are_data_driven(bundle):
    loaded = load_availability(str(bundle[0]))
    players = source_players(loaded[1])
    players[0]["aliases"].append("Ace")
    q = parse_request("How did Ace play when Blake was out?", players, "2025-26")
    assert (q["player_id"], q["teammate_id"]) == (811, 822)
    reverse = parse_request(
        "How did Blake Reed play when Avery Finch was out?", players, "2025-26"
    )
    assert (reverse["player_id"], reverse["teammate_id"]) == (822, 811)
    assert compare_availability(loaded, reverse)["groups"]["did_not_play"] == []
    players.append(dict(player_id=833, player_name="Blake Swift", aliases=[]))
    with pytest.raises(SemanticError, match="full names"):
        parse_request(
            "How did Avery Finch play when Blake was out?", players, "2025-26"
        )
    players[0]["observed_teams"] = ["ATL"]
    players[1]["observed_teams"] = ["ATL"]
    players[2]["observed_teams"] = ["BOS"]
    assert (
        parse_request(
            "How did Avery Finch play when Blake was out?", players, "2025-26"
        )["teammate_id"]
        == 822
    )


def test_checksum_and_schedule_fail_closed(bundle, tmp_path):
    path, doc = bundle
    altered = deepcopy(doc)
    altered["stats"]["rows"][0]["pts"] = 1000
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(altered))
    with pytest.raises(ValueError, match="checksum"):
        load_availability(str(bad))
    altered = deepcopy(doc)
    altered["games"].append(altered["games"][0])
    altered["sha256"] = digest({k: v for k, v in altered.items() if k != "sha256"})
    duplicate = tmp_path / "duplicate.json"
    append_snapshot(duplicate, altered)
    with pytest.raises(ValueError, match="Duplicate"):
        load_availability(str(duplicate))


def test_late_reports_and_bulletin_omissions_do_not_prove_absence(bundle):
    loaded = deepcopy(load_availability(str(bundle[0])))
    reports = loaded[3]
    key = ("2025-26", "2025-11-02", "ATL")
    base = reports[key][0]
    # A later team bulletin that omits this teammate invalidates the older listing.
    reports[key].append(
        {**base, "player_id": 899, "report_timestamp_utc": "2025-11-02T19:00:00Z"}
    )
    key4 = ("2025-26", "2025-11-04", "ATL")
    reports[key4][0]["report_timestamp_utc"] = "2025-11-04T21:00:00Z"
    result = compare_availability(loaded, request(loaded))
    assert result["groups"]["did_not_play"] == []
    assert all(m["difference"] is None for m in result["metrics"])
    payload = render_answer(result, source_players(loaded[1]))
    assert not payload["charts"]
    assert "Avery Finch" in payload["answer"] and "Blake Reed" in payload["answer"]


@pytest.mark.parametrize(
    "question",
    [
        "What were Avery Finch points during Blake Reed’s absence?",
        "How did Avery Finch perform when Blake Reed missed games?",
        "How did Avery Finch play with Blake Reed unavailable?",
    ],
)
def test_additional_absence_phrasing(bundle, question):
    loaded = load_availability(str(bundle[0]))
    assert wants_availability(question)
    assert request(loaded, question)["teammate_id"] == 822


@pytest.mark.parametrize(
    "question",
    [
        "How was Avery Finch scoring impacted by Blake Reed being out?",
        "Analyze Avery Finch points when Blake Reed was injured",
        "Show Avery Finch total points and average assists without Blake Reed",
        "Compare Avery Finch regular season versus playoffs without Blake Reed",
        "Compare Avery Finch without Blake Reed and Blake Reed without Avery Finch",
    ],
)
def test_unexecutable_or_conflicting_intent_cannot_be_partially_answered(
    bundle, question
):
    loaded = load_availability(str(bundle[0]))
    assert wants_availability(question)
    with pytest.raises(SemanticError):
        request(loaded, question)


@pytest.mark.parametrize("transport", ["/api/agent/ask", "/api/agent/ask/stream"])
@pytest.mark.parametrize("restore", ["live", "browser", "legacy_browser", "disk"])
def test_pair_followup_counts_full_appearances_after_restore(
    bundle, monkeypatch, tmp_path, transport, restore
):
    planner = Client({})
    settings = _test_settings(
        openai_api_key="test-key",
        research_availability_path=str(bundle[0]),
        agent_history_enabled=restore == "disk",
        agent_history_path=str(tmp_path / "history.jsonl"),
        agent_rate_limit_per_minute=0,
        agent_rate_limit_daily=0,
    )
    store = InMemoryConversationStore()
    monkeypatch.setattr(main, "_season_conversation_store", lambda season: store)
    client = build_client(settings=settings, agent_client=planner)
    question = (
        "How did Avery Finch play while Blake Reed was out in the regular season?"
    )
    first = client.post(
        "/api/agent/ask", json={"question": question, "conversation_id": "tab-a"}
    ).json()
    assert first["status"] == "ok"
    assert [p["player_id"] for p in first["conversation_context"]["players"]] == [
        811,
        822,
    ]
    prior = dict(first["conversation_context"], question=question)
    if restore == "legacy_browser":
        prior = {
            "question": question,
            "players": [{"player_id": 811, "player_name": "Avery Finch"}],
            "availability_scope": first["availability_scope"],
            "scope": {"phases": ["Regular Season"]},
        }
    if restore != "live":
        store = InMemoryConversationStore()
    # An unrelated tab's successful state must not replace tab A's context.
    store.append_turn(
        "tab-b",
        question="Other player",
        answer="ok",
        context={"players": [{"player_id": 999, "player_name": "Casey Vale"}]},
        max_turns=6,
    )
    body = {
        "question": "How many games do each player played this season",
        "conversation_id": "tab-a",
    }
    if "browser" in restore:
        body["previous_context"] = prior
    response = client.post(transport, json=body)
    assert response.status_code == 200
    if transport.endswith("stream"):
        events = [
            json.loads(line[6:])
            for line in response.text.splitlines()
            if line.startswith("data: ")
        ]
        result = next(e["payload"] for e in events if e.get("type") == "final")
    else:
        result = response.json()
    assert result["status"] == "ok", result
    assert result["tables"][0]["rows"] == [["Avery Finch", 8], ["Blake Reed", 4]]
    for player, evidence in zip(
        result["conversation_context"]["players"], result["appearance_evidence"]
    ):
        expected = {
            r["game_id"]
            for r in bundle[1]["stats"]["rows"]
            if r["player_id"] == player["player_id"]
        }
        assert set(evidence["rows"][0]["game_ids"]) == expected
        assert evidence["rows"][0]["value"] == len(expected)
        assert evidence["scope"]["player_id"] == player["player_id"]
        assert evidence["provenance"]["snapshot_id"] == bundle[1]["stats"]["sha256"]
    assert store.get_turns("tab-b", 6)[-1].context["players"][0]["player_id"] == 999
    assert planner.calls == 0
    main.app.dependency_overrides.clear()


def test_appearance_count_rejects_unconsumed_filters_and_context_mismatch(
    bundle, monkeypatch
):
    planner = Client({})
    settings = _test_settings(
        openai_api_key="test-key",
        research_availability_path=str(bundle[0]),
        agent_rate_limit_per_minute=0,
        agent_rate_limit_daily=0,
    )
    store = InMemoryConversationStore()
    monkeypatch.setattr(main, "_season_conversation_store", lambda season: store)
    client = build_client(settings=settings, agent_client=planner)
    first = client.post(
        "/api/agent/ask",
        json={
            "question": "How did Avery Finch play without Blake Reed in the regular season?",
            "conversation_id": "tab-a",
        },
    ).json()
    for question in [
        "How many games did each player play at home this season?",
        "How many games did each player play in those same games?",
        "How many games did each player play while Blake Reed was out?",
    ]:
        result = client.post(
            "/api/agent/ask", json={"question": question, "conversation_id": "tab-a"}
        ).json()
        assert not result["tables"]
    # A failed follow-up does not erase the last successful pair.
    result = client.post(
        "/api/agent/ask",
        json={
            "question": "How many games did both players play this season?",
            "conversation_id": "tab-a",
        },
    ).json()
    assert result["tables"][0]["rows"] == [["Avery Finch", 8], ["Blake Reed", 4]]
    store = InMemoryConversationStore()
    prior = dict(first["conversation_context"], question="Earlier question")
    prior["players"][0]["player_name"] = "Wrong Identity"
    response = client.post(
        "/api/agent/ask",
        json={
            "question": "How many games did each player play?",
            "conversation_id": "tab-a",
            "previous_context": prior,
        },
    )
    assert response.status_code == 400
    assert planner.calls == 0
    main.app.dependency_overrides.clear()


def test_pre_status_availability_history_recovers_both_players(bundle):
    from app.agent.followup import analysis_context, hydrate_availability_context

    legacy = render_answer(
        compare_availability(
            load_availability(str(bundle[0])),
            request(load_availability(str(bundle[0]))),
        ),
        source_players(load_availability(str(bundle[0]))[1]),
    )
    legacy.pop("status")
    legacy.pop("conversation_context")
    context = hydrate_availability_context(
        analysis_context("Earlier pair", legacy), str(bundle[0])
    )
    assert [p["player_id"] for p in context["players"]] == [811, 822]
    assert context["availability_scope"]["teammate_id"] == 822


def test_games_played_uses_unique_appearances_not_box_score_completeness(bundle):
    from app.agent.semantics import Query, run_query

    evidence = deepcopy(load_availability(str(bundle[0]))[1])
    for row in evidence.rows:
        for component in COMPONENTS:
            row[component] = None
    query = Query(metric="gp", season="2025-26", aggregation="total", player_id=811)
    result = run_query(evidence, query)
    assert result["rows"][0]["value"] == 8
    assert result["rows"][0]["missing_component_games"] == 0
    assert result["metric"]["unit"] == "games"
    with pytest.raises(SemanticError):
        run_query(
            evidence,
            Query(metric="gp", season="2025-26", aggregation="average", player_id=811),
        )
    evidence.rows.append(dict(evidence.rows[0]))
    with pytest.raises(SemanticError):
        run_query(evidence, query)


def with_baseline(bundle):
    """Ten earlier appearances outside the requested comparison dates."""
    loaded = deepcopy(load_availability(str(bundle[0])))
    doc, evidence, games, reports, index = loaded
    for day in range(1, 11):
        for pid in (811, 822):
            row = {
                **evidence.rows[0],
                "game_id": f"prior{day}",
                "game_date": f"2025-10-{day:02}",
                "player_id": pid,
                "player_name": "Avery Finch" if pid == 811 else "Blake Reed",
                "min": 30,
            }
            evidence.rows.append(row)
            index[(row["season"], row["game_id"], pid)] = row
            games[(row["season"], row["game_id"], "ATL")] = {
                **games[("2025-26", "g1", "ATL")],
                "game_id": row["game_id"],
                "game_date": row["game_date"],
                "scheduled_start_utc": row["game_date"] + "T20:00:00Z",
            }
    return loaded


def test_final_minutes_override_out_report_and_reconcile(bundle):
    loaded = load_availability(str(bundle[0]))
    result = compare_availability(loaded, request(loaded))
    g7 = next(g for g in result["groups"]["both_played"] if g["game_id"] == "g7")
    assert g7["report_status"] == "Out"
    assert g7["warnings"] == ["Recorded minutes override the pregame report"]
    assert (
        sum(map(len, result["groups"].values())) + len(result["excluded_games"])
        == result["scope_appearances"]
    )
    ids = [g["game_id"] for group in result["groups"].values() for g in group] + [
        g["game_id"] for g in result["excluded_games"]
    ]
    assert len(ids) == len(set(ids)) == 8
    assert result["policy_version"] == "availability/2"


def test_minutes_filter_uses_prior_history_both_roles_and_strict_boundary(bundle):
    loaded = with_baseline(bundle)
    req = {**request(loaded), "start": "2025-11-01", "end": "2025-11-08"}
    loaded[4][("2025-26", "g2", 811)]["min"] = 14.9
    loaded[4][("2025-26", "g3", 822)]["min"] = 14.9
    loaded[4][("2025-26", "g4", 811)]["min"] = 15
    # A future outlier must never affect these baselines.
    loaded[4][("2025-26", "g8", 811)]["min"] = 200
    result = compare_availability(loaded, req)
    limited = [g for g in result["excluded_games"] if g["reason"] == "limited_minutes"]
    assert {g["game_id"] for g in limited} == {"g2", "g3"}
    assert {g["participation"] for g in limited} == {"both_played", "did_not_play"}
    assert [g["game_id"] for g in result["groups"]["did_not_play"]] == ["g4"]
    for game in limited:
        for check in game["minutes_checks"]:
            assert check["baseline_minutes"] == 30
            assert check["cutoff_minutes"] == 15
            assert "g8" not in check["baseline_game_ids"]
    included = compare_availability(loaded, {**req, "include_limited_minutes": True})
    assert included["scope_appearances"] == result["scope_appearances"] == 8
    assert "limited_minutes" not in included["excluded"]
    assert {g["game_id"] for g in included["groups"]["did_not_play"]} == {"g2", "g4"}
    payload = render_answer(result, source_players(loaded[1]))
    assert payload["tables"][1]["collapsible"]
    assert len(payload["tables"][1]["rows"]) == 8
    assert "Include limited-minute appearances" in payload["followups"]


def test_final_dnp_and_questionable_report_unknown_stays_unknown(bundle):
    loaded = deepcopy(load_availability(str(bundle[0])))
    loaded[0]["participation"] = [
        dict(
            season="2025-26",
            game_id="g5",
            player_id=822,
            team_abbr="ATL",
            minutes=0,
            source_url="https://example.com/final/g5",
        )
    ]
    result = compare_availability(loaded, request(loaded))
    assert "g5" in {g["game_id"] for g in result["groups"]["did_not_play"]}
    # Without explicit participation or a verified Out report, a missing row is unknown.
    loaded[0]["participation"] = []
    result = compare_availability(loaded, request(loaded))
    assert (
        next(g for g in result["excluded_games"] if g["game_id"] == "g5")[
            "participation"
        ]
        == "unknown"
    )
    assert (
        result["groups"]["both_played"][0]["minutes_checks"][0]["status"]
        == "insufficient_baseline"
    )


def test_minutes_options_survive_followups_and_validate_bounds(bundle):
    loaded = load_availability(str(bundle[0]))
    players = source_players(loaded[1])
    prior = request(loaded)
    option = parse_request(
        "Include limited-minute appearances", players, "2025-26", prior
    )
    assert option["include_limited_minutes"]
    option = parse_request("Use a 60% minutes threshold", players, "2025-26", option)
    assert option["minutes_threshold"] == 0.6
    assert parse_request("What about assists?", players, "2025-26", option)[
        "include_limited_minutes"
    ]
    assert not parse_request(
        "Exclude limited-minute appearances", players, "2025-26", option
    )["include_limited_minutes"]
    with pytest.raises(SemanticError):
        parse_request("Use a 0% minutes threshold", players, "2025-26", option)


def test_bad_report_matchup_rejected_before_serving(bundle):
    from app.availability import prepare_availability

    doc = deepcopy(bundle[1])
    doc["reports"][0]["matchup"] = "BOS@NYK"
    with pytest.raises(ValueError, match="does not belong"):
        prepare_availability(doc)


def test_restored_context_keeps_minutes_policy():
    context = main.PriorAvailability(
        player_id=811,
        teammate_id=822,
        season="2025-26",
        phase="Both",
        metrics=["pts"],
        include_limited_minutes=True,
        minutes_threshold=0.6,
    )
    assert context.model_dump()["include_limited_minutes"] is True
    assert context.model_dump()["minutes_threshold"] == 0.6


def test_final_boxscore_capture_preserves_dnp_and_rejects_unknown_minutes():
    from scripts.repair_availability_evidence import participation_rows

    schedule = dict(
        game_id="g1",
        season="2025-26",
        team_abbr="ATL",
        opponent_abbr="BOS",
        scheduled_start_utc="2025-11-01T20:00:00Z",
    )
    payload = {
        "game": dict(
            gameId="g1",
            gameStatus=3,
            gameTimeUTC="2025-11-01T20:00:00Z",
            homeTeam=dict(
                teamTricode="ATL",
                players=[
                    dict(
                        personId=811, played="1", statistics=dict(minutes="PT00M01.00S")
                    ),
                    dict(
                        personId=822, played="0", statistics=dict(minutes="PT00M00.00S")
                    ),
                ],
            ),
            awayTeam=dict(teamTricode="BOS", players=[]),
        )
    }
    records = participation_rows(payload, schedule, "https://example.com/boxscore")
    assert records[0]["minutes"] > 0
    assert records[1]["minutes"] == 0
    payload["game"]["homeTeam"]["players"][1]["statistics"]["minutes"] = None
    with pytest.raises(ValueError, match="Unknown final"):
        participation_rows(payload, schedule, "https://example.com/boxscore")


def test_participation_snapshot_rejects_conflicting_minutes(bundle):
    from app.availability import prepare_availability

    doc = deepcopy(bundle[1])
    doc["participation"] = [
        dict(
            season="2025-26",
            game_id="g1",
            player_id=811,
            team_abbr="ATL",
            minutes=0,
            source_url="https://example.com/g1",
        )
    ]
    with pytest.raises(ValueError, match="disagree"):
        prepare_availability(doc)
