from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from app.agent.claims import validate
from app.agent.conversation import InMemoryConversationStore
from app.agent.semantic_answer import SemanticAsk
from app.agent.semantics import Evidence
from tests.test_semantic_migration import settings

SEASON = "2025-26"
PHASE = "Regular Season"
FIRST = date(2025, 11, 1)
PLAYER = "Taylor James"
QUESTION = (
    "Where did Taylor James’s points come from in the 2025-26 regular season "
    "from 2025-11-01 to 2025-11-13?"
)


def source(games=13):
    return [
        dict(
            player_id=271,
            player_name=PLAYER,
            game_id=str(i + 1).zfill(10),
            game_date=(FIRST + timedelta(days=i)).isoformat(),
            season=SEASON,
            season_type=PHASE,
            team_abbr="DET",
            opponent_abbr="BOS",
            pts=15 if i else 18,
            fgm=5 if i else 6,
            fg3m=2 if i else 3,
            ftm=3,
            ast=4,
        )
        for i in range(games)
    ]


def answerer(rows, store=None):
    evidence = Evidence(
        rows,
        frozenset({(SEASON, PHASE)}),
        "source fixture",
        "scoped-fixture",
        {(SEASON, PHASE): max(row["game_date"] for row in rows)},
        complete=True,
    )
    agent = SemanticAsk(
        settings(SEASON),
        SimpleNamespace(load=lambda _: ({}, evidence)),
        store,
    )
    return lambda q: agent.answer(
        q, client=None, model="fixture", conversation_id="scoring-report"
    )


@pytest.mark.parametrize(
    "question",
    [
        QUESTION,
        "Show me Taylor James’ scoring mix from 2025-11-01 to 2025-11-13.",
        "Break down Taylor James scoring by source between 2025-11-01 and 2025-11-13.",
    ],
)
def test_scoring_report_shares_scope_with_all_thirteen_appearances(question):
    answer = answerer(source())(question)
    assert answer["status"] == "ok", answer["answer"]
    assert answer["semantic_plan"]["model_calls"] == 0
    assert (
        "198 points across 13 recorded appearances (15.2 per game)" in answer["answer"]
    )
    assert answer["answer"].startswith(
        "Three-pointers were Taylor James’s largest source of points (40.9%)."
    )
    assert "3 more points than two-pointers on 12 fewer makes" in answer["answer"]
    assert "Game high" not in answer["answer"]
    assert answer["followups"][0] == "Show scoring game by game"
    assert "date_range" not in answer["assumptions"][0]
    assert answer["tables"][0]["rows"][0] == [
        "2025-11-01",
        "0000000001",
        PHASE,
        "DET",
        "BOS",
        "18.0",
    ]
    assert len(answer["tables"][0]["rows"]) == 13
    assert answer["tables"][1]["rows"] == [
        ["Three-pointers", 81, "6.2", "40.9%", 27],
        ["Two-pointers", 78, "6.0", "39.4%", 39],
        ["Free throws", 39, "3.0", "19.7%", 39],
    ]
    rows = answer["semantic_evidence"]["scoring_sources"]["rows"]
    assert [row["share"] for row in rows] == pytest.approx(
        [81 / 198, 78 / 198, 39 / 198]
    )
    assert [chart["type"] for chart in answer["charts"]] == ["line", "bar"]
    assert len(answer["charts"][0]["series"][0]["points"]) == 13
    assert [p["y"] for p in answer["charts"][1]["series"][0]["points"]] == [81, 78, 39]
    assert validate(answer)["errors"] == []
    answer["charts"][1]["series"][0]["points"][0]["y"] += 1
    assert validate(answer)["status"] == "invalid"


def test_scoring_report_never_guesses_between_source_identities():
    rows = source()
    rows.append(
        dict(rows[0], player_id=272, player_name="Casey James", team_abbr="BOS")
    )
    answer = answerer(rows)(
        "Where did James’s points come from in the 2025-26 regular season?"
    )
    assert answer["status"] == "clarification_required"
    assert {item["player_id"] for item in answer["clarification_options"]} == {271, 272}
    assert not answer["tables"] and not answer["charts"]


@pytest.mark.parametrize("wrong", [None, 0, 40])
def test_scoring_sources_withheld_when_an_appearance_cannot_reconcile(wrong):
    rows = source()
    rows[1]["ftm"] = wrong
    answer = answerer(rows)(QUESTION)
    assert answer["status"] == "ok"
    assert "Scoring sources are unavailable" in answer["answer"]
    assert "largest source of points" not in answer["answer"]
    assert "Game high" not in answer["answer"]
    assert answer["followups"][0] == "Show scoring game by game"
    assert [len(table["rows"]) for table in answer["tables"]] == [13]
    assert [chart["type"] for chart in answer["charts"]] == ["line"]
    assert validate(answer)["errors"] == []


