import json
from datetime import date
from types import SimpleNamespace

import pytest
from app.agent.conversation import InMemoryConversationStore
from app.agent.performance_overview import (
    build_overview,
    overview_scope,
    resolve_overview_player,
    wants_overview,
)
from app.agent.semantic_answer import SemanticAsk
from app.agent.semantic_serving import source_players
from app.agent.semantics import COMPONENTS, Evidence, SemanticError


@pytest.fixture
def evidence():
    rows = []
    for season, year, multiplier in (("2024-25", 2024, 1), ("2025-26", 2025, 2)):
        for player, scale in ((1, 2), (2, 1), (3, 3)):
            for day in range(1, 7):
                rows.append(
                    {
                        **dict.fromkeys(COMPONENTS, 0),
                        "season": season,
                        "season_type": "Regular Season" if day < 6 else "Playoffs",
                        "game_id": f"{year}{day}",
                        "game_date": f"{year}-11-{day:02}",
                        "player_id": player,
                        "player_name": f"Player {player}",
                        "team_abbr": {1: "ATL", 2: "BOS", 3: "CHI"}[player],
                        "opponent_abbr": "NYK",
                        "pts": scale * multiplier * 5,
                        "reb": scale * multiplier,
                        "ast": scale * multiplier,
                        "stl": scale,
                        "blk": scale,
                    }
                )
    covered = frozenset(
        (s, p) for s in ("2024-25", "2025-26") for p in ("Regular Season", "Playoffs")
    )
    return Evidence(
        rows,
        covered,
        "fixture",
        "overview-test",
        {s: "2026-06-01" for s in covered},
        complete=True,
    )


def scope():
    return overview_scope(
        "performance the past 12 months", "2025-26", date(2026, 9, 12)
    )


def test_calendar_scope_includes_baseline_seasons_and_both_phases():
    s = scope()
    assert (s["start"], s["end"]) == (date(2025, 9, 13), date(2026, 9, 12))
    assert (s["previous_start"], s["previous_end"]) == (
        date(2024, 9, 13),
        date(2025, 9, 12),
    )
    assert set(s["seasons"]) == {"2024-25", "2025-26"}
    assert s["phases"] == ["Regular Season", "Playoffs"]


def test_overview_math_table_charts_profile_agree(evidence):
    result = build_overview(
        "Player 1 performance past 12 months",
        evidence,
        source_players(evidence),
        scope(),
    )
    assert result["status"] == "ok"
    assert len(result["tables"][0]["rows"]) == 11
    pts = result["tables"][0]["rows"][0]
    assert pts[:5] == ["PTS", "20.0", "per game", "50.0", "10.0"]
    assert pts[5].startswith("6/6 valid appearances;")
    from app.agent.visualization import VisualizationAgent

    expected_charts = list(result["charts"])
    result = VisualizationAgent().enrich(None, result, "performance", "none", "none")
    assert result["charts"] == expected_charts
    assert result["visualization"]["chart_ids"] == ["pts", "reb", "ast", "stl", "blk"]
    assert len(result["charts"]) == 5
    assert result["charts"][0]["series"][0]["points"][0]["y"] == 20
    assert "20.0 points" in result["answer"]
    assert "up 10.0 against his baseline" in result["answer"]
    assert result["answer"] == "\n\n".join(
        result["semantic_evidence"]["summary_paragraphs"]
    )
    assert result["player_profile"]["player"]["headshot_url"].endswith("/1.png")


def test_missing_values_are_not_zero_or_qualified(evidence):
    next(r for r in evidence.rows if r["player_id"] == 1 and r["season"] == "2025-26")[
        "stl"
    ] = None
    result = build_overview(
        "Player 1 performance", evidence, source_players(evidence), scope()
    )
    row = next(r for r in result["tables"][0]["rows"] if r[0] == "STL")
    assert row[1:5] == ["2.0", "per game", "Unavailable", "Unavailable"]
    assert "5/6 valid appearances" in row[5]
    assert len(result["charts"]) == 4
    assert "2.0 steals" not in result["answer"]


def test_duplicate_names_require_selection_or_chat(evidence):
    players = source_players(evidence)
    for p in players[:2]:
        p["player_name"] = "Same Name"
        p["aliases"] = ["Same Name"]
    first = build_overview("Same Name performance", evidence, players, scope())
    assert first["status"] == "clarification_required"
    assert first["player_profile"] is None
    assert len(first["clarification_options"]) == 2
    assert len({p["label"] for p in first["clarification_options"]}) == 2
    for reply in ("ATL", "1"):
        result = build_overview(
            "Same Name performance\nClarification: " + reply, evidence, players, scope()
        )
        assert result["semantic_evidence"]["player_id"] == 1
    result = build_overview(
        "Same Name performance", evidence, players, scope(), {"player_id": 2}
    )
    assert result["semantic_evidence"]["player_id"] == 2
    assert (
        build_overview(
            "Same Name performance", evidence, players, scope(), {"player_id": 3}
        )["status"]
        == "clarification_required"
    )


