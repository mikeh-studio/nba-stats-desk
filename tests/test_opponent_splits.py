from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest
from app.agent.claims import expand_claim, finalize, validate
from app.agent.conversation import InMemoryConversationStore
from app.agent.name_matching import PlayerNameError
from app.agent.opponent_splits import (
    DEFAULT_METRICS,
    answer_opponent_splits,
    compute_opponent_splits,
    parse_opponent_splits,
    wants_opponent_splits,
)
from app.agent.player_comparison import comparison_sides
from app.agent.routes import RouteContext, select_route, semantic_shape
from app.agent.semantics import Evidence, SemanticError
from app.agent.service import StatsAgent
from scripts.ask_eval.fixtures import frozen_repository
from tests.test_semantic_migration import settings

SEASON = "2024-25"
QUESTION = "Show Avery Example's opponent splits in 2024-25"
PLAYER = {"player_id": 101, "player_name": "Avery Example", "aliases": ["Avery"]}
OTHER = {"player_id": 202, "player_name": "Avery Sample", "aliases": ["Avery"]}


@pytest.fixture
def facts():
    regular, playoffs = "Regular Season", "Playoffs"
    first, semis = "first_round", "conference_semifinals"
    row_fields = "game_id game_date season_type playoff_round opponent_abbr home_away wl pts fga fgm fg3a fg3m fta ftm plus_minus".split()
    rows = [
        {
            "season": SEASON,
            "player_id": PLAYER["player_id"],
            "player_name": PLAYER["player_name"],
            "team_abbr": "NYK",
            **dict(zip(row_fields, game, strict=True)),
            "ast": 4,
            "reb": 6,
            # Published game percentages are deliberately unusable as inputs.
            "ts_pct": 0.99,
            "fg_pct": 0.99,
            "fg3_pct": 0.99,
        }
        for game in [
            (
                "01",
                "2025-01-03",
                regular,
                None,
                "BOS",
                "HOME",
                "W",
                11,
                5,
                4,
                1,
                1,
                3,
                2,
                -3,
            ),
            (
                "02",
                "2025-01-05",
                regular,
                None,
                "BOS",
                "AWAY",
                "L",
                12,
                20,
                5,
                9,
                1,
                6,
                1,
                -17,
            ),
            (
                "03",
                "2025-01-07",
                regular,
                None,
                "MIA",
                "AWAY",
                "W",
                30,
                20,
                12,
                7,
                3,
                4,
                3,
                10,
            ),
            (
                "04",
                "2025-01-09",
                regular,
                None,
                "MIA",
                "HOME",
                "W",
                32,
                18,
                12,
                6,
                4,
                5,
                4,
                15,
            ),
            (
                "05",
                "2025-01-12",
                regular,
                None,
                "MIA",
                "HOME",
                "W",
                34,
                18,
                13,
                5,
                3,
                5,
                5,
                22,
            ),
            (
                "06",
                "2025-04-21",
                playoffs,
                first,
                "MIA",
                "HOME",
                "W",
                30,
                17,
                11,
                5,
                3,
                6,
                5,
                9,
            ),
            (
                "07",
                "2025-04-23",
                playoffs,
                first,
                "MIA",
                "AWAY",
                "L",
                17,
                21,
                7,
                5,
                2,
                2,
                1,
                -12,
            ),
            (
                "08",
                "2025-05-10",
                playoffs,
                semis,
                "BOS",
                "HOME",
                "W",
                24,
                16,
                10,
                4,
                2,
                4,
                2,
                6,
            ),
        ]
    ]
    through = {
        (SEASON, "Regular Season"): "2025-01-12",
        (SEASON, "Playoffs"): "2025-05-10",
    }
    return Evidence(
        rows, frozenset(through), "fixture", "fixture-v1", through, complete=True
    )


@pytest.mark.parametrize(
    "question",
    [
        "Which team did Avery Example struggle against most in 2024-25?",
        "Avery Example's opponent splits",
        "How did Avery Example perform against each opponent?",
        "Avery Example struggled most against which team?",
        "Show the toughest matchup for Avery Example",
        "Break down Avery Example’s scoring by opponent in the first round",
        "How many games did Avery Example play against each team?",
    ],
)
def test_opponent_route_cues(question):
    assert wants_opponent_splits(question)
    assert semantic_shape(question) == "opponent_splits"


