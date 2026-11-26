import json
from dataclasses import replace
from datetime import date, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from app.agent.clock import semantic_date
from app.agent.publication_ask_source import PublicationAskSource
from app.agent.semantics import COMPONENTS, Evidence
from app.main import app, get_repository, get_settings
from app.repository import BigQueryWarehouseRepository
from app.seasons import DEFAULT_SEASON
from fastapi.testclient import TestClient
from tests.test_semantic_migration import Warehouse, settings

FIRST = f"{DEFAULT_SEASON[:4]}-10-20"
LAST = f"{DEFAULT_SEASON[:4]}-11-17"
EARLY = f"{DEFAULT_SEASON[:4]}-11-01"
LATE = f"{DEFAULT_SEASON[:4]}-11-22"
ACTIVE = "pub_aaaaaaaaaaaaaaaa"
PRIOR = "pub_bbbbbbbbbbbbbbbb"
OPENER = "How is Taylor Example playing this season?"


def source(publication_id, first=FIRST, last=LAST):
    phase = "Regular Season"
    scope = DEFAULT_SEASON, phase
    rows = [
        {
            **dict.fromkeys(COMPONENTS, 0),
            "season": DEFAULT_SEASON,
            "season_type": phase,
            "game_id": f"{i:010}",
            "game_date": game_date,
            "player_id": 1000,
            "player_name": "Taylor Example",
            "team_abbr": "DAL",
            "opponent_abbr": "BOS",
            "pts": points,
            "ast": assists,
            "fgm": twos + threes,
            "fga": twos + threes,
            "fg3m": threes,
            "fg3a": threes,
            "ftm": free_throws,
            "fta": free_throws,
        }
        for i, (game_date, points, assists, twos, threes, free_throws) in enumerate(
            [(EARLY, 15, 2, 4, 2, 1), (LAST, 30, 6, 6, 5, 3)], start=1
        )
        if first <= game_date <= last
    ]
    evidence = Evidence(
        rows,
        frozenset({scope}),
        "validated weekly publication",
        publication_id,
        {scope: last},
        complete=True,
        cohort_complete=False,
    )
    return PublicationAskSource(
        evidence,
        {
            "scope": {
                "season": DEFAULT_SEASON,
                "phase": phase,
                "first": first,
                "last": last,
            },
            "publication_id": publication_id,
            "freshness": "fresh",
        },
    )


@pytest.fixture
def production(monkeypatch):
    captured = {ACTIVE: source(ACTIVE), PRIOR: source(PRIOR, last=EARLY)}
    reads = []
    legacy = Warehouse()
    cfg = replace(settings(DEFAULT_SEASON), openai_api_key=None)
    repo = BigQueryWarehouseRepository(cfg, client=SimpleNamespace())
    repo._governed_warehouse = legacy

    def read(publication_id):
        reads.append(publication_id)
        return captured[publication_id or ACTIVE]

    repo.ask_publication_source = read
    monkeypatch.setattr("app.main._prewarm_performance_flow", lambda repo: None)
    monkeypatch.setitem(app.dependency_overrides, get_settings, lambda: cfg)
    monkeypatch.setitem(app.dependency_overrides, get_repository, lambda: repo)
    token = semantic_date.set(date.fromisoformat(LATE))
    try:
        with TestClient(app) as client:
            yield client, captured, reads, legacy, repo
    finally:
        semantic_date.reset(token)


def add_riley(captured, publication_id, *, points):
    publication = captured[publication_id]
    earlier = publication.evidence.rows[0]
    riley = dict(
        earlier,
        player_id=2000,
        player_name="Riley Example",
        pts=points,
    )
    captured[publication_id] = replace(
        publication,
        evidence=replace(
            publication.evidence, rows=[*publication.evidence.rows, riley]
        ),
    )