def test_two_different_named_players_are_not_silently_collapsed(evidence):
    players = source_players(evidence)
    players[0]["aliases"] = ["A Very Long Name"]
    assert (
        len(
            resolve_overview_player(
                "A Very Long Name and Player 2 performance", players, evidence.rows
            )
        )
        == 2
    )


@pytest.mark.parametrize(
    "question",
    [
        "Player 1 points past 12 months",
        "Player 1 fantasy performance",
        "Compare Player 1 stats against Player 2",
    ],
)
def test_specific_questions_keep_existing_route(question):
    assert not wants_overview(question)


def test_scope_does_not_substitute_last_games_or_unavailable_archives():
    assert (
        overview_scope("performance last 10 games", "2025-26")["recent_appearances"]
        == 10
    )
    with pytest.raises(SemanticError):
        overview_scope("performance past 100 months", "2025-26")


@pytest.mark.parametrize(
    "selection", [None, {"player_id": 1, "player_name": "Same Name"}]
)
def test_conversation_resumes_original_dates_after_clarification(
    evidence, monkeypatch, selection
):
    from app.agent import semantic_answer

    monkeypatch.setattr(
        semantic_answer, "overview_scope", lambda *args, **kwargs: scope()
    )
    for r in evidence.rows:
        if r["player_id"] in (1, 2):
            r["player_name"] = "Same Name"
    calls = []

    class Warehouse:
        def load(self, seasons):
            calls.append(seasons)
            return {"capture": {}}, evidence

    store = InMemoryConversationStore()
    agent = SemanticAsk(
        SimpleNamespace(season="2025-26", agent_conversation_max_turns=6),
        Warehouse(),
        store,
    )
    first = agent.answer(
        "Same Name performance past 12 months",
        client=None,
        model="test",
        conversation_id="test",
    )
    assert first["status"] == "clarification_required"
    second = agent.answer(
        "ATL",
        client=None,
        model="test",
        conversation_id="test",
        selected_player=selection,
    )
    assert second["semantic_evidence"]["player_id"] == 1
    assert second["semantic_evidence"]["scope"]["start"] == "2025-09-13"
    assert store.get_pending_clarification("test") is None
    assert len(calls) == 2


@pytest.mark.parametrize(
    "selection", [None, {"player_id": 1, "player_name": "Same Name"}]
)
def test_specific_metric_duplicate_name_can_resume(evidence, selection):
    for row in evidence.rows:
        if row["player_id"] in (1, 2):
            row["player_name"] = "Same Name"

    class Warehouse:
        def load(self, seasons):
            return {"capture": {}}, evidence

    def create(**kwargs):
        return SimpleNamespace(
            output_text=json.dumps(
                {
                    "status": "query",
                    "message": "",
                    "queries": [
                        {
                            "metric": "pts",
                            "aggregation": "average",
                            "aggregation_source": "planner_selected",
                            "season": "2025-26",
                            "player_name": "resolved_player_1",
                        }
                    ],
                }
            )
        )

    client = SimpleNamespace(responses=SimpleNamespace(create=create))
    agent = SemanticAsk(
        SimpleNamespace(season="2025-26", agent_conversation_max_turns=6),
        Warehouse(),
        InMemoryConversationStore(),
    )
    first = agent.answer(
        "Same Name points", client=client, model="test", conversation_id="metric"
    )
    assert first["status"] == "clarification_required"
    assert len(first["clarification_options"]) == 2
    result = agent.answer(
        "ATL",
        client=client,
        model="test",
        conversation_id="metric",
        selected_player=selection,
    )
    assert result["status"] == "ok"
    assert result["player_profile"]["player"]["player_id"] == 1


@pytest.mark.parametrize(
    "wording",
    [
        "How did Player 1 perform",
        "How did Player 1 play",
        "How did Player 1 do",
        "How has Player 1 been doing",
        "How did Player 1 fare",
        "Player 1 performance overview",
    ],
)
def test_natural_performance_phrasing_uses_full_overview(evidence, wording):
    class Warehouse:
        def load(self, seasons):
            return {"capture": {}}, evidence

    agent = SemanticAsk(
        SimpleNamespace(season="2025-26", agent_conversation_max_turns=6),
        Warehouse(),
    )
    result = agent.answer(
        wording + " during the 2025-26 regular season?", client=None, model="test"
    )
    assert result["status"] == "ok"
    assert result["semantic_plan"]["model_calls"] == 0
    assert len(result["charts"]) == 5
    assert result["semantic_evidence"]["scope"]["phases"] == ["Regular Season"]


