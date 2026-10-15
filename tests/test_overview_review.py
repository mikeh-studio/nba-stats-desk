"""Independent values and actual endpoint checks for the Overview PR review."""

import json
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest
from app.agent.overview_insights import select_insights
from app.agent.overview_positions import comparison_intent, peer_question
from app.agent.performance_overview import overview_scope, wants_overview
from app.agent.semantics import Evidence, SemanticError
from fastapi.testclient import TestClient
from tests.test_overview_positions import fixture
from tests.test_semantic_migration import Model, settings


@pytest.fixture
def source():
    grouped, context = fixture(30)
    rows = []
    for phase, season, month, offset in [
        ("Regular Season", "2024-25", "2024-11", 0),
        ("Playoffs", "2024-25", "2025-05", 30),
        ("Playoffs", "2023-24", "2024-05", 10),
        ("Regular Season", "2023-24", "2023-11", 0),
    ]:
        for group in grouped.values():
            for old in group:
                row = deepcopy(old)
                day = int(row["game_id"])
                row.update(
                    game_id=f"{season}-{phase}-{day}",
                    season=season,
                    season_type=phase,
                    game_date=f"{month}-{day:02}",
                    pts=row["pts"] + offset,
                    min=[10, 20, 40, 50, 60][day - 1],
                    fgm=2,
                    fga=4,
                )
                rows.append(row)
    coverage = frozenset((r["season"], r["season_type"]) for r in rows)
    return Evidence(
        rows,
        coverage,
        "fictional",
        "review-fixture",
        {k: "2025-06-30" for k in coverage},
        complete=True,
        position_context=context,
    )


@pytest.fixture
def ask(source):
    from app.main import app, get_agent_client, get_repository, get_settings
    from app.repository import BigQueryWarehouseRepository

    config = replace(settings("2024-25"), performance_cache_prewarm_enabled=False)
    repo = BigQueryWarehouseRepository(config, client=SimpleNamespace())
    repo._governed_warehouse = SimpleNamespace(
        load=lambda seasons: ({"capture": {}}, source)
    )
    old = dict(app.dependency_overrides)
    app.dependency_overrides.update(
        {get_settings: lambda: config, get_repository: lambda: repo}
    )

    def run(question, suffix, model=None, conversation_id=None):
        # Unsupported shapes must clarify without attempting model generation.
        app.dependency_overrides[get_agent_client] = lambda: model or object()
        with TestClient(app) as client:
            response = client.post(
                "/api/agent/ask" + suffix,
                json={"question": question, "conversation_id": conversation_id},
            )
            assert response.status_code == 200, response.text
            if not suffix:
                return response.json()
            return next(
                json.loads(line[6:])["payload"]
                for line in response.text.splitlines()
                if line.startswith("data: ")
                and json.loads(line[6:]).get("type") == "final"
            )

    yield run
    app.dependency_overrides.clear()
    app.dependency_overrides.update(old)


@pytest.mark.parametrize("suffix", ["", "/stream"])
def test_league_leader_reaches_rank_planner_not_single_player(ask, suffix):
    model = Model(
        "pts",
        operation="rank",
        player_name=None,
        min_games=5,
        direction="higher",
        limit=3,
        season_type="Regular Season",
    )
    p = ask("Who led the league in scoring in 2024-25?", suffix, model)
    assert p["status"] == "ok"
    assert model.calls and not p["semantic_evidence"].get("presentation")
    assert [r["player_id"] for r in p["semantic_evidence"]["rows"]] == [30, 29, 28]
    assert [r["value"] for r in p["semantic_evidence"]["rows"]] == [30, 29, 28]


@pytest.mark.parametrize("suffix", ["", "/stream"])
def test_shooting_log_reaches_metric_planner(ask, suffix):
    model = Model(
        "fg_pct",
        operation="game_log",
        aggregation="ratio",
        player_name="resolved_player_1",
        season_type="Regular Season",
        limit=100,
    )
    p = ask(
        "Give Example 1 a season game log of shooting percentage from the field in 2024-25.",
        suffix,
        model,
    )
    assert model.calls and p["status"] == "ok"
    assert not p["semantic_evidence"].get("presentation")
    assert [r["value"] for r in p["semantic_evidence"]["rows"]] == [0.5] * 5
    assert "50.0%" in str(p["tables"])


