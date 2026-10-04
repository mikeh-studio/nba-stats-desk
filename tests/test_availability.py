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
        "both_played": ["g1", "g3"],
        "reported_out": ["g2", "g4"],
    }
    pts = next(m for m in result["metrics"] if m["metric"] == "pts")
    assert pts["groups"]["both_played"]["value"] == 15
    assert pts["groups"]["reported_out"]["value"] == 35
    assert pts["difference"] == 20
    assert result["excluded"] == {
        "unverified_status_or_membership": 1,
        "not_teammates": 1,
        "conflicting_status": 1,
        "unfinished_game": 1,
    }
    payload = render_answer(result, source_players(loaded[1]))
    assert "Avery Finch" in payload["answer"] and "Blake Reed" in payload["answer"]
    assert payload["player_profile"]["player"]["player_id"] == 811
    for group, sample in result["groups"].items():
        for game in sample:
            assert game["player_id"] == 811 and game["teammate_id"] == 822
            if group == "reported_out":
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
    assert metric["groups"]["reported_out"]["display_value"] == 50
    assert metric["difference"] == 40
    q = request(
        loaded,
        "Show Avery Finch total points without Blake Reed from 2025-11-02 to 2025-11-04 on the road against BOS",
    )
    result = compare_availability(loaded, q)
    assert result["metrics"][0]["groups"]["reported_out"]["value"] == 70
    followup = request(loaded, "What about assists?", q)
    assert followup == {**q, "metrics": ["ast"]}
    assert (
        compare_availability(loaded, followup)["metrics"][0]["groups"]["reported_out"][
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
            follow["availability_evidence"]["metrics"][0]["groups"]["reported_out"][
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
    assert m["groups"]["reported_out"]["valid_games"] == 1
    assert m["groups"]["reported_out"]["missing_component_games"] == 1
    assert m["difference"] is None
    assert (
        "missing components" in render_answer(result, source_players(copy[1]))["answer"]
    )


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
    with pytest.raises(SemanticError):
        compare_availability(loaded, reverse)
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
    with pytest.raises(SemanticError, match="No games meet"):
        compare_availability(loaded, request(loaded))


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