@pytest.mark.parametrize(
    "question",
    [
        "How did Curry perform in scoring per 36 minutes?",
        "How did Curry do in fantasy?",
        "How did Curry play against Boston?",
        "How did Curry rank in assists?",
        "How did Curry perform at home?",
        "How did Curry play on the road?",
        "How did Curry perform away?",
        "How did Curry do in minutes?",
        "How did Curry perform in FGM?",
        "How did Curry do in turnovers?",
    ],
)
def test_specific_performance_requests_do_not_become_overviews(question):
    assert not wants_overview(question)


def test_home_performance_keeps_venue_filter_in_governed_plan(evidence):
    from tests.test_semantic_planner import client_for, query_plan

    for row in evidence.rows:
        row["home_away"] = "HOME" if row["game_date"].endswith("01") else "AWAY"
        row["pts"] = 7 if row["home_away"] == "HOME" else 40
    plan = query_plan()
    plan["queries"][0].update(
        season="2025-26", season_type="Regular Season", home_away="HOME"
    )
    agent = SemanticAsk(
        SimpleNamespace(season="2025-26", agent_conversation_max_turns=6),
        SimpleNamespace(load=lambda seasons: ({"capture": {}}, evidence)),
    )
    result = agent.answer(
        "How did Player 1 perform at home during the 2025-26 regular season?",
        client=client_for(plan),
        model="test",
    )
    assert result["status"] == "ok"
    assert result["semantic_evidence"]["rows"][0]["value"] == 7
    assert result["query_plan"]["queries"][0]["home_away"] == "HOME"
    assert "summary_paragraphs" not in result["semantic_evidence"]


def test_full_playoffs_compare_same_season_regular_season(evidence):
    scoped = overview_scope(
        "Player 1 performance in 2025-26 playoffs", "2025-26", date(2026, 10, 5)
    )
    assert scoped["previous_phases"] == ["Regular Season"]
    assert scoped["previous_start"] == scoped["start"]
    assert scoped["seasons"] == ["2025-26"]
    result = build_overview(
        "Player 1 performance", evidence, source_players(evidence), scoped
    )
    metrics = result["semantic_evidence"]["metrics"]
    assert metrics[0]["observed_games"] == 1
    assert metrics[0]["previous"]["observed_games"] == 5
    assert "1 playoff appearance" in result["answer"]
    facts = result["semantic_evidence"]["game_insights"]
    assert facts["observed_games"] == 1
    assert facts["peak_date"] == "2025-11-06"


def test_missing_regular_baseline_coverage_withholds_playoff_changes(evidence):
    from dataclasses import replace

    evidence = replace(
        evidence,
        rows=[
            r
            for r in evidence.rows
            if r["season"] == "2025-26" and r["season_type"] == "Playoffs"
        ],
        covered_scopes=frozenset({("2025-26", "Playoffs")}),
    )
    scoped = overview_scope(
        "Player 1 performance in 2025-26 playoffs", "2025-26", date(2026, 10, 5)
    )
    result = build_overview(
        "Player 1 performance", evidence, source_players(evidence), scoped
    )
    assert all(m["change"] is None for m in result["semantic_evidence"]["metrics"])
    assert "baseline is" not in result["answer"]


def test_calendar_comparison_is_preceding_disjoint_window():
    scoped = overview_scope(
        "Player 1 performance past 14 days regular season", "2025-26", date(2026, 1, 28)
    )
    assert (scoped["start"], scoped["end"]) == (date(2026, 1, 15), date(2026, 1, 28))
    assert (scoped["previous_start"], scoped["previous_end"]) == (
        date(2026, 1, 1),
        date(2026, 1, 14),
    )
    assert scoped["previous_phases"] == ["Regular Season"]


def test_historical_playoff_year_is_not_default_season():
    scoped = overview_scope(
        "Player 1 performance in 2025 playoffs", "2025-26", date(2026, 10, 5)
    )
    assert scoped["start"] == date(2024, 7, 1)
    assert scoped["seasons"] == ["2024-25"]