@pytest.mark.parametrize("suffix", ["", "/stream"])
@pytest.mark.parametrize(
    "question,phrase",
    [
        ("Who were the best rebounding guards in 2024-25?", "Position comparisons"),
        (
            "How did Example 1 vs Example 2 perform among guards in 2024-25?",
            "two-player scorecard",
        ),
        (
            "Compare Example 1 with Example 2 among guards in 2024-25.",
            "two-player scorecard",
        ),
        (
            "Give an overview of Example 1 among guards and wings in 2024-25.",
            "Choose one comparison group",
        ),
        (
            "How was Example 1 scoring in January 2025 compared with the previous season and other guards?",
            "current window alone",
        ),
    ],
)
def test_unsupported_composition_preserves_requested_intent(
    ask, suffix, question, phrase
):
    p = ask(question, suffix)
    assert p["status"] == "clarification_required"
    assert phrase in p["answer"] and "Which player" not in p["answer"]
    assert not p.get("semantic_evidence") and not p["tables"]


@pytest.mark.parametrize("suffix", ["", "/stream"])
@pytest.mark.parametrize(
    "baseline,phase,start,change",
    [
        ("previous season's playoffs", "Playoffs", "2023-07-01", 20),
        ("prior season", "Playoffs", "2023-07-01", 20),
        ("his regular season", "Regular Season", "2024-07-01", 30),
        ("previous season's regular season", "Regular Season", "2023-07-01", 30),
    ],
)
def test_explicit_baseline_dates_phase_and_arithmetic(
    ask, suffix, baseline, phase, start, change
):
    q = f"How did Example 1's scoring in the 2024-25 playoffs compare with {baseline} and other guards?"
    p = ask(q, suffix)
    assert p["status"] == "ok", p
    e = p["semantic_evidence"]
    assert e["scope"]["previous_start"] == start
    assert e["scope"]["previous_phases"] == [phase]
    points = next(m for m in e["metrics"] if m["key"] == "pts")
    assert points["value"] == 31 and points["change"] == change
    assert points["previous"]["observed_games"] == 5
    assert f"up {change:.1f} against his baseline" in p["answer"]
    assert e["request_fulfillment"]["status"] == "complete"


@pytest.mark.parametrize("suffix", ["", "/stream"])
@pytest.mark.parametrize("window", ["regular season", "first two appearances"])
def test_missing_focus_does_not_substitute_or_make_empty_sentence(
    ask, source, suffix, window
):
    next(
        r
        for r in source.rows
        if r["player_id"] == 1
        and r["season"] == "2024-25"
        and r["season_type"] == "Regular Season"
    )["pts"] = None
    p = ask(f"How was Example 1's scoring in the {window} of 2024-25?", suffix)
    assert p["status"] == "ok"
    assert "Points are incomplete" in p["answer"]
    assert "rebounds" not in p["answer"] and "averaged  per game" not in p["answer"]
    assert (
        p["semantic_evidence"]["request_fulfillment"]["reason"]
        == "incomplete_requested_metric"
    )
    assert p["semantic_evidence"]["request_fulfillment"]["status"] == "partial"


@pytest.mark.parametrize("suffix", ["", "/stream"])
def test_two_player_first_appearances_share_stat_and_profile_sample(ask, suffix):
    p = ask(
        "Compare Example 1 and Example 2 in their first two appearances of 2024-25.",
        suffix,
    )
    assert p["status"] == "ok"
    for profile in p["player_profiles"]:
        assert profile["player"]["games_sampled"] == 2
        assert profile["minutes"] == 30 and profile["minutes_per_game"] == 15
    for metric in p["semantic_evidence"]["metrics"]:
        for side in ("left", "right"):
            assert metric[side]["observed_games"] == 2
            assert metric[side]["game_ids"] == [
                "2024-25-Regular Season-1",
                "2024-25-Regular Season-2",
            ]


@pytest.mark.parametrize("suffix", ["", "/stream"])
def test_defending_champions_does_not_change_general_focus(ask, suffix):
    p = ask(
        "How did Example 1 perform for the defending champions in the 2024-25 regular season?",
        suffix,
    )
    assert p["status"] == "ok"
    assert p["semantic_evidence"]["presentation"]["profile"] == "general"


def test_three_point_request_still_bypasses_overview():
    assert not wants_overview("How was Example 1's three-point shooting in 2024-25?")


def test_baseline_intent_retained_separately_from_normalized_question():
    q = "Example scoring in the 2024-25 playoffs compared with the previous season's playoffs and other guards"
    intent = comparison_intent(q)
    assert intent["baseline_period"] == "prior_season" and intent[
        "baseline_phases"
    ] == ["Playoffs"]
    s = overview_scope(peer_question(q), "2024-25", comparison=intent)
    assert str(s["previous_start"]) == "2023-07-01" and s["previous_phases"] == [
        "Playoffs"
    ]
    with pytest.raises(SemanticError):
        overview_scope(
            q.replace("in the 2024-25 playoffs", "in January 2025"), "2024-25"
        )