@pytest.mark.parametrize(
    "opponents",
    [
        "every team",
        "each team",
        "all teams",
        "every opponent",
        "each opponent",
        "all opponents",
        "every NBA team",
    ],
)
def test_collective_opponent_is_not_a_second_player(opponents):
    question = f"How did Avery Example perform versus {opponents} in 2024-25?"
    assert comparison_sides(question) is None
    assert semantic_shape(question) == "opponent_splits"


@pytest.mark.parametrize(
    ("question", "route"),
    [
        ("How many points did Avery Example average against BOS?", "metrics"),
        ("How did Avery Example perform against BOS?", "overview"),
        ("Avery Example vs Blair Sample opponent splits", "player_comparison"),
        ("Compare Avery Example and Blair Sample by opponent", "player_comparison"),
        ("Avery Example vs Blair Sample versus every team", "player_comparison"),
        ("Compare Avery Example at home vs on the road by opponent", "player_split"),
        ("Compare Avery Example in the regular season vs playoffs", "player_split"),
    ],
)
def test_existing_routes_keep_priority(question, route):
    assert semantic_shape(question) == route


def test_ratios_delta_and_score_use_governed_components(facts):
    request = parse_opponent_splits(QUESTION, [PLAYER], SEASON)
    assert request.metrics == DEFAULT_METRICS
    result = compute_opponent_splits(facts, request)
    assert result["toughest_opponent"] == "BOS"
    assert result["best_opponent"] == "MIA"
    bos, mia = result["opponents"]
    assert bos["struggle_score"] > 0 > mia["struggle_score"]
    assert bos["struggle_score"] == pytest.approx(
        sum(bos["score_components"].values()) / 3
    )
    assert (bos["games"], bos["wins"], bos["losses"]) == (2, 1, 1)
    assert bos["small_sample"] and not mia["small_sample"]
    assert "fewer than three" in " ".join(result["assumptions"])
    assert [row["game_id"] for row in result["toughest_opponent_games"]] == ["02", "01"]
    assert result["baseline"]["games"] == 5
    assert bos["metrics"]["pts"]["total"] == 23
    assert bos["metrics"]["pts"]["value"] == 11.5
    assert bos["metrics"]["pts"]["difference"] == pytest.approx(11.5 - 119 / 5)
    for key, numerator, denominator in [
        ("ts_pct", 23, 2 * (25 + 0.44 * 9)),
        ("fg_pct", 9, 25),
        ("fg3_pct", 2, 10),
    ]:
        metric = bos["metrics"][key]
        assert metric["numerator"] == pytest.approx(numerator)
        assert metric["denominator"] == pytest.approx(denominator)
        assert metric["value"] == pytest.approx(numerator / denominator)
        assert metric["difference"] == pytest.approx(
            100
            * (numerator / denominator - result["baseline"]["metrics"][key]["value"])
        )
    shots = parse_opponent_splits(QUESTION + " for FT% and eFG%", [PLAYER], SEASON)
    metrics = compute_opponent_splits(facts, shots)["opponents"][0]["metrics"]
    assert metrics["ft_pct"]["value"] == pytest.approx(3 / 9)
    assert metrics["efg_pct"]["value"] == pytest.approx((9 + 0.5 * 2) / 25)


def test_equivalent_opponent_stat_lines_have_no_distinct_score_extremes(facts):
    # Unequal sample sizes give the same pooled stat lines, with float noise
    # from summing the shooting denominator over two versus ten appearances.
    rows = [
        {**row, "game_id": f"{i:02}", "opponent_abbr": "BOS" if i < 2 else "MIA"}
        for i, row in enumerate(facts.rows[:2] * 6)
    ]
    request = parse_opponent_splits(QUESTION, [PLAYER], SEASON)
    result = compute_opponent_splits(replace(facts, rows=rows), request)
    first, second = result["opponents"]
    assert first["struggle_score"] == pytest.approx(second["struggle_score"])
    assert result["toughest_opponent"] is None
    assert result["best_opponent"] is None
    assert result["toughest_opponent_games"] == []


@pytest.mark.parametrize(
    ("scope", "matches"),
    [
        (" first round at home", ["06"]),
        (" first round home games", ["06"]),
        (" playoffs", ["06", "07", "08"]),
        (" from 2025-01-04 through 2025-01-09 on the road", ["02", "03"]),
        (" last 2 games on the road", ["02", "03"]),
        (" last 2 home games", ["04", "05"]),
    ],
)
def test_scope_filters_before_grouping(facts, scope, matches):
    request = parse_opponent_splits(QUESTION + scope, [PLAYER], SEASON)
    evidence = compute_opponent_splits(facts, request)
    assert sorted(evidence["baseline"]["metrics"]["pts"]["game_ids"]) == matches
    if len(matches) == 1:
        assert evidence["toughest_opponent"] is None
        assert evidence["toughest_opponent_games"] == []