@pytest.mark.parametrize(
    "question,grain",
    [
        ("How well did Player 1 play in the 2025-26 playoffs?", "monthly"),
        ("How did Player 1 perform in November 2025?", "daily"),
        ("How did Player 1 perform week of 2025-11-01?", "daily"),
        (
            "How did Player 1 perform in November?\nClarification: November 2025",
            "daily",
        ),
    ],
)
def test_three_paragraph_overview_json_and_stream_agree(evidence, question, grain):
    from app.main import app, get_agent_client, get_repository, get_settings
    from app.repository import BigQueryWarehouseRepository
    from fastapi.testclient import TestClient
    from tests.test_semantic_migration import settings

    config = settings("2025-26")
    repo = BigQueryWarehouseRepository(config, client=SimpleNamespace())
    repo._governed_warehouse = SimpleNamespace(
        load=lambda seasons: ({"capture": {}}, evidence)
    )
    old = dict(app.dependency_overrides)
    app.dependency_overrides.update(
        {
            get_settings: lambda: config,
            get_repository: lambda: repo,
            get_agent_client: lambda: object(),
        }
    )
    try:
        with TestClient(app) as client:
            body = {"question": question}
            direct = client.post("/api/agent/ask", json=body)
            assert direct.status_code == 200
            payload = direct.json()
            assert payload["status"] == "ok"
            assert payload["charts"][0]["grain"] == grain
            assert len(payload["charts"]) == 5
            assert 1 <= len(payload["semantic_evidence"]["summary_paragraphs"]) <= 4
            if payload["semantic_evidence"]["insights"][0].get("game_variation"):
                assert "Across his games" in payload["answer"]
            assert payload["semantic_evidence"]["summary_paragraphs"][-1].startswith(
                "Scope:"
            )
            assert payload["answer"] == "\n\n".join(
                payload["semantic_evidence"]["summary_paragraphs"]
            )
            streamed = client.post("/api/agent/ask/stream", json=body)
            events = [
                json.loads(line[6:])
                for line in streamed.text.splitlines()
                if line.startswith("data: ")
            ]
            final = next(e for e in events if e.get("type") == "final")
            result = final.get("payload", final.get("response", final))
            assert result["semantic_evidence"] == payload["semantic_evidence"]
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(old)


@pytest.mark.parametrize("phrase", ["play-in", "playin", "play in tournament"])
def test_play_in_tournament_remains_unsupported(phrase):
    with pytest.raises(SemanticError, match="box scores only"):
        overview_scope(
            f"Player 1 performance in the {phrase}", "2025-26", date(2026, 10, 5)
        )


@pytest.mark.parametrize(
    "profile,phrase,count",
    [
        ("general", "performance", 11),
        ("scoring", "scoring performance", 9),
        ("playmaking", "playmaking performance", 6),
        ("rebounding", "rebounding performance", 6),
        ("defense", "defensive activity performance", 6),
        ("contribution", "contribution beyond scoring performance", 6),
    ],
)
def test_profile_selection_is_explicit_and_does_not_depend_on_percentiles(
    evidence, profile, phrase, count
):
    from app.agent.overview_policy import PROFILES

    q = f"Player 1 {phrase} in 2025-26"
    assert wants_overview(q)
    result = build_overview(q, evidence, source_players(evidence), scope())
    policy = result["semantic_evidence"]["presentation"]
    assert policy["profile"] == profile
    assert policy["primary_keys"] == list(PROFILES[profile])
    assert len(result["tables"][0]["rows"]) == count
    assert len(result["tables"][0]["rows"]) + len(result["tables"][1]["rows"]) == len(
        result["semantic_evidence"]["metrics"]
    )
    assert "stood out at" not in result["answer"]
    if profile == "scoring":
        assert "fg3m" in policy["primary_keys"]
    if profile == "defense":
        assert policy["takeaways"][0]["area"] == "Defensive activity"
        assert policy["limitation"] == ""
    if profile == "rebounding":
        assert policy["takeaways"][0]["area"] == "Rebounding"
        assert policy["limitation"] == ""


def test_limited_sample_retains_values_but_not_comparisons_or_standout_claims(evidence):
    scoped = scope()
    scoped["start"] = scoped["end"] = date(2025, 11, 1)
    result = build_overview(
        "Player 1 performance", evidence, source_players(evidence), scoped
    )
    stats = result["semantic_evidence"]["metrics"]
    pts = next(m for m in stats if m["key"] == "pts")
    assert pts["value"] == 20 and pts["sample"]["status"] == "limited"
    assert pts["display_change"] is None and pts["percentile"] is None
    assert all(m["display_change"] is None for m in stats)
    assert all(
        "exceeded" not in note["text"].lower()
        for note in result["semantic_evidence"]["presentation"]["takeaways"]
    )


def test_shooting_samples_pool_attempts_and_zero_attempts_are_unavailable(evidence):
    own = [r for r in evidence.rows if r["player_id"] == 1 and r["season"] == "2025-26"]
    for row in own:
        row.update(fg3m=0, fg3a=0)
    own[0].update(fg3m=1, fg3a=1)
    own[1].update(fg3m=0, fg3a=9)
    result = build_overview(
        "Player 1 scoring performance", evidence, source_players(evidence), scope()
    )
    three = next(
        m for m in result["semantic_evidence"]["metrics"] if m["key"] == "fg3_pct"
    )
    assert three["value"] == 10
    assert "1 makes / 10 attempts" in three["sample"]["text"]
    assert "at least 50 3PA" in three["sample"]["text"]
    assert three.get("percentile") is None
    for row in own:
        row.update(fg3m=0, fg3a=0)
    result = build_overview(
        "Player 1 performance", evidence, source_players(evidence), scope()
    )
    three = next(
        m for m in result["semantic_evidence"]["metrics"] if m["key"] == "fg3_pct"
    )
    assert three["value"] is None and three["sample"]["status"] == "unavailable"
    assert "0 makes / 0 attempts" in three["sample"]["text"]