def test_sub_display_assist_rate_does_not_emit_zero_change():
    from tests.test_overview_insights import metrics

    values = metrics.__wrapped__()
    for m in values:
        if m["key"] == "ast":
            m.update(change=0.01, display_change=0.01)
        if m["key"] == "ast_per36":
            m.update(change=-0.001, display_change=-0.001)
    finding = next(
        f
        for f in select_insights(values, {}, "regular-season")
        if f["id"] == "creation_context"
    )
    assert "fell 0.0" not in finding["summary"]


@pytest.mark.parametrize("suffix", ["", "/stream"])
def test_defense_lead_keeps_matching_evidence_keys(ask, source, suffix):
    for row in source.rows:
        row["stl"] = row["player_id"]
        row["blk"] = row["player_id"]
        if row["player_id"] == 1:
            row["blk"] = 20 if row["season"] == "2024-25" else 1
    p = ask(
        "How did Example 1's defense in the 2024-25 regular season compare with the prior season and other guards?",
        suffix,
    )
    e = p["semantic_evidence"]
    assert e["insights"][0]["metric_keys"] == ["stl"]
    assert "Steals among guards" == e["insights"][0]["title"]
    assert e["insights"][1]["metric_keys"] == ["blk"]
    assert "blocks" in e["insights"][1]["text"]
    assert e["presentation"]["takeaways"][0]["insight_id"] == "activity_context_stl"


@pytest.mark.parametrize(
    "question",
    [
        "Give Example 1 game logs of shooting percentages in 2024-25.",
        "Show Example 1 shooting percentages game-by-game in 2024-25.",
    ],
)
def test_explicit_metric_operation_variants_never_become_overview(question):
    assert not wants_overview(question)


@pytest.mark.parametrize("suffix", ["", "/stream"])
@pytest.mark.parametrize(
    "reply",
    ["Guards", "Wings", "Give an overview of Example 2 among guards in 2024-25."],
)
def test_group_clarification_resumes_same_conversation(ask, suffix, reply):
    from uuid import uuid4

    cid = str(uuid4())
    first = ask(
        "Give an overview of Example 1 among guards and wings in 2024-25.",
        suffix,
        conversation_id=cid,
    )
    assert first["status"] == "clarification_required"
    result = ask(reply, suffix, conversation_id=cid)
    assert result["status"] == "ok"
    evidence = result["semantic_evidence"]
    expected = "Wing" if reply == "Wings" else "Guard"
    assert evidence["request_fulfillment"]["requested_group"] == expected
    metric = next(m for m in evidence["metrics"] if m["key"] == "pts")
    assert metric["value"] == (17 if "Example 2" in reply else 16)
    assert result["conversation_id"] == cid


@pytest.mark.parametrize("suffix", ["", "/stream"])
@pytest.mark.parametrize(
    "first",
    [
        "Compare Example 1 with Example 2 among guards in 2024-25.",
        "Who were the best rebounding guards in 2024-25?",
    ],
)
def test_composition_reply_can_restate_single_player(ask, suffix, first):
    from uuid import uuid4

    cid = str(uuid4())
    assert ask(first, suffix, conversation_id=cid)["status"] == "clarification_required"
    assert (
        ask("Example 1", suffix, conversation_id=cid)["status"]
        == "clarification_required"
    )
    result = ask(
        "How was Example 1 rebounding among guards in 2024-25?",
        suffix,
        conversation_id=cid,
    )
    assert result["status"] == "ok"
    assert (
        result["semantic_evidence"]["request_fulfillment"]["requested_group"] == "Guard"
    )


@pytest.mark.parametrize("suffix", ["", "/stream"])
@pytest.mark.parametrize(
    "reply",
    ["Use November 2023 as the comparison", "from 2023-11-01 through 2023-11-30"],
)
def test_baseline_reply_never_replaces_current_window(ask, suffix, reply):
    from uuid import uuid4

    cid = str(uuid4())
    first = "How was Example 1 scoring in November 2024 compared with the previous season and other guards?"
    assert ask(first, suffix, conversation_id=cid)["status"] == "clarification_required"
    rejected = ask(reply, suffix, conversation_id=cid)
    assert rejected["status"] == "clarification_required"
    assert rejected.get("semantic_evidence") is None
    assert "current window alone" in rejected["answer"]
    result = ask(
        "How was Example 1 scoring in November 2024 among guards?",
        suffix,
        conversation_id=cid,
    )
    assert result["status"] == "ok"
    assert result["semantic_evidence"]["scope"]["start"] == "2024-11-01"
    assert result["semantic_evidence"]["scope"]["end"] == "2024-11-30"
    assert (
        next(m for m in result["semantic_evidence"]["metrics"] if m["key"] == "pts")[
            "value"
        ]
        == 1
    )