def ask(client, question, *, endpoint="ask", season=DEFAULT_SEASON, **params):
    response = client.post(
        f"/api/agent/{endpoint}?season={season}", json={"question": question, **params}
    )
    assert response.status_code == 200, response.text
    if endpoint == "ask":
        return response.json()
    events = [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    assert events[-1]["type"] == "final", events[-1]
    return events[-1]["payload"]


@pytest.fixture
def game_publication(production):
    _, captured, _, _, _ = production
    release = captured[ACTIVE]
    template = release.evidence.rows[0]
    year = DEFAULT_SEASON[:4]

    def appearance(day, game, pid, name, team, opponent, pts, oreb, dreb, ast):
        makes = pts // 2
        return dict(
            template,
            game_id=f"{game:010}",
            game_date=f"{year}-11-{day:02}",
            player_id=pid,
            player_name=name,
            team_abbr=team,
            opponent_abbr=opponent,
            pts=pts,
            reb=oreb + dreb,
            oreb=oreb,
            dreb=dreb,
            ast=ast,
            fgm=makes,
            fga=makes + 2,
            fg3m=0,
            fg3a=0,
            ftm=pts % 2,
            fta=pts % 2,
        )

    player = "Victor Wembanyama"
    pid = 1641705
    day_rows = [
        appearance(12, 120, pid, player, "SAS", "DET", 30, 5, 13, 4),
        appearance(12, 120, 100, "SAS Teammate", "SAS", "DET", 50, 2, 8, 6),
        appearance(12, 120, 200, "DET Scorer", "DET", "SAS", 40, 3, 9, 8),
        appearance(12, 120, 201, "DET Teammate", "DET", "SAS", 35, 2, 7, 6),
    ]
    other_rows = [
        appearance(2, 20, pid, player, "SAS", "DEN", 29, 2, 6, 2),
        appearance(4, 40, pid, player, "SAS", "DEN", 30, 2, 9, 4),
        appearance(9, 90, pid, player, "SAS", "BOS", 31, 3, 9, 4),
        appearance(14, 140, pid, player, "SAS", "DET", 99, 30, 40, 0),
    ]
    game = dict(
        game_id=day_rows[0]["game_id"],
        game_date=day_rows[0]["game_date"],
        season=DEFAULT_SEASON,
        season_type="Regular Season",
        home_team_abbr="SAS",
        away_team_abbr="DET",
        home_score=80,
        away_score=75,
    )
    participants = [
        dict(
            game_id=game["game_id"],
            player_id=row["player_id"],
            team_abbr=row["team_abbr"],
            played=True,
        )
        for row in day_rows
    ]
    participants.append(
        dict(game_id=game["game_id"], player_id=900, team_abbr="SAS", played=False)
    )
    captured[ACTIVE] = replace(
        release,
        evidence=replace(
            release.evidence,
            rows=day_rows + other_rows,
            game_results=[game],
            game_participants=participants,
        ),
    )
    return production, game


@pytest.mark.parametrize(
    "question",
    [
        "How did Wembanyama play yesterday?",
        "How did Wembanyama played yesterday?",
        "How did Wembanyama do last night?",
        "How did Wembanyama play last night?",
        f"How did Wembanyama play against Detroit on November 12, {DEFAULT_SEASON[:4]}?",
        f"How did Wembanyama perform on {DEFAULT_SEASON[:4]}-11-12 vs DET?",
    ],
)
def test_single_game_analysis_uses_verified_rosters_and_pregame_baseline(
    game_publication, question
):
    (client, _, _, legacy, _), game = game_publication
    token = semantic_date.set(date.fromisoformat(f"{DEFAULT_SEASON[:4]}-11-13"))
    try:
        payload = ask(client, question)
    finally:
        semantic_date.reset(token)
    assert payload["status"] == "ok", payload["answer"]
    assert payload["publication_id"] == ACTIVE and legacy.calls == []
    report = payload["semantic_evidence"]
    assert report["kind"] == "game_performance" and report["lead_metric"] == "reb"
    assert report["player"]["player_id"] == 1641705
    assert report["game"]["game_id"] == game["game_id"]
    assert (report["game"]["home_score"], report["game"]["away_score"]) == (80, 75)
    assert report["baseline"]["games"] == 3
    assert report["baseline"]["last"].endswith("-11-09")
    rebounds = next(row for row in report["metrics"] if row["key"] == "reb")
    assert rebounds["baseline"] == pytest.approx(31 / 3)
    assert rebounds["change"] == pytest.approx(18 - 31 / 3)
    assert report["team_rebounding"]["team"]["rate"] == pytest.approx(7 / 23)
    assert report["team_rebounding"]["opponent"]["rate"] == pytest.approx(5 / 26)
    assert {row["player_id"] for row in report["rosters"]["team"]} == {1641705, 100}
    assert {row["player_id"] for row in report["rosters"]["opponent"]} == {200, 201}
    assert "SAS beat DET 80–75" in payload["answer"]
    assert "64.3%" in payload["answer"]
    assert payload["charts"] == []


@pytest.mark.parametrize("affected_id", [1641705, 100, 201])
def test_match_rosters_do_not_authorize_unreconciled_team_rebounds(
    game_publication, affected_id
):
    (client, captured, _, _, _), game = game_publication
    release = captured[ACTIVE]
    rows = [
        dict(row, dreb=row["dreb"] - 1)
        if row["game_id"] == game["game_id"] and row["player_id"] == affected_id
        else row
        for row in release.evidence.rows
    ]
    captured[ACTIVE] = replace(release, evidence=replace(release.evidence, rows=rows))
    payload = ask(
        client,
        f"What stood out about Victor Wembanyama's rebounding against Detroit on {game['game_date']}?",
    )
    assert payload["status"] == "ok", payload["answer"]
    report = payload["semantic_evidence"]
    assert report["rosters"]["status"] == "ok"
    assert "SAS beat DET 80–75" in payload["answer"]
    assert report["team_rebounding"]["status"] == "unavailable"
    assert "offensive rebound rate was" not in payload["answer"]
    contribution = {row["key"] for row in report["contribution"]}
    assert {"pts", "reb", "ast"} <= contribution
    assert ({"oreb", "dreb"} <= contribution) == (affected_id == 201)
    metrics = {row["key"]: row["value"] for row in report["metrics"]}
    assert metrics["reb"] == 18
    assert (metrics["dreb"] is None) == (affected_id == 1641705)


def test_pregame_component_average_requires_each_split_to_reconcile(game_publication):
    (client, captured, _, _, _), game = game_publication
    release = captured[ACTIVE]
    rows = [
        dict(row, dreb=row["dreb"] - 1)
        if row["player_id"] == 1641705 and row["game_date"].endswith("-11-02")
        else row
        for row in release.evidence.rows
    ]
    captured[ACTIVE] = replace(release, evidence=replace(release.evidence, rows=rows))
    result = ask(client, f"How did Wembanyama play on {game['game_date']}?")
    report = result["semantic_evidence"]
    metrics = {row["key"]: row for row in report["metrics"]}
    assert metrics["reb"]["baseline"] == pytest.approx(31 / 3)
    assert report["rebound_baseline_verified"] is False
    for key in ("oreb", "dreb"):
        assert metrics[key]["value"] is not None
        assert metrics[key]["baseline"] is None and metrics[key]["change"] is None
        assert metrics[key]["baseline_games"] == 2


def test_single_game_followup_expands_opponents_but_keeps_original_cutoff(
    game_publication,
):
    (client, _, _, legacy, _), game = game_publication
    opening = ask(
        client, f"How did Wembanyama play against Detroit on {game['game_date']}?"
    )
    followup = ask(
        client,
        "Show his rebounds game by game",
        endpoint="ask/stream",
        conversation_id=opening["conversation_id"],
    )
    assert followup["status"] == "ok", followup["answer"]
    assert followup["publication_id"] == ACTIVE and legacy.calls == []
    section = followup["semantic_evidence"]
    assert section["scope"]["as_of"] == game["game_date"]
    assert not section["scope"]["opponent_abbr"]
    assert [row["value"] for row in section["rows"]] == [8, 11, 12, 18]


def test_game_scoring_spike_leads_over_similar_relative_decline(game_publication):
    (client, captured, _, _, _), game = game_publication
    release = captured[ACTIVE]
    adjusted = []
    for row in release.evidence.rows:
        if row["game_id"] == game["game_id"] and row["player_id"] == 1641705:
            row = dict(row, pts=53, fgm=26, fga=29, ftm=1, fta=1, reb=6, oreb=2, dreb=4)
        elif row["game_id"] == game["game_id"] and row["player_id"] == 100:
            row = dict(row, pts=27, fgm=13, fga=15, ftm=1, fta=1)
        elif row["player_id"] == 1641705 and row["game_date"] < game["game_date"]:
            row = dict(row, pts=39, fgm=19, fga=22, ftm=1, fta=1)
        adjusted.append(row)
    captured[ACTIVE] = replace(
        release, evidence=replace(release.evidence, rows=adjusted)
    )
    result = ask(client, f"How did Wembanyama perform on {game['game_date']}?")
    assert result["semantic_evidence"]["lead_metric"] == "pts"
    assert result["answer"].startswith("Victor Wembanyama recorded 53 points")
    assert "6 rebounds (4.3 below his pregame average)" in result["answer"]
    assert "66.2% of SAS’s points (53 of 80)" in result["answer"]


@pytest.mark.parametrize(
    "metric,count,lead", [("blk", 9, "blk"), ("stl", 8, "stl"), ("blk", 1, "reb")]
)
def test_game_defensive_standout_requires_a_meaningful_total(
    game_publication, metric, count, lead
):
    (client, captured, _, _, _), game = game_publication
    release = captured[ACTIVE]
    rows = [
        dict(row, **{metric: count if row["player_id"] == 1641705 else 3})
        if row["game_id"] == game["game_id"] and row["team_abbr"] == "SAS"
        else row
        for row in release.evidence.rows
    ]
    captured[ACTIVE] = replace(release, evidence=replace(release.evidence, rows=rows))
    result = ask(client, f"How did Wembanyama play on {game['game_date']}?")
    assert result["semantic_evidence"]["lead_metric"] == lead
    if metric == lead:
        label = {"blk": "blocks", "stl": "steals"}[lead]
        assert result["answer"].startswith(
            f"Victor Wembanyama recorded {count} {label}"
        )
        assert f"of SAS’s {label} ({count} of {count + 3})" in result["answer"]


def test_yesterday_remains_exact_when_publication_ends_earlier(game_publication):
    (client, _, _, legacy, _), _ = game_publication
    answer = ask(client, "How did Wembanyama play yesterday?")
    expected = (date.fromisoformat(LATE) - timedelta(days=1)).isoformat()
    assert answer["status"] == "unsupported_coverage"
    assert expected in answer["answer"] and LAST in answer["answer"]
    assert answer["semantic_evidence"] is None and legacy.calls == []


def test_single_game_withholds_team_comparisons_for_incomplete_roster(game_publication):
    (client, captured, _, _, _), game = game_publication
    release = captured[ACTIVE]
    captured[ACTIVE] = replace(
        release,
        evidence=replace(
            release.evidence, game_participants=release.evidence.game_participants[1:]
        ),
    )
    payload = ask(client, f"How did Wembanyama play on {game['game_date']}?")
    assert payload["status"] == "ok", payload["answer"]
    report = payload["semantic_evidence"]
    assert report["rosters"] == {"status": "unavailable", "team": [], "opponent": []}
    assert report["contribution"] == []
    assert report["team_rebounding"]["status"] == "unavailable"
    assert "SAS beat DET 80–75" in payload["answer"]


@pytest.mark.parametrize("endpoint", ["ask", "ask/stream"])
def test_standard_current_season_ask_uses_authorized_release(production, endpoint):
    client, _, reads, legacy, _ = production
    payload = ask(client, OPENER, endpoint=endpoint)
    assert payload["status"] == "ok", payload["answer"]
    assert payload["publication_id"] == ACTIVE
    assert payload["source_coverage"] == {
        "publication_id": ACTIVE,
        "season": DEFAULT_SEASON,
        "phase": "Regular Season",
        "first": FIRST,
        "last": LAST,
        "freshness": "fresh",
        "cohort_complete": False,
    }
    context = payload["conversation_context"]
    assert context["publication_id"] == ACTIVE
    assert context["scope"]["start"] == FIRST
    assert context["scope"]["end"] == LAST
    assert context["scope"]["phases"] == ["Regular Season"]
    assert reads == [None, ACTIVE]
    assert legacy.calls == []


def test_explicit_archived_scope_preserves_ordinary_warehouse(production):
    client, _, reads, legacy, _ = production
    payload = ask(client, "Regular Player total points in the 2024-25 regular season")
    assert payload["status"] == "ok", payload["answer"]
    assert not payload.get("publication_id")
    assert reads == []
    assert legacy.calls == [["2024-25"]]


def test_explicit_as_of_overview_keeps_date_within_release(production):
    client, _, _, legacy, _ = production
    payload = ask(client, f"How is Taylor Example playing as of {EARLY}?")
    assert payload["status"] == "ok", payload["answer"]
    context = payload["conversation_context"]
    assert context["scope"]["start"] == FIRST
    assert context["scope"]["end"] == EARLY
    assert legacy.calls == []


@pytest.mark.parametrize(
    "question",
    [
        f"What stood out about Victor Wembanyama’s rebounding against Detroit on November 17, {DEFAULT_SEASON[:4]}?",
        f"What stood out about Victor Wembanyama’s rebounding against DET on {LAST}?",
        f"How unusual was Victor Wembanyama’s 999-rebound game against Detroit on {LAST}?",
    ],
)
def test_dated_rebounding_starter_resolves_verified_matchup(production, question):
    client, captured, _, legacy, _ = production
    publication = captured[ACTIVE]
    earlier, target = publication.evidence.rows
    rows = [
        dict(earlier, player_id=1641705, player_name="Victor Wembanyama", reb=10),
        dict(
            target,
            player_id=1641705,
            player_name="Victor Wembanyama",
            opponent_abbr="DET",
            reb=18,
            oreb=5,
            dreb=13,
        ),
    ]
    captured[ACTIVE] = replace(
        publication, evidence=replace(publication.evidence, rows=rows)
    )
    payload = ask(client, question)
    assert payload["status"] == "ok", payload["answer"]
    assert payload["publication_id"] == ACTIVE and not legacy.calls
    assert payload["semantic_plan"]["model_calls"] == 0
    assert "18 rebounds against DET" in payload["answer"]
    assert "999" not in payload["answer"]
    assert ("different total" in payload["answer"]) == ("999" in question)
    assert "5 offensive and 13 defensive" in payload["answer"]
    assert (
        "10.0 rebounds across his 1 earlier regular-season appearance in this dataset"
        in payload["answer"]
    )
    assert "8.0 rebounds above" in payload["answer"]
    evidence = payload["semantic_evidence"]
    assert evidence["rows"][0]["game_id"] == target["game_id"]
    assert evidence["scope"]["start_date"] == evidence["scope"]["as_of"] == LAST
    assert evidence["scope"]["opponent_abbr"] == "DET"
    assert evidence["rebounding_context"]["previous"]["first"] == EARLY


def test_rebounding_mismatch_withholds_split_and_incomplete_baseline(production):
    client, captured, _, _, _ = production
    publication = captured[ACTIVE]
    earlier, target = publication.evidence.rows
    rows = [dict(earlier, reb=None), dict(target, reb=18, oreb=5, dreb=12)]
    captured[ACTIVE] = replace(
        publication, evidence=replace(publication.evidence, rows=rows)
    )
    payload = ask(
        client,
        f"What stood out about Taylor Example’s rebounding against Boston on {LAST}?",
    )
    assert payload["status"] == "ok", payload["answer"]
    assert "18 rebounds against BOS" in payload["answer"]
    assert payload["semantic_evidence"]["rebounding_context"] == {
        "split": None,
        "previous": None,
    }


def test_dated_rebounding_rejects_tomorrow_on_the_nba_calendar(production, monkeypatch):
    client, _, reads, legacy, _ = production

    def eastern_today(timezone):
        assert timezone == "America/New_York"
        return date.fromisoformat(EARLY)

    monkeypatch.setattr("app.agent.game_performance.today_in", eastern_today)
    payload = ask(
        client,
        f"What stood out about Taylor Example’s rebounding against Boston on {LAST}?",
    )
    assert payload["status"] == "unsupported_coverage"
    assert "NBA Eastern calendar" in payload["answer"]
    assert not reads and not legacy.calls


def test_rebounding_request_does_not_ignore_different_opponent(production):
    client, _, _, legacy, _ = production
    payload = ask(
        client,
        f"What stood out about Taylor Example’s rebounding against Detroit on {LAST}?",
    )
    assert payload["status"] == "no_observations", payload["answer"]
    assert not payload["semantic_evidence"]["rows"] and not legacy.calls


def test_restored_scoring_followup_keeps_earlier_as_of_date(production):
    client, _, _, legacy, _ = production
    result = ask(
        client,
        "Tell me more about his stats",
        conversation_id=str(uuid4()),
        previous_context={
            "question": "Where did Taylor Example’s points come from this season?",
            "analysis_type": "game_log",
            "players": [{"player_id": 1000, "player_name": "Taylor Example"}],
            "metrics": ["pts"],
            "scope": {
                "season": DEFAULT_SEASON,
                "season_type": "Regular Season",
                "window": "season_to_date",
                "as_of": EARLY,
            },
            "publication_id": ACTIVE,
        },
    )
    assert result["status"] == "ok", result["answer"]
    assert result["publication_id"] == ACTIVE
    evidence = result["semantic_evidence"]
    assert (evidence["scope"]["start"], evidence["scope"]["end"]) == (FIRST, EARLY)
    assert "recent_comparison" not in evidence
    assert legacy.calls == []


def test_more_stats_after_scoring_keeps_primary_scope_and_labels_recent_change(
    production,
):
    client, captured, _, legacy, _ = production
    publication = captured[ACTIVE]
    template = publication.evidence.rows[0]
    rows = list(publication.evidence.rows)
    for index, (day, pts) in enumerate(
        [(4, 18), (8, 24), (12, 30), (14, 36)], start=10
    ):
        makes = pts // 3
        rows.append(
            dict(
                template,
                game_id=f"{index:010}",
                game_date=f"{DEFAULT_SEASON[:4]}-11-{day:02}",
                pts=pts,
                fgm=makes,
                fga=makes,
                fg3m=makes,
                fg3a=makes,
                ftm=0,
                fta=0,
            )
        )
    rows += [
        dict(row, player_id=1001, player_name="Jordan Example", pts=3) for row in rows
    ]
    captured[ACTIVE] = replace(
        publication,
        evidence=replace(publication.evidence, rows=rows, cohort_complete=True),
    )

    opener = ask(client, "Where did Taylor Example’s points come from this season?")
    assert opener["status"] == "ok"
    result = ask(
        client,
        "Tell me more about his stats",
        endpoint="ask/stream",
        conversation_id=opener["conversation_id"],
    )
    assert result["status"] == "ok", result["answer"]
    assert result["publication_id"] == ACTIVE
    assert result["ask_route"]["route"] == "player_overview"
    assert result["charts"]
    evidence = result["semantic_evidence"]
    assert evidence["player_id"] == 1000
    assert (evidence["scope"]["start"], evidence["scope"]["end"]) == (FIRST, LAST)
    points = next(metric for metric in evidence["metrics"] if metric["key"] == "pts")
    assert (points["value"], points["change"], points["percentile"]) == (
        25.5,
        None,
        100,
    )
    recent = evidence["recent_comparison"]
    assert (
        recent["start"],
        recent["end"],
        recent["previous_start"],
        recent["previous_end"],
    ) == (
        f"{DEFAULT_SEASON[:4]}-11-11",
        LAST,
        f"{DEFAULT_SEASON[:4]}-11-04",
        f"{DEFAULT_SEASON[:4]}-11-10",
    )
    assert (recent["observed_games"], recent["previous_appearances"]) == (3, 2)
    recent_points = next(
        metric for metric in recent["metrics"] if metric["key"] == "pts"
    )
    assert (
        recent_points["value"],
        recent_points["previous"]["value"],
        recent_points["change"],
    ) == (32, 21, 11)
    assert legacy.calls == []


@pytest.mark.parametrize("endpoint", ["ask", "ask/stream"])
def test_standard_scoring_mix_and_scoped_log_use_same_publication(production, endpoint):
    client, _, _, legacy, _ = production
    opener = ask(
        client,
        "Where did Taylor Example’s points come from this season?",
        endpoint=endpoint,
    )
    assert opener["status"] == "ok", opener["answer"]
    assert opener["publication_id"] == ACTIVE
    assert opener["answer"].startswith(
        "Three-pointers were Taylor Example’s largest source of points (46.7%)."
    )
    assert "1 more point than two-pointers on 3 fewer makes" in opener["answer"]
    assert "Game high" not in opener["answer"]
    assert len(opener["tables"]) == 1
    assert opener["semantic_evidence"]["scoring_sources"]["rows"][0] == {
        "label": "Three-pointers",
        "points": 21,
        "per_game": 10.5,
        "share": pytest.approx(21 / 45),
        "makes": 7,
    }

    followup = ask(
        client,
        opener["followups"][0],
        endpoint=endpoint,
        conversation_id=opener["conversation_id"],
    )
    assert followup["publication_id"] == ACTIVE
    assert (
        followup["semantic_evidence"]["scope"] == opener["semantic_evidence"]["scope"]
    )
    assert "Game high: 30 points against BOS" in followup["answer"]
    assert "scoring_sources" not in followup["semantic_evidence"]
    assert legacy.calls == []


@pytest.mark.parametrize(
    "question",
    [
        f"Taylor Example total points from {EARLY} to {LATE}",
        f"Taylor Example total points in the {DEFAULT_SEASON} playoffs",
        f"Taylor Example total points in {DEFAULT_SEASON} and 2024-25",
        "Who leads the league in points per game this season?",
    ],
)
def test_publication_refuses_unverified_scopes_and_league_ranks(production, question):
    client, _, reads, legacy, _ = production
    payload = ask(client, question)
    assert payload["status"] == "unsupported_coverage", payload["answer"]
    assert payload["semantic_evidence"] is None
    assert reads and legacy.calls == []


@pytest.mark.parametrize(
    "question",
    [
        "Taylor Example total points",
        "How many points did Taylor Example score?",
        "Taylor Example average points",
    ],
)
def test_simple_questions_keep_structured_publication_evidence(production, question):
    client, _, _, legacy, _ = production
    payload = ask(client, question, endpoint="ask/stream")
    assert payload["status"] == "ok", payload["answer"]
    assert payload["publication_id"] == ACTIVE
    expected = 22.5 if "average" in question else 45
    assert payload["semantic_evidence"]["rows"][0]["value"] == expected
    assert payload["player_profile"]["player"]["player_name"] == "Taylor Example"
    assert (
        payload["tables"]
        and payload["conversation_context"]["publication_id"] == ACTIVE
    )
    assert legacy.calls == []


def test_exact_publication_comparison_uses_requested_metric_and_aggregation(production):
    client, captured, _, legacy, _ = production
    add_riley(captured, ACTIVE, points=5)
    payload = ask(client, "Compare Taylor Example vs Riley Example total points")
    assert payload["status"] == "ok", payload["answer"]
    assert payload["publication_id"] == ACTIVE
    assert payload["semantic_evidence"]["difference"] == 40
    assert len(payload["tables"]) == 2
    assert [p["player"]["player_id"] for p in payload["player_profiles"]] == [
        1000,
        2000,
    ]
    assert not legacy.calls


@pytest.mark.parametrize(
    ("question", "selected_id", "field", "value"),
    [
        ("Example total points", 2000, "rows", 5),
        ("Compare Example vs Riley Example total points", 1000, "difference", 40),
    ],
)
@pytest.mark.parametrize("endpoint", ["ask", "ask/stream"])
def test_publication_stat_uses_resolved_name_after_selection(
    production, question, selected_id, field, value, endpoint
):
    client, captured, _, _, _ = production
    add_riley(captured, ACTIVE, points=5)
    first = ask(client, question, endpoint=endpoint)
    assert first["status"] == "clarification_required"
    choice = next(
        row for row in first["clarification_options"] if row["player_id"] == selected_id
    )
    result = ask(
        client,
        question,
        endpoint=endpoint,
        conversation_id=first["conversation_id"],
        selected_player_id=choice["player_id"],
        selected_player_name=choice["player_name"],
    )
    assert result["status"] == "ok", result["answer"]
    assert result["publication_id"] == ACTIVE
    assert f"Using {choice['player_name']}" in result["answer"]
    observed = result["semantic_evidence"][field]
    assert (observed[0]["value"] if field == "rows" else observed) == value


@pytest.mark.parametrize("withdrawn", [False, True])
def test_player_clarification_stays_on_original_release(production, withdrawn):
    client, captured, reads, legacy, repo = production
    add_riley(captured, ACTIVE, points=12)
    add_riley(captured, PRIOR, points=32)
    question = f"How did Example play on {EARLY}?"
    first = ask(client, question)
    assert first["status"] == "clarification_required"
    assert first["publication_id"] == ACTIVE
    choice = next(
        row for row in first["clarification_options"] if row["player_id"] == 2000
    )

    read = repo.ask_publication_source
    repo.ask_publication_source = lambda pin: read(pin or PRIOR)
    if withdrawn:
        del captured[ACTIVE]

    def pick():
        return ask(
            client,
            question,
            conversation_id=first["conversation_id"],
            selected_player_id=choice["player_id"],
            selected_player_name=choice["player_name"],
        )

    result = pick()
    if withdrawn:
        assert result["status"] == "publication_unavailable"
        assert result["semantic_evidence"] is None
        replay = result["publication_retry"]
        assert replay["publication_id"] == ACTIVE and replay["player_id"] == 2000
        assert replay["player_name"] == "Riley Example"
        assert pick()["status"] == "publication_unavailable"
    else:
        assert result["status"] == "ok", result["answer"]
        assert result["publication_id"] == ACTIVE
        metrics = {
            metric["key"]: metric["value"]
            for metric in result["semantic_evidence"]["metrics"]
        }
        assert metrics["pts"] == 12
    assert reads == [None, ACTIVE, ACTIVE, ACTIVE]
    assert not legacy.calls


def test_restored_publication_choice_validates_original_release(production):
    client, captured, reads, legacy, _ = production
    add_riley(captured, PRIOR, points=5)
    question = "Example total points"
    result = ask(
        client,
        question,
        conversation_id=str(uuid4()),
        previous_context={"question": question, "publication_id": PRIOR},
        selected_player_id=2000,
        selected_player_name="Riley Example",
    )
    assert result["status"] == "ok", result["answer"]
    assert result["publication_id"] == PRIOR
    assert result["semantic_evidence"]["rows"][0]["value"] == 5
    assert reads == [PRIOR, PRIOR]
    assert not legacy.calls


def test_saved_unavailable_name_reply_replays_original_question(production):
    client, captured, _, _, _ = production
    add_riley(captured, ACTIVE, points=12)
    question = "Example total points"
    first = ask(client, question)
    assert first["status"] == "clarification_required"
    original = captured.pop(ACTIVE)
    failed = ask(client, "Riley Example", conversation_id=first["conversation_id"])
    assert failed["status"] == "publication_unavailable"
    retry = failed["publication_retry"]
    assert retry["question"] == question
    assert retry["player_name"] == "Riley Example" and retry["publication_id"] == ACTIVE

    captured[ACTIVE] = original
    recovered = ask(
        client,
        retry["question"],
        conversation_id=str(uuid4()),
        previous_context={
            "question": retry["question"],
            "publication_id": retry["publication_id"],
        },
        selected_player_id=retry["player_id"],
        selected_player_name=retry["player_name"],
    )
    assert recovered["status"] == "ok", recovered["answer"]
    assert recovered["semantic_evidence"]["rows"][0]["value"] == 12


def test_relative_game_clarification_keeps_its_eastern_date(production, monkeypatch):
    client, captured, _, _, _ = production
    add_riley(captured, ACTIVE, points=12)
    publication = captured[ACTIVE]
    riley = publication.evidence.rows[-1]
    tomorrow = date.fromisoformat(EARLY) + timedelta(days=1)
    captured[ACTIVE] = replace(
        publication,
        evidence=replace(
            publication.evidence,
            rows=[
                *publication.evidence.rows,
                dict(riley, game_id="0000000099", game_date=str(tomorrow), pts=42),
            ],
        ),
    )
    eastern_today = [tomorrow]
    monkeypatch.setattr(
        "app.agent.game_performance.today_in", lambda _: eastern_today[0]
    )
    question = "How did Example play yesterday?"
    first = ask(client, question)
    assert first["status"] == "clarification_required"
    assert first["clarification_question"] == f"How did Example play {EARLY}?"
    eastern_today[0] += timedelta(days=1)
    resolved = ask(
        client,
        question,
        conversation_id=first["conversation_id"],
        selected_player_id=2000,
        selected_player_name="Riley Example",
    )
    assert resolved["status"] == "ok", resolved["answer"]
    report = resolved["semantic_evidence"]
    assert report["game"]["game_date"] == EARLY
    assert (
        next(metric["value"] for metric in report["metrics"] if metric["key"] == "pts")
        == 12
    )


@pytest.mark.parametrize("scalar", [True, False])
@pytest.mark.parametrize("recovery", ["selection", "saved_retry", "warm_saved_retry"])
def test_multi_player_browser_recovery_revalidates_prior_choice_on_publication(
    production, scalar, recovery
):
    client, captured, reads, legacy, _ = production
    add_riley(captured, PRIOR, points=5)
    publication = captured[PRIOR]
    row = publication.evidence.rows[0]
    captured[PRIOR] = replace(
        publication,
        evidence=replace(
            publication.evidence,
            rows=[
                *publication.evidence.rows,
                dict(row, player_id=3000, player_name="Kyle Mason", pts=11),
                dict(row, player_id=4000, player_name="Drew Mason", pts=17),
            ],
        ),
    )
    question = (
        "Compare Example vs Mason total points"
        if scalar
        else "Compare Example vs Mason this season\nSelected player: Riley Example"
    )
    hints = (
        {
            "publication_identity_choices": [
                {
                    "mention": "Example",
                    "player_id": 2000,
                    "player_name": "Riley Example",
                }
            ]
        }
        if scalar
        else {"publication_comparison_choices": {"0": 2000}}
    )
    conversation_id = str(uuid4())
    prior = {"question": question, "publication_id": PRIOR, **hints}
    if recovery == "warm_saved_retry":
        earlier = "Taylor Example total points"
        assert (
            ask(
                client,
                earlier,
                conversation_id=conversation_id,
                previous_context={"question": earlier, "publication_id": PRIOR},
            )["status"]
            == "ok"
        )
    if recovery != "selection":
        pending = ask(
            client,
            question,
            conversation_id=conversation_id,
            previous_context={**prior, "restore_publication_clarification": True},
        )
        assert pending["status"] == "clarification_required", pending["answer"]
        assert pending["publication_id"] == PRIOR
        assert {option["player_id"] for option in pending["clarification_options"]} == {
            3000,
            4000,
        }
    result = ask(
        client,
        question,
        conversation_id=conversation_id,
        selected_player_id=3000,
        selected_player_name="Kyle Mason",
        previous_context=prior,
    )
    assert result["status"] == "ok", result["answer"]
    assert result["publication_id"] == PRIOR
    report = result["semantic_evidence"]
    value = (
        report["difference"]
        if scalar
        else next(row["delta"] for row in report["metrics"] if row["key"] == "pts")
    )
    assert value == -6
    assert reads == [PRIOR] * (
        2 + 2 * (recovery != "selection") + 2 * (recovery == "warm_saved_retry")
    )
    assert not legacy.calls


def test_saved_retry_hints_cannot_be_reused_for_another_question(production):
    client, _, reads, _, _ = production
    response = client.post(
        "/api/agent/ask",
        json={
            "question": "Taylor Example average points",
            "conversation_id": str(uuid4()),
            "previous_context": {
                "question": "Taylor Example total points",
                "publication_id": PRIOR,
                "restore_publication_clarification": True,
            },
        },
    )
    assert response.status_code == 400 and not reads


def test_saved_retry_keeps_choices_if_the_original_release_is_still_unavailable(
    production,
):
    client, captured, reads, legacy, _ = production
    original = captured.pop(PRIOR)
    question = "Compare Example vs Mason total points"
    prior = {
        "question": question,
        "publication_id": PRIOR,
        "restore_publication_clarification": True,
        "publication_identity_choices": [
            {"mention": "Example", "player_id": 2000, "player_name": "Riley Example"}
        ],
    }
    result = ask(client, question, conversation_id=str(uuid4()), previous_context=prior)
    assert result["status"] == "publication_unavailable"
    assert result["semantic_evidence"] is None
    retry = result["publication_retry"]
    assert retry["question"] == question and retry["publication_id"] == PRIOR
    assert retry["restore_clarification"] is True
    assert retry["choices"]["Example"]["player_id"] == 2000
    assert reads == [PRIOR] and not legacy.calls
    captured[PRIOR] = original


def test_appearance_comparison_exposes_each_players_dates(production):
    client, captured, _, _, _ = production
    add_riley(captured, ACTIVE, points=5)
    publication = captured[ACTIVE]
    taylor, _, riley = publication.evidence.rows
    second = date.fromisoformat(EARLY) + timedelta(days=2)
    third = second + timedelta(days=1)
    rows = [
        *publication.evidence.rows,
        dict(taylor, game_id="0000000050", game_date=str(second), pts=9),
        dict(riley, game_id="0000000051", game_date=str(third), pts=7),
    ]
    captured[ACTIVE] = replace(
        publication, evidence=replace(publication.evidence, rows=rows)
    )
    answer = ask(
        client,
        "Compare Taylor Example and Riley Example in their first two appearances",
    )
    assert answer["status"] == "ok", answer["answer"]
    assert [
        (profile["first"], profile["last"]) for profile in answer["player_profiles"]
    ] == [
        (EARLY, str(second)),
        (EARLY, str(third)),
    ]


def test_cold_followup_keeps_original_publication_or_withholds(production):
    client, captured, reads, legacy, _ = production
    payload = ask(
        client,
        "What about total assists?",
        conversation_id=str(uuid4()),
        previous_context={
            "question": f"Taylor Example total points from {EARLY} to {EARLY}",
            "players": [{"player_id": 1000, "player_name": "Taylor Example"}],
            "metrics": ["pts"],
            "scope": {
                "season": DEFAULT_SEASON,
                "season_type": "Regular Season",
                "window": "date_range",
                "start_date": EARLY,
                "as_of": EARLY,
            },
            "publication_id": PRIOR,
        },
    )
    assert payload["status"] == "ok", payload["answer"]
    assert payload["publication_id"] == PRIOR
    assert payload["semantic_evidence"]["rows"][0]["value"] == 2
    assert reads == [PRIOR, PRIOR]
    captured.pop(PRIOR)
    failed = ask(
        client,
        f"Taylor Example total points from {EARLY} to {EARLY}",
        conversation_id=payload["conversation_id"],
    )
    assert failed["status"] == "publication_unavailable", failed["answer"]
    assert failed["semantic_evidence"] is None
    assert reads[-1] == PRIOR and legacy.calls == []


def test_revocation_before_response_withholds_facts(production):
    client, _, reads, legacy, repo = production
    original = repo.ask_publication_source

    def revoking_reader(publication_id):
        if publication_id:
            raise ValueError("authority revoked")
        return original(publication_id)

    repo.ask_publication_source = revoking_reader
    payload = ask(client, OPENER)
    assert payload["status"] == "publication_unavailable", payload["answer"]
    assert payload["semantic_evidence"] is None
    assert not payload.get("conversation_context")
    assert reads == [None]
    assert legacy.calls == []


def test_same_conversation_cannot_move_to_another_source(production):
    client, _, reads, legacy, _ = production
    original = ask(client, OPENER)
    conversation_id = original["conversation_id"]
    response = client.post(
        "/api/agent/ask",
        json={
            "question": "What about total assists?",
            "conversation_id": conversation_id,
            "previous_context": {"question": OPENER, "publication_id": PRIOR},
        },
    )
    assert response.status_code == 409, response.text
    for question in (
        "Who won MVP in 2024-25?",
        "How many points did Stephen Curry score in the 2025-26 NBA Finals?",
    ):
        refused = ask(client, question, conversation_id=conversation_id)
        assert refused["status"] == "unsupported_scope", refused["answer"]
        assert refused["semantic_evidence"] is None
        assert not refused.get("participation_evidence")
    assert reads == [None, ACTIVE] and legacy.calls == []


def test_explicit_legacy_mode_retains_unpublished_current_behavior(production):
    client, _, _, legacy, repo = production
    repo.ask_publication_source = lambda _: None
    payload = ask(client, OPENER)
    assert not payload.get("publication_id")
    assert legacy.calls and DEFAULT_SEASON in legacy.calls[0]