def test_partial_sample_changes_are_withheld_and_takeaways_disclose_limit(evidence):
    own = [r for r in evidence.rows if r["player_id"] == 1 and r["season"] == "2025-26"]
    own[0]["ast"] = None
    result = build_overview(
        "Player 1 playmaking performance", evidence, source_players(evidence), scope()
    )
    ast = next(m for m in result["semantic_evidence"]["metrics"] if m["key"] == "ast")
    assert ast["valid_games"] == 5 and ast["observed_games"] == 6
    assert ast["sample"]["status"] == "limited" and ast["display_change"] is None
    assert "1 missing components" in ast["sample"]["text"]
    notes = result["semantic_evidence"]["presentation"]["takeaways"]
    assert any("Assists are incomplete" in n["text"] for n in notes)
    assert result["semantic_evidence"]["request_fulfillment"]["status"] == "partial"
    assert "assists per game" not in result["answer"]


@pytest.mark.parametrize(
    "condition", ["home", "against Boston", "per 36 minutes", "in the fourth quarter"]
)
def test_focus_does_not_erase_unsupported_or_specific_conditions(condition):
    q = f"Player 1 scoring performance {condition}"
    if condition == "in the fourth quarter":
        with pytest.raises(SemanticError):
            overview_scope(q, "2025-26")
    else:
        assert not wants_overview(q)


@pytest.mark.parametrize(
    "requested",
    [
        "offensive rebounds",
        "box-outs",
        "screen assists",
        "potential assists",
        "rim protection",
        "defensive rating",
        "fouls",
        "rebound chances",
    ],
)
def test_focused_profiles_cannot_substitute_box_scores_for_unconnected_measures(
    requested,
):
    from app.agent.question_intent import question_intent

    assert (
        question_intent(f"How did Curry perform in {requested}?")["status"]
        == "unsupported_scope"
    )


@pytest.mark.parametrize(
    "phrase,lo,hi,grain",
    [
        ("in November 2025", "2025-11-01", "2025-11-30", "daily"),
        ("in Feb. 2024", "2024-02-01", "2024-02-29", "daily"),
        ("week of 2025-12-29", "2025-12-29", "2026-01-04", "daily"),
        ("week starting 2025-11-01", "2025-11-01", "2025-11-07", "daily"),
        ("past 1 month as of 2025-11-30", "2025-10-31", "2025-11-30", "daily"),
        ("past week as of 2025-11-30", "2025-11-24", "2025-11-30", "daily"),
        ("last week as of 2025-11-30", "2025-11-17", "2025-11-23", "daily"),
        ("last month as of 2025-11-30", "2025-10-01", "2025-10-31", "daily"),
        ("this month as of 2025-11-06", "2025-11-01", "2025-11-06", "daily"),
        ("from 2025-11-01 through 2025-12-02", "2025-11-01", "2025-12-02", "monthly"),
        ("past 3 months as of 2025-11-30", "2025-08-31", "2025-11-30", "monthly"),
    ],
)
def test_overview_calendar_grain(phrase, lo, hi, grain):
    scoped = overview_scope(
        "Player 1 performance " + phrase, "2025-26", date(2026, 10, 12)
    )
    assert str(scoped["start"]) == lo
    assert str(scoped["end"]) == hi
    assert scoped["chart_grain"] == grain


def test_named_month_compares_full_previous_calendar_month():
    scoped = overview_scope(
        "performance in February 2024", "2025-26", date(2026, 10, 12)
    )
    assert scoped["previous_start"] == date(2024, 1, 1)
    assert scoped["previous_end"] == date(2024, 1, 31)


@pytest.mark.parametrize(
    "phrase",
    [
        "first week of November 2025",
        "week of 2025-02-30",
        "in November 2025 and December 2025",
        "since November 2025",
        "in a specific week",
        "week of 2025-11-01 and week of 2025-11-08",
        "in November 2027",
        "in November 2025 from 2025-11-01 through 2025-11-06",
        "in November 2025 as of 2025-11-15",
        "in November 2025 during 2024-25",
        "this week and last week",
        "past 1 month and past 1 week",
        "during specific weeks",
    ],
)
def test_ambiguous_calendar_does_not_broaden_to_season(phrase):
    with pytest.raises(SemanticError) as exc:
        overview_scope("Player 1 performance " + phrase, "2025-26", date(2026, 10, 12))
    assert exc.value.code == "clarification_required"