@pytest.mark.parametrize("suffix", ["", "/stream"])
@pytest.mark.parametrize("connector", ["vs", "vs.", "versus", "compared to"])
def test_single_player_cohort_wording_keeps_overview(ask, suffix, connector):
    result = ask(f"How did Example 1 play {connector} other guards in 2024-25?", suffix)
    assert result["status"] == "ok"
    assert (
        result["semantic_evidence"]["request_fulfillment"]["requested_group"] == "Guard"
    )
    assert (
        next(m for m in result["semantic_evidence"]["metrics"] if m["key"] == "pts")[
            "value"
        ]
        == 16
    )


@pytest.mark.parametrize("suffix", ["", "/stream"])
def test_restatement_after_answer_preserves_new_dates(ask, suffix):
    from uuid import uuid4

    cid = str(uuid4())
    first = ask(
        "Give an overview of Example 1 in November 2024.", suffix, conversation_id=cid
    )
    assert first["status"] == "ok"
    assert first["semantic_evidence"]["scope"]["start"] == "2024-11-01"
    assert (
        ask("How was his scoring among guards and wings?", suffix, conversation_id=cid)[
            "status"
        ]
        == "clarification_required"
    )
    result = ask(
        "How was his scoring among guards in the 2024-25 regular season?",
        suffix,
        conversation_id=cid,
    )
    assert result["status"] == "ok"
    scope = result["semantic_evidence"]["scope"]
    assert (scope["start"], scope["end"]) == ("2024-07-01", "2025-06-30")
    assert scope["phases"] == ["Regular Season"]
    assert result["semantic_evidence"]["player_id"] == 1


@pytest.mark.parametrize("suffix", ["", "/stream"])
@pytest.mark.parametrize(
    "prefix", ["Please give", "Can you give", "Okay, show", "Overview of"]
)
def test_natural_restatement_recovers(ask, suffix, prefix):
    from uuid import uuid4

    cid = str(uuid4())
    assert (
        ask(
            "Give an overview of Example 1 among guards and wings in 2024-25.",
            suffix,
            conversation_id=cid,
        )["status"]
        == "clarification_required"
    )
    question = (
        f"{prefix} an overview of Example 1 among guards in 2024-25."
        if prefix != "Overview of"
        else "Overview of Example 1 among guards in 2024-25."
    )
    assert ask(question, suffix, conversation_id=cid)["status"] == "ok"


@pytest.mark.parametrize("suffix", ["", "/stream"])
@pytest.mark.parametrize(
    "reply,group",
    [
        ("Guards", "Guard"),
        ("Forwards", "Wing"),
        ("Centers", "Big"),
        ("How about wings?", "Wing"),
    ],
)
def test_oxford_list_and_group_aliases(ask, suffix, reply, group):
    from uuid import uuid4

    cid = str(uuid4())
    assert (
        ask(
            "Give an overview of Example 1 among guards, wings, and bigs in 2024-25.",
            suffix,
            conversation_id=cid,
        )["status"]
        == "clarification_required"
    )
    result = ask(reply, suffix, conversation_id=cid)
    assert result["status"] == "ok"
    assert (
        result["semantic_evidence"]["request_fulfillment"]["requested_group"] == group
    )


@pytest.mark.parametrize("suffix", ["", "/stream"])
def test_named_profile_replaces_pending_request(ask, suffix):
    from uuid import uuid4

    cid = str(uuid4())
    assert (
        ask(
            "Give an overview of Example 1 among guards and wings in 2024-25.",
            suffix,
            conversation_id=cid,
        )["status"]
        == "clarification_required"
    )
    result = ask(
        "Example 2 among guards in the 2024-25 regular season",
        suffix,
        conversation_id=cid,
    )
    assert result["status"] == "ok"
    assert result["semantic_evidence"]["player_id"] == 2


@pytest.mark.parametrize("suffix", ["", "/stream"])
@pytest.mark.parametrize(
    "reply", ["What about November 2023?", "Compare with November 2023"]
)
def test_baseline_fragment_after_answer_is_not_new_current_period(ask, suffix, reply):
    from uuid import uuid4

    cid = str(uuid4())
    assert (
        ask(
            "Give an overview of Example 1 in November 2024.",
            suffix,
            conversation_id=cid,
        )["status"]
        == "ok"
    )
    assert (
        ask(
            "How was his scoring in November 2024 compared with the previous season and other guards?",
            suffix,
            conversation_id=cid,
        )["status"]
        == "clarification_required"
    )
    result = ask(reply, suffix, conversation_id=cid)
    assert result["status"] == "clarification_required"
    assert result.get("semantic_evidence") is None