def test_unknown_outcomes_and_missing_shooting_components(facts):
    rows = deepcopy(facts.rows)
    rows[0].update(wl=None, game_result=None, fgm=None)
    rows[1].update(fg3a=0, fg3m=0)
    request = parse_opponent_splits(QUESTION, [PLAYER], SEASON)
    bos = compute_opponent_splits(replace(facts, rows=rows), request)["opponents"][0]
    assert (bos["wins"], bos["losses"], bos["unknown_outcomes"]) == (0, 1, 1)
    assert bos["metrics"]["fg_pct"]["valid_games"] == 1
    assert bos["metrics"]["fg_pct"]["value"] == pytest.approx(5 / 20)
    assert bos["metrics"]["fg3_pct"]["value"] == 1.0


def test_uppercase_player_alias_and_metric_are_not_team_filters(facts):
    player = {**PLAYER, "aliases": ["AE"]}
    result = answer_opponent_splits(
        "Show the toughest matchup for AE in 2024-25", facts, [player], SEASON
    )
    assert result["opponent_splits"]["player"]["player_id"] == PLAYER["player_id"]
    request = parse_opponent_splits(QUESTION + " for PTS", [player], SEASON)
    assert request.metrics == ("pts",)
    with pytest.raises(SemanticError, match="single-opponent summary"):
        parse_opponent_splits(QUESTION + " against BOS", [player], SEASON)


def test_ambiguous_unknown_and_empty_scope(facts):
    with pytest.raises(PlayerNameError) as exc:
        parse_opponent_splits("Show Avery's opponent splits", [PLAYER, OTHER], SEASON)
    assert exc.value.result["status"] == "clarification_required"
    assert {p["player_id"] for p in exc.value.result["matches"]} == {101, 202}
    with pytest.raises((SemanticError, PlayerNameError), match="player|match"):
        parse_opponent_splits(
            "How did Zyxwv Qzxyn do against every opponent?", [PLAYER], SEASON
        )
    with pytest.raises(SemanticError, match="No verified player appearances") as exc:
        answer_opponent_splits(
            QUESTION + " from 2025-02-02 through 2025-02-04", facts, [PLAYER], SEASON
        )
    assert exc.value.code == "unsupported_coverage"


def test_strict_claims_cover_prose_record_delta_and_drilldown(facts):
    payload = answer_opponent_splits(QUESTION, facts, [PLAYER], SEASON)
    report = validate(payload)
    assert report["status"] == "complete", report
    assert payload["tables"][0]["rows"][0][:3] == ["BOS", 2, "1–1"]
    for table, row, column in [(0, 0, 2), (0, 0, 5), (1, 0, 2), (2, 0, 4)]:
        changed = deepcopy(payload)
        changed["tables"][table]["rows"][row][column] = "999"
        assert (
            finalize(changed, mode="enforce")["reason_code"] == "claim_binding_mismatch"
        )
    changed = deepcopy(payload)
    changed["answer"] += " Arbitrary assertion."
    assert finalize(changed, mode="enforce")["status"] == "error"
    changed = deepcopy(payload)
    changed["claim_bindings"]["claims"].pop()
    assert finalize(changed, mode="enforce")["status"] == "error"


def test_claim_validation_rejects_deleted_opponent_row_and_bindings(facts):
    payload = answer_opponent_splits(QUESTION, facts, [PLAYER], SEASON)
    row = len(payload["tables"][0]["rows"]) - 1
    del payload["tables"][0]["rows"][row]
    manifest = payload["claim_bindings"]
    manifest["claims"] = [
        claim
        for claim in manifest["claims"]
        if expand_claim(manifest, claim)["destination"][:4]
        != ["tables", 0, "rows", row]
    ]
    report = validate(payload)
    assert report["status"] == "invalid", report
    assert report["unbound_count"] == 0
    assert any(
        "Missing required opponent split" in e["reason"] for e in report["errors"]
    )