def test_daily_series_uses_exact_game_dates_without_filling_off_days(evidence):
    scoped = overview_scope(
        "Player 1 performance week of 2025-11-01", "2025-26", date(2026, 10, 12)
    )
    result = build_overview(
        "Player 1 performance week of 2025-11-01",
        evidence,
        source_players(evidence),
        scoped,
    )
    chart = result["charts"][0]
    assert chart["grain"] == "daily"
    from app.agent.visualization import VisualizationAgent

    expected_charts = list(result["charts"])
    result = VisualizationAgent().enrich(None, result, "performance", "none", "none")
    assert result["charts"] == expected_charts
    assert result["visualization"]["chart_ids"] == ["pts", "reb", "ast", "stl", "blk"]
    points = chart["series"][0]["points"]
    assert [p["x"] for p in points] == [f"2025-11-{day:02}" for day in range(1, 7)]
    assert [p["y"] for p in points] == [20] * 6
    metric = result["semantic_evidence"]["metrics"][0]
    assert metric["trend"] == {"grain": "daily", "points": points}
    assert "monthly" not in metric
    assert result["tables"][0]["rows"][0][1] == "20.0"
    assert "game-by-game" in " ".join(result["assumptions"])


def test_monthly_series_retains_saved_evidence_contract(evidence):
    result = build_overview(
        "Player 1 performance", evidence, source_players(evidence), scope()
    )
    chart = result["charts"][0]
    metric = result["semantic_evidence"]["metrics"][0]
    assert chart["grain"] == "monthly"
    assert (
        metric["monthly"] == metric["trend"]["points"] == chart["series"][0]["points"]
    )


def test_named_calendar_month_keeps_playoff_phase_and_year():
    scoped = overview_scope(
        "Player 1 performance in May 2025 playoffs", "2025-26", date(2026, 10, 12)
    )
    assert scoped["start"] == date(2025, 5, 1)
    assert scoped["end"] == date(2025, 5, 31)
    assert scoped["phases"] == ["Playoffs"]


@pytest.mark.parametrize(
    "phrase,lo,hi",
    [
        ("past month as of 2025-03-15", "2025-01-16", "2025-02-15"),
        ("this month as of 2025-03-15", "2025-02-01", "2025-02-28"),
    ],
)
def test_singular_month_comparison_windows(phrase, lo, hi):
    scoped = overview_scope("performance " + phrase, "2025-26", date(2026, 10, 12))
    assert str(scoped["previous_start"]) == lo
    assert str(scoped["previous_end"]) == hi


@pytest.mark.parametrize(
    "reply", ["2025", "November 2025", "from 2025-11-01 through 2025-11-30"]
)
def test_calendar_clarification_resolves_without_repeating_ambiguity(reply):
    scoped = overview_scope(
        "Player 1 performance in November\nClarification: " + reply,
        "2025-26",
        date(2026, 10, 12),
    )
    assert scoped["start"] == date(2025, 11, 1)
    assert scoped["end"] == date(2025, 11, 30)
    assert scoped["chart_grain"] == "daily"


@pytest.mark.parametrize("inverse", [False, True])
def test_takeaways_pair_assists_turnovers_and_keep_minutes_context(evidence, inverse):
    for row in evidence.rows:
        if row["player_id"] == 1:
            current = row["season"] == "2025-26"
            row.update(
                ast=2 if current == inverse else 5,
                tov=4 if current == inverse else 1,
                min=32 if current else 30,
            )
    result = build_overview(
        "Player 1 performance", evidence, source_players(evidence), scope()
    )
    policy = result["semantic_evidence"]["presentation"]
    assert policy["version"] == "ask_metric_presentation/5"
    assert len(policy["takeaways"]) == 3
    change = next(n for n in policy["takeaways"] if n["area"] == "Creation")
    assert change["title"] == (
        "Fewer assists, more turnovers" if inverse else "More assists, fewer turnovers"
    )
    assert ("Turnovers rose 3.0" if inverse else "Turnovers fell 3.0") in result[
        "answer"
    ]
    assert "per 36 minutes" in result["answer"]
    assert (
        next(m for m in result["semantic_evidence"]["metrics"] if m["key"] == "min")[
            "display_change"
        ]
        == 2
    )
    assert all("overall defensive impact" not in n["text"] for n in policy["takeaways"])


def test_takeaways_do_not_reconstruct_unqualified_baseline_changes(evidence):
    for row in evidence.rows:
        if row["player_id"] == 1 and row["season"] == "2024-25":
            row["ast"] = None
    result = build_overview(
        "Player 1 playmaking performance", evidence, source_players(evidence), scope()
    )
    notes = result["semantic_evidence"]["presentation"]["takeaways"]
    assert all("AST +" not in n["text"] and "AST -" not in n["text"] for n in notes)
    assert all("More assists" not in n["title"] for n in notes)