def test_scoring_total_uses_full_period_when_game_log_is_bounded():
    rows = source(101)
    answer = answerer(rows)(
        "Break down Taylor James’s points from 2025-11-01 to 2026-02-09"
    )
    assert answer["status"] == "ok"
    assert "1,518 points across 101 recorded appearances" in answer["answer"]
    assert "display_limit_applied_latest_games" in answer["assumptions"]
    assert len(answer["tables"][0]["rows"]) == 100
    assert sum(row[1] for row in answer["tables"][1]["rows"]) == 1518


@pytest.mark.parametrize(
    "log_question",
    [
        "Show scoring game by game",
        "Show me the points game log.",
        "Show Taylor James’s scoring game by game.",
        "Show me his scoring game by game.",
    ],
)
def test_normal_followups_preserve_player_and_range_and_can_show_alternate_metric(
    log_question,
):
    ask = answerer(source(), InMemoryConversationStore())
    original = ask(QUESTION)
    history = ask(log_question)
    assert history["status"] == "ok", history["answer"]
    assert "15.2 points per game over 13 recorded appearances." in history["answer"]
    assert "Game high: 18 points against BOS on 2025-11-01." in history["answer"]
    assert "scoring_sources" not in history["semantic_evidence"]
    assert history["tables"] == original["tables"][:1]
    followup = ask("Show me his assists game by game.")
    assert followup["status"] == "ok", followup["answer"]
    assert followup["semantic_evidence"]["metric"]["key"] == "ast"
    assert followup["semantic_evidence"]["scope"] == {
        **history["semantic_evidence"]["scope"],
        "metric": "ast",
    }
    assert [r[-1] for r in followup["tables"][0]["rows"]] == ["4.0"] * 13
    assert validate(followup)["errors"] == []


@pytest.mark.parametrize("subject", ["Taylor James’s", "his"])
def test_game_log_followup_explicit_dates_override_the_prior_range(subject):
    ask = answerer(source(), InMemoryConversationStore())
    ask(QUESTION)
    result = ask(f"Show {subject} scoring game by game from 2025-11-03 to 2025-11-07.")
    assert result["status"] == "ok", result["answer"]
    log = result["tables"][0]["rows"]
    assert len(log) == 5
    assert (log[0][0], log[-1][0]) == ("2025-11-03", "2025-11-07")


@pytest.mark.parametrize(
    "free_throws,leaders",
    [
        (3, "Two-pointers and three-pointers"),
        (6, "Two-pointers, three-pointers, and free throws"),
    ],
)
def test_scoring_report_describes_tied_sources_without_inventing_a_winner(
    free_throws, leaders
):
    rows = [dict(source(1)[0], pts=12 + free_throws, fgm=5, fg3m=2, ftm=free_throws)]
    answer = answerer(rows)(QUESTION)
    assert answer["answer"].startswith(
        f"{leaders} tied for Taylor James’s largest scoring source"
    )
    assert "fewer makes" not in answer["answer"]
    assert "6 points each" in answer["answer"]
    assert validate(answer)["errors"] == []


def test_scoring_report_only_compares_fewer_makes_when_supported():
    rows = [dict(source(1)[0], pts=17, fgm=7, fg3m=2, ftm=1)]
    answer = answerer(rows)(QUESTION)
    assert answer["answer"].startswith(
        "Two-pointers were Taylor James’s largest source of points (58.8%)."
    )
    assert "4 more points than three-pointers" in answer["answer"]
    assert "fewer makes" not in answer["answer"]


def test_zero_total_has_no_scoring_share_or_largest_source():
    rows = [dict(source(1)[0], pts=0, fgm=0, fg3m=0, ftm=0)]
    answer = answerer(rows)(QUESTION)
    assert "scored zero points across 1 recorded appearance" in answer["answer"]
    assert "largest source" not in answer["answer"]
    assert [row[-2:] for row in answer["tables"][1]["rows"]] == [["Unavailable", 0]] * 3
    assert validate(answer)["errors"] == []


@pytest.mark.parametrize("column,replacement", [(3, "41.0%"), (4, 29)])
def test_scoring_share_and_makes_are_bound_to_source_claims(column, replacement):
    answer = answerer(source())(QUESTION)
    answer["tables"][1]["rows"][0][column] = replacement
    assert validate(answer)["status"] == "invalid"