@pytest.fixture
def split_agent(facts, monkeypatch):
    config = replace(settings(), ask_claim_enforcement="enforce")
    repository = frozen_repository(config, {"rows": facts.rows}, "complete")
    agent = StatsAgent(
        config, repository, conversation_store=InMemoryConversationStore()
    )

    def unexpected(*args, **kwargs):
        pytest.fail("Opponent splits must use the governed game rows directly")

    monkeypatch.setattr(agent, "_get_client", unexpected)
    monkeypatch.setattr("app.agent.service.answer_legacy_repository", unexpected)
    return agent


def test_stats_agent_answers_collective_versus_phrase(split_agent):
    result = split_agent.answer(
        "How did Avery Example perform versus every team in 2024-25?"
    )
    assert result["status"] == "ok", result
    assert result["ask_route"]["route"] == "opponent_splits"
    assert result["claim_validation"]["status"] == "complete"


@pytest.mark.parametrize(
    ("constraint", "message"),
    [
        ("compared to the league average", "league-average comparison"),
        ("vs league average", "league-average comparison"),
        ("relative to league", "league-average comparison"),
        ("against the Celtics", "single-opponent summary"),
        ("in the first quarter", "whole games only"),
        ("in the 4th quarter", "whole games only"),
        ("in the second half", "whole games only"),
        ("in the last three games", "numeric last-N-games window"),
        ("in the last 2 games or the last three games", "numeric last-N-games window"),
    ],
)
def test_stats_agent_withholds_unconsumed_opponent_split_constraints(
    split_agent, constraint, message
):
    question = f"Show Avery Example's points by opponent in 2024-25 {constraint}"
    result = split_agent.answer(question)
    assert result["status"] == "unsupported_scope", result
    assert result["ask_route"]["route"] == "opponent_splits"
    assert message in result["answer"]
    assert not result.get("opponent_splits")
    assert not result["tables"]


def test_stats_agent_follows_up_without_model_or_legacy(split_agent):
    agent = split_agent
    result = agent.answer(QUESTION + " on the road", conversation_id="splits")
    assert result["status"] == "ok", result
    assert result["claim_validation"]["status"] == "complete"
    assert result["ask_route"] == {
        "route": "opponent_splits",
        "source": "deterministic",
        "reason_code": None,
    }
    reply = agent.answer("What about FT%?", conversation_id="splits")
    assert reply["status"] == "ok", reply
    assert reply["ask_route"]["source"] == "prior_route"
    assert reply["opponent_splits"]["scope"] == result["opponent_splits"]["scope"]
    assert reply["opponent_splits"]["display_metrics"] == ["ft_pct"]
    assert reply["claim_validation"]["status"] == "complete"


def test_without_governed_evidence_refuses_before_legacy(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Opponent splits must never call legacy")

    monkeypatch.setattr("app.agent.service.answer_legacy_repository", unexpected)
    agent = StatsAgent(settings(), SimpleNamespace())
    response = agent.answer(QUESTION)
    assert response["status"] == "unsupported_coverage"
    assert response["ask_route"]["route"] == "opponent_splits"


def test_round_route_precedes_generic_round_handler():
    question = QUESTION + " in the first round"
    context = RouteContext(question, question, {}, {}, SimpleNamespace(), True)
    assert select_route(context).key == "opponent_splits"


def test_pinned_publication_provides_game_rows_and_checks_explicit_date_bounds():
    from app.repository import BigQueryWarehouseRepository
    from app.seasons import DEFAULT_SEASON
    from tests.test_production_ask_source import ACTIVE, EARLY, FIRST, LAST, source
    from tests.test_semantic_migration import Warehouse

    publication = source(ACTIVE)
    cfg = replace(settings(DEFAULT_SEASON), ask_claim_enforcement="enforce")
    facts = Warehouse()
    repository = BigQueryWarehouseRepository(cfg, client=SimpleNamespace())
    repository._governed_warehouse = facts
    repository.ask_publication_source = lambda publication_id: publication
    agent = StatsAgent(cfg, repository)

    question = "Show Taylor Example's opponent splits"
    payload = agent.answer(question)
    assert payload["status"] == "ok", payload["answer"]
    assert payload["claim_validation"]["status"] == "complete"
    assert payload["publication_id"] == ACTIVE
    assert payload["opponent_splits"]["scope"]["first_game_date"] == EARLY
    assert payload["opponent_splits"]["scope"]["last_game_date"] == LAST
    assert payload["opponent_splits"]["opponents"][0]["unknown_outcomes"] == 2

    refused = agent.answer(
        question + f" from {DEFAULT_SEASON[:4]}-10-01 through {FIRST}"
    )
    assert refused["status"] == "unsupported_coverage"
    assert facts.calls == []