def test_takeaways_use_valid_game_range_when_comparison_is_thin(evidence):
    evidence.rows[:] = [
        r
        for r in evidence.rows
        if r["season"] == "2025-26" or int(r["game_date"][-2:]) < 3
    ]
    own = [r for r in evidence.rows if r["player_id"] == 1 and r["season"] == "2025-26"]
    own[0]["pts"] = 40
    result = build_overview(
        "Player 1 scoring performance", evidence, source_players(evidence), scope()
    )
    notes = result["semantic_evidence"]["presentation"]["takeaways"]
    assert not any(n["role"] == "change" for n in notes)
    assert all("against his baseline" not in n["text"] for n in notes)
    assert result["semantic_evidence"]["game_insights"]["max_points"] == 40
    assert result["semantic_evidence"]["insights"][0]["id"] == "scoring_context"


def test_thin_focus_uses_one_warning_and_no_filler_takeaways(evidence):
    scoped = scope()
    scoped["start"] = scoped["end"] = date(2025, 11, 1)
    result = build_overview(
        "Player 1 performance", evidence, source_players(evidence), scoped
    )
    notes = result["semantic_evidence"]["presentation"]["takeaways"]
    assert len(notes) <= 3
    assert all("exceeded" not in n["text"] for n in notes)
    assert "small sample" in result["answer"]


@pytest.mark.parametrize(
    "question",
    [
        "How did Klay Thompson played last year?",
        "How did Klay Thompson play last season?",
        "Tell me about Klay Thompson's performance last year",
        "How did Klay Thompson perform in November?",
        "How has Klay Thompson played lately?",
        "How has Klay Thompson performed recently?",
        "How did Klay Thompson perform in 2025?",
        "How did Klay Thompson play over the past year?",
    ],
)
def test_conversational_defaults_answer_json_and_sse(evidence, question):
    from app.main import app, get_agent_client, get_repository, get_settings
    from app.repository import BigQueryWarehouseRepository
    from fastapi.testclient import TestClient
    from tests.test_semantic_migration import settings

    for row in evidence.rows:
        if row["player_id"] == 1:
            row["player_name"] = "Klay Thompson"
    config = settings("2025-26")
    repo = BigQueryWarehouseRepository(config, client=SimpleNamespace())
    repo._governed_warehouse = SimpleNamespace(
        load=lambda seasons: ({"capture": {}}, evidence)
    )
    old = dict(app.dependency_overrides)
    app.dependency_overrides.update(
        {
            get_settings: lambda: config,
            get_repository: lambda: repo,
            get_agent_client: lambda: object(),
        }
    )
    try:
        with TestClient(app) as client:
            for suffix in ("", "/stream"):
                response = client.post(
                    "/api/agent/ask" + suffix, json={"question": question}
                )
                assert response.status_code == 200
                payload = (
                    response.json()
                    if not suffix
                    else next(
                        json.loads(line[6:])["payload"]
                        for line in response.text.splitlines()
                        if line.startswith("data: ")
                        and json.loads(line[6:]).get("type") == "final"
                    )
                )
                assert payload["status"] == "ok", payload
                assert payload["player_profile"]["player"]["player_id"] == 1
                assert payload["scope_assumptions"]
                assert not payload.get("clarification_options")
                pts = payload["semantic_evidence"]["metrics"][0]
                assert pts["value"] in (10, 20, 15)
                if "last year" in question or "last season" in question:
                    assert (
                        payload["semantic_evidence"]["scope"]["start"] == "2025-07-01"
                    )
                    assert pts["value"] == 20
                    assert len(payload["period_options"]) == 2
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(old)


@pytest.mark.parametrize(
    "failure", ["unsupported_coverage", "access_denied", "invalid_evidence"]
)
def test_missing_baseline_does_not_replace_requested_scope(evidence, failure):
    from dataclasses import replace

    current = replace(
        evidence,
        rows=[r for r in evidence.rows if r["season"] == "2025-26"],
        covered_scopes=frozenset(
            s for s in evidence.covered_scopes if s[0] == "2025-26"
        ),
    )
    calls = []

    class Warehouse:
        def load(self, seasons):
            calls.append(seasons)
            if seasons != ["2025-26"]:
                raise SemanticError(failure, "Unavailable baseline")
            return {}, current

    agent = SemanticAsk(
        SimpleNamespace(season="2025-26", agent_conversation_max_turns=6), Warehouse()
    )
    payload = agent.answer(
        "How did Player 1 perform last year?", client=object(), model="fixture"
    )
    if failure == "unsupported_coverage":
        assert payload["status"] == "ok"
        assert payload["semantic_evidence"]["scope"]["start"] == "2025-07-01"
        assert payload["semantic_evidence"]["metrics"][0]["value"] == 20
        assert payload["semantic_evidence"]["metrics"][0]["display_change"] is None
        assert calls[-1] == ["2025-26"]
    else:
        assert payload["status"] == failure
        assert len(calls) == 1


def test_recent_playoffs_compare_previous_playoff_appearances(evidence):
    rows = [
        {
            **evidence.rows[0],
            "season": "2025-26",
            "season_type": "Playoffs",
            "game_id": f"recent-{day}",
            "game_date": f"2026-05-{day:02}",
            "pts": day,
        }
        for day in range(1, 21)
    ]
    recent_evidence = Evidence(
        rows,
        evidence.covered_scopes,
        "fixture",
        "recent-playoffs",
        evidence.data_through,
        complete=True,
    )
    question = "Player 1 performance last 10 games in 2025-26 playoffs"
    period = overview_scope(question, "2025-26", date(2026, 10, 12))
    payload = build_overview(
        question, recent_evidence, source_players(recent_evidence), period
    )
    result = payload["semantic_evidence"]
    assert result["scope"]["previous_phases"] == ["Playoffs"]
    points = next(m for m in result["metrics"] if m["key"] == "pts")
    assert points["value"] == 15.5
    assert points["previous"]["value"] == 5.5
    assert points["previous"]["valid_games"] == 10
    assert points["display_change"] == 10


@pytest.mark.parametrize(
    "profile,word,abbreviation",
    [("scoring", "points", "pts"), ("playmaking", "turnovers", "tov")],
)
def test_focused_prose_uses_statistic_names(evidence, profile, word, abbreviation):
    payload = build_overview(
        f"Player 1 {profile} performance", evidence, source_players(evidence), scope()
    )
    first = payload["semantic_evidence"]["summary_paragraphs"][0]
    assert word in first
    assert abbreviation not in first


ABSENCE_QUESTIONS = [
    "Player 1 stats when Player 2 is out",
    "Player 1 stats in games his teammate sat out",
    "Player 1 stats with Player 2 out",
    "Player 1 stats when Player 2 sat",
    "Player 1 stats in games Player 2 missed",
    "Player 1 stats when Player 2 was inactive",
    "Player 1 stats in Player 2 DNP games",
    "Player 1 stats when Player 2 didn't play",
    "Player 1 stats without his teammate",
]


@pytest.mark.parametrize("question", ABSENCE_QUESTIONS)
def test_overview_scope_rejects_teammate_absence(question):
    for text in (question, question + "\nClarification: Player 1"):
        with pytest.raises(SemanticError) as exc:
            overview_scope(text + " in 2025-26", "2025-26", date(2026, 9, 12))
        assert exc.value.code == "clarification_required"
        assert "teammate availability" in str(exc.value)


@pytest.mark.parametrize("question", ABSENCE_QUESTIONS[:2])
def test_comparison_scope_rejects_teammate_absence(question):
    from app.agent.player_comparison import comparison_scope

    with pytest.raises(SemanticError, match="teammate availability"):
        comparison_scope(
            question.replace("Player 1", "Player 1 vs Player 3"),
            "2025-26",
            date(2026, 9, 12),
        )


@pytest.mark.parametrize(
    "question",
    [
        "Player 1 stats in games he outscored his matchup",
        "Player 1 stats out of all 2025-26 games",
        "Find out Player 1 stats",
        "Player 1 stats on Saturday nights",
    ],
)
def test_absence_guard_ignores_unrelated_out_wording(question):
    s = overview_scope(question + " in 2025-26", "2025-26", date(2026, 9, 12))
    assert s["phases"] == ["Regular Season", "Playoffs"]


@pytest.mark.parametrize(
    "question",
    [
        "Compare teammates Player 1 and Player 2 during the 2025-26 regular season",
        "Player 1 vs Player 2, former teammates, during the 2025-26 regular season",
    ],
)
def test_teammate_relationship_alone_keeps_scorecard(evidence, question):
    # "Teammates" names a relationship, not an availability condition.
    class Warehouse:
        def load(self, seasons):
            return {"capture": {}}, evidence

    agent = SemanticAsk(
        SimpleNamespace(season="2025-26", agent_conversation_max_turns=6),
        Warehouse(),
    )
    result = agent.answer(question, client=None, model="test")
    assert result["status"] == "ok"
    assert result["semantic_plan"]["model_calls"] == 0


@pytest.mark.parametrize("question", ABSENCE_QUESTIONS[:2])
@pytest.mark.parametrize(
    "selection", [None, {"player_id": 1, "player_name": "Player 1"}]
)
def test_absence_overview_never_drops_filter_or_asks_for_player(
    evidence, monkeypatch, question, selection
):
    # Bypass the earlier availability refusal to exercise the overview guard itself,
    # including the selected-player resume path.
    from app.agent import availability_ask

    monkeypatch.setattr(availability_ask, "wants_availability", lambda q: False)

    class Warehouse:
        def load(self, seasons):
            return {"capture": {}}, evidence

    agent = SemanticAsk(
        SimpleNamespace(season="2025-26", agent_conversation_max_turns=6),
        Warehouse(),
    )
    result = agent.answer(
        question + " during the 2025-26 regular season",
        client=None,
        model="test",
        selected_player=selection,
    )
    assert result["status"] == "clarification_required"
    assert "teammate availability" in result["answer"]
    assert "Which player" not in result["answer"]
    assert not result.get("charts")
