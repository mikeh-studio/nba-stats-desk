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
    opponent_splits_followup,
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


def test_programmatic_alias_preserves_opponent_split_identity_and_claims(
    facts, monkeypatch
):
    name = "Alperen Şengün"
    alias = "Sengun"
    rows = [{**row, "player_name": name} for row in facts.rows]
    agent = split_agent.__wrapped__(replace(facts, rows=rows), monkeypatch)
    result = agent.answer(
        f"Show the toughest matchup for {alias} in 2024-25", conversation_id="alias"
    )
    assert result["status"] == "ok", result
    assert result["ask_route"]["route"] == "opponent_splits"
    assert result["opponent_splits"]["player"]["player_name"] == name
    assert result["claim_validation"]["status"] == "complete"
    reply = agent.answer("What about 3PA?", conversation_id="alias")
    assert reply["status"] == "ok", reply
    assert reply["opponent_splits"]["player"] == result["opponent_splits"]["player"]
    assert reply["opponent_splits"]["display_metrics"] == ["fg3a"]
    assert reply["claim_validation"]["status"] == "complete"


def test_stats_agent_answers_home_filter_before_collective_versus_phrase(
    facts, split_agent
):
    question = "How did Avery Example perform at home versus every team in 2024-25?"
    request = parse_opponent_splits(question, [PLAYER], SEASON)
    assert request.query.home_away == "HOME"
    evidence = compute_opponent_splits(facts, request)
    assert sorted(evidence["baseline"]["metrics"]["pts"]["game_ids"]) == [
        "01",
        "04",
        "05",
    ]
    result = split_agent.answer(question)
    assert result["status"] == "ok", result
    assert result["ask_route"]["route"] == "opponent_splits"
    assert result["opponent_splits"]["scope"]["home_away"] == "HOME"
    assert result["claim_validation"]["status"] == "complete"


@pytest.mark.parametrize(
    ("constraint", "message"),
    [
        ("compared to the league average", "league-average comparison"),
        ("vs league average", "league-average comparison"),
        ("relative to league", "league-average comparison"),
        ("against the Celtics", "single-opponent summary"),
        ("in the first quarter", "whole-game box scores"),
        ("in the 4th quarter", "whole-game box scores"),
        ("in the second half", "whole-game box scores"),
        ("in the last three games", "numeric last-N-games window"),
        ("in the last 2 games or the last three games", "numeric last-N-games window"),
        ("in non-home games", "affirmatively"),
        ("in non-playoff games", "affirmatively"),
        ("through the trade deadline", "explicit date range"),
        ("above 20 points", "stat thresholds"),
        ("with a +/- above zero", "stat thresholds"),
        ("only opponents with a winning record", "cannot filter opponents"),
        ("on a winning streak", "game outcomes"),
        ("for the Knicks", "teams and arenas"),
        ("as a Knick", "player-role filters"),
        ("at Madison Square Garden", "teams and arenas"),
        ("limited to BOS and MIA", "teams and arenas"),
        ("in games decided by three points or fewer", "additional game"),
        ("in tournament games", "competition"),
        ("as a rookie", "player-role filters"),
    ],
)
def test_stats_agent_withholds_unconsumed_opponent_split_constraints(
    split_agent, constraint, message
):
    question = f"Show Avery Example's points by opponent in 2024-25 {constraint}"
    result = split_agent.answer(question)
    assert result["status"] == "unsupported_scope", result
    assert result["ask_route"]["route"] == "opponent_splits"
    assert message.casefold() in result["answer"].casefold()
    assert not result.get("opponent_splits")
    assert not result["tables"]


@pytest.mark.parametrize(
    "metric",
    [
        "two-point field goal percentage",
        "2PT% and 3PT%",
        "FG% and 2FG%",
        "season-high points",
        "peak points",
        "rebounds and assists per 36",
    ],
)
def test_refuses_incompletely_supported_metric_requests(split_agent, metric):
    result = split_agent.answer(f"Avery Example's {metric} by opponent in 2024-25")
    assert result["status"] == "unsupported_scope", result
    assert result["ask_route"]["route"] == "opponent_splits"
    assert not result["tables"]


@pytest.mark.parametrize(
    ("question", "followup"),
    [
        ("Show Avery Example's fast break points by opponent", "Fast Break Points"),
        (
            "Show Avery Example's points scored on fast breaks against each opponent",
            "points scored on fast breaks",
        ),
        (
            "How many points did Avery Example score in the paint against each team",
            "points in the paint",
        ),
        ("Show Avery Example's paint points by opponent", "paint points"),
        (
            "Show Avery Example's points off turnovers by opponent",
            "points off turnovers",
        ),
        (
            "Show Avery Example's points scored from turnovers by opponent",
            "points scored from turnovers",
        ),
        (
            "Show Avery Example's rebounds and corner three percentage by opponent",
            "rebounds and corner three percentage",
        ),
        (
            "How did Avery Example shoot threes from the corners against every opponent",
            "three-point percentage from the corners",
        ),
    ],
)
def test_validates_complete_metric_expression_on_initial_and_followup(
    split_agent, question, followup
):
    initial = split_agent.answer(f"{question} in 2024-25?")
    assert (
        split_agent.answer(QUESTION, conversation_id="metric-scope")["status"] == "ok"
    )
    reply = split_agent.answer(
        f"What about {followup}?", conversation_id="metric-scope"
    )
    assert reply["ask_route"]["source"] == "prior_route"
    for result in (initial, reply):
        assert result["status"] == "unsupported_scope", result
        assert result["ask_route"]["route"] == "opponent_splits"
        assert not result.get("opponent_splits")
        assert not result["tables"]


@pytest.mark.parametrize(
    ("question", "keys"),
    [
        ("How did Avery Example fare against each team", list(DEFAULT_METRICS)),
        (
            "How well did Avery Example play against every opponent",
            list(DEFAULT_METRICS),
        ),
        ("Show the toughest matchup for Avery Example", list(DEFAULT_METRICS)),
        ("How many games did Avery Example play against each team", ["gp"]),
        ("Show Avery Example's number of appearances by opponent", ["gp"]),
        ("How many points did Avery Example average against each team", ["pts"]),
    ],
)
def test_generic_question_defaults_and_explicit_metrics_still_resolve(
    split_agent, question, keys
):
    result = split_agent.answer(f"{question} in 2024-25?")
    assert result["status"] == "ok", result
    assert result["opponent_splits"]["display_metrics"] == keys


def test_an_unqualified_point_count_still_requires_aggregation(split_agent):
    result = split_agent.answer(
        "How many points did Avery Example score against each team in 2024-25?"
    )
    assert result["status"] == "clarification_required", result
    assert result["ask_route"]["route"] == "opponent_splits"
    assert "total points or points per game" in result["answer"]


@pytest.mark.parametrize(
    ("metric", "keys"),
    [
        ("games played", ["gp"]),
        ("3-point shots attempted", ["fg3a"]),
        ("free throw shooting %", ["ft_pct"]),
        ("FT %", ["ft_pct"]),
        ("points and assists per 36", ["pts_per36", "ast_per36"]),
        ("points per game and assists per 36", ["pts", "ast_per36"]),
    ],
)
def test_preserves_compound_metric_meaning(split_agent, metric, keys):
    result = split_agent.answer(f"Avery Example's {metric} by opponent in 2024-25")
    assert result["status"] == "ok", result
    assert result["opponent_splits"]["display_metrics"] == keys
    assert result["claim_validation"]["status"] == "complete"


def test_discloses_short_last_n_sample(split_agent):
    result = split_agent.answer(f"{QUESTION} in the last 10 games")
    assert result["status"] == "ok", result
    assert result["opponent_splits"]["baseline"]["games"] == 5
    assert "fewer than the requested 10 appearances" in result["answer"]
    assert result["claim_validation"]["status"] == "complete"


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


def test_ambiguous_player_selection_preserves_canonical_name_for_followup(
    facts, monkeypatch
):
    direct = answer_opponent_splits(
        "Show Avery's opponent splits", facts, [PLAYER], SEASON
    )
    stored_question = direct["conversation_context"]["opponent_splits_question"]
    assert "Avery Example" in stored_question
    followup_query = opponent_splits_followup("What about FT%?", stored_question)
    assert followup_query is not None
    parsed_followup = parse_opponent_splits(followup_query, [PLAYER, OTHER], SEASON)
    assert parsed_followup.player["player_id"] == PLAYER["player_id"]
    assert parsed_followup.metrics == ("ft_pct",)

    other_rows = [
        {
            **row,
            "game_id": f"other-{row['game_id']}",
            "player_id": OTHER["player_id"],
            "player_name": OTHER["player_name"],
            "team_abbr": "MIA",
        }
        for row in facts.rows[:2]
    ]
    config = replace(settings(), ask_claim_enforcement="enforce")
    repository = frozen_repository(
        config, {"rows": [*facts.rows, *other_rows]}, "complete"
    )
    agent = StatsAgent(
        config, repository, conversation_store=InMemoryConversationStore()
    )

    def unexpected(*args, **kwargs):
        pytest.fail("Opponent splits must use the governed game rows directly")

    monkeypatch.setattr(agent, "_get_client", unexpected)
    monkeypatch.setattr("app.agent.service.answer_legacy_repository", unexpected)

    clarify = agent.answer("Show Avery's opponent splits", conversation_id="ambiguous")
    assert clarify["status"] == "clarification_required", clarify
    assert {opt["player_id"] for opt in clarify["clarification_options"]} == {
        PLAYER["player_id"],
        OTHER["player_id"],
    }
    selected = agent.answer("Avery Example", conversation_id="ambiguous")
    assert selected["status"] == "ok", selected
    assert selected["opponent_splits"]["player"]["player_id"] == PLAYER["player_id"]
    assert (
        "Avery Example" in selected["conversation_context"]["opponent_splits_question"]
    )
    assert selected["claim_validation"]["status"] == "complete"

    followup = agent.answer("What about FT%?", conversation_id="ambiguous")
    assert followup["status"] == "ok", followup
    assert followup["ask_route"]["source"] == "prior_route"
    assert followup["opponent_splits"]["player"]["player_id"] == PLAYER["player_id"]
    assert followup["opponent_splits"]["display_metrics"] == ["ft_pct"]
    assert followup["claim_validation"]["status"] == "complete"


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


@pytest.mark.parametrize(
    "clause",
    [
        "in games with 20+ points",
        "with 20+ pts",
        "in 20-point games",
        "against every team in the Western Conference",
        "against every team in the Eastern Conference",
        "against Atlantic Division teams",
        "against playoff teams",
        "in wins",
        "in losses",
        "in winning games",
        "on back-to-backs",
        "on zero rest",
        "with 2 days rest",
        "as a starter",
        "off the bench",
        "in overtime",
    ],
)
def test_unsupported_game_and_opponent_filters_rejected_before_computing(
    split_agent, clause
):
    question = f"How did Avery Example perform against every team {clause} in 2024-25?"
    result = split_agent.answer(question)
    assert result["status"] == "unsupported_scope", (clause, result)
    assert result["ask_route"]["route"] == "opponent_splits"
    assert not result.get("opponent_splits")
    assert not result["tables"]


def test_conference_playoff_round_still_succeeds(split_agent):
    result = split_agent.answer(
        "Break down Avery Example's scoring by opponent in the Conference Finals in 2024-25"
    )
    assert result["status"] in {"ok", "unsupported_coverage"}, result
    assert result["ask_route"]["route"] == "opponent_splits"

    qualified = split_agent.answer(
        "Break down Avery Example's scoring by opponent in the Western Conference Finals in 2024-25"
    )
    assert qualified["status"] == "unsupported_scope", qualified
    assert qualified["ask_route"]["reason_code"] == "unsupported_round"


def test_last_n_appearance_window_preserves_venue(facts, split_agent):
    away = parse_opponent_splits(
        "Show Avery Example's opponent splits in his last 2 away appearances in 2024-25",
        [PLAYER],
        SEASON,
    )
    assert away.query.window == "last_n_games"
    assert away.query.n == 2
    assert away.query.home_away == "AWAY"
    away_ids = sorted(
        compute_opponent_splits(facts, away)["baseline"]["metrics"]["pts"]["game_ids"]
    )
    assert away_ids == ["02", "03"]

    home = parse_opponent_splits(
        "Show Avery Example's opponent splits in his last 2 home appearances in 2024-25",
        [PLAYER],
        SEASON,
    )
    assert home.query.home_away == "HOME"
    home_ids = sorted(
        compute_opponent_splits(facts, home)["baseline"]["metrics"]["pts"]["game_ids"]
    )
    assert home_ids == ["04", "05"]

    conflict = split_agent.answer(
        "Show Avery Example's opponent splits at home in his last 2 away appearances in 2024-25"
    )
    assert conflict["status"] == "unsupported_scope"


@pytest.mark.parametrize(
    "calendar_clause",
    [
        "in February and March of 2024-25",
        "from January through March 2025",
        "in 2024-25 as of 2025-01-10 and as of 2025-01-12",
        "in 2024-25 past 5 days",
        "in 2024-25 last 5 days and last 10 days",
    ],
)
def test_unresolved_or_conflicting_calendar_constraints_do_not_fall_through(
    split_agent, calendar_clause
):
    result = split_agent.answer(
        f"Show Avery Example's opponent splits {calendar_clause}"
    )
    assert result["status"] == "clarification_required", (calendar_clause, result)
    assert not result.get("opponent_splits")


def test_followup_inherits_total_aggregation_unless_overridden(split_agent):
    first = split_agent.answer(
        "Show Avery Example's total points by opponent in 2024-25",
        conversation_id="totals",
    )
    assert first["status"] == "ok", first
    assert first["opponent_splits"]["aggregation"] == "total"

    inherited = split_agent.answer("What about assists?", conversation_id="totals")
    assert inherited["status"] == "ok", inherited
    assert inherited["opponent_splits"]["aggregation"] == "total"
    assert inherited["opponent_splits"]["display_metrics"] == ["ast"]

    overridden = split_agent.answer(
        "What about rebounds per game?", conversation_id="totals"
    )
    assert overridden["status"] == "ok", overridden
    assert overridden["opponent_splits"]["aggregation"] == "average"
    assert overridden["opponent_splits"]["display_metrics"] == ["reb"]

    still_avg = split_agent.answer("What about steals?", conversation_id="totals")
    assert still_avg["status"] == "ok", still_avg
    assert still_avg["opponent_splits"]["aggregation"] == "average"


def test_identical_full_name_disambiguation_survives_to_followup(facts, monkeypatch):
    twin = {"player_id": 202, "player_name": "Avery Example", "aliases": ["Avery"]}
    other_rows = [
        {
            **row,
            "game_id": f"twin-{row['game_id']}",
            "player_id": twin["player_id"],
            "player_name": twin["player_name"],
            "team_abbr": "MIA",
        }
        for row in facts.rows[:2]
    ]
    config = replace(settings(), ask_claim_enforcement="enforce")
    repository = frozen_repository(
        config, {"rows": [*facts.rows, *other_rows]}, "complete"
    )
    agent = StatsAgent(
        config, repository, conversation_store=InMemoryConversationStore()
    )

    def unexpected(*args, **kwargs):
        pytest.fail("Opponent splits must use the governed game rows directly")

    monkeypatch.setattr(agent, "_get_client", unexpected)
    monkeypatch.setattr("app.agent.service.answer_legacy_repository", unexpected)

    clarify = agent.answer(
        "Show Avery Example's opponent splits in 2024-25", conversation_id="twins"
    )
    assert clarify["status"] == "clarification_required", clarify
    assert {opt["player_id"] for opt in clarify["clarification_options"]} == {101, 202}

    selected = agent.answer(
        "Show Avery Example's opponent splits in 2024-25",
        conversation_id="twins",
        selected_player={"player_id": 101, "player_name": "Avery Example"},
    )
    assert selected["status"] == "ok", selected
    assert selected["opponent_splits"]["player"]["player_id"] == 101

    followup = agent.answer("What about FT%?", conversation_id="twins")
    assert followup["status"] == "ok", followup
    assert followup["ask_route"]["route"] == "opponent_splits"
    assert followup["ask_route"]["source"] == "prior_route"
    assert followup["opponent_splits"]["player"]["player_id"] == 101
    assert followup["opponent_splits"]["display_metrics"] == ["ft_pct"]
    assert followup["claim_validation"]["status"] == "complete"


def test_browser_only_recovery_restores_opponent_splits_and_revalidates(facts):
    from app import main as main_module
    from app.main import app, get_agent_client, get_repository, get_settings
    from app.repository import BigQueryWarehouseRepository
    from fastapi.testclient import TestClient

    twin_rows = [
        {
            **row,
            "game_id": f"twin-{row['game_id']}",
            "player_id": 202,
            "player_name": "Avery Example",
            "team_abbr": "MIA",
        }
        for row in facts.rows[:2]
    ]
    cfg = replace(
        settings(SEASON), ask_claim_enforcement="enforce", agent_history_enabled=False
    )
    repo = BigQueryWarehouseRepository(cfg, client=SimpleNamespace())
    repo._governed_warehouse = SimpleNamespace(
        load=lambda seasons: (
            {"capture": {}},
            replace(facts, rows=[*facts.rows, *twin_rows]),
        )
    )
    old = dict(app.dependency_overrides)
    app.dependency_overrides.update(
        {
            get_settings: lambda: cfg,
            get_repository: lambda: repo,
            get_agent_client: lambda: object(),
        }
    )
    main_module._season_conversation_store(SEASON)._conversations.clear()
    try:
        with TestClient(app) as client:
            first = client.post(
                f"/api/agent/ask?season={SEASON}",
                json={
                    "question": "Show Avery Example's total points by opponent in his last 2 away appearances in 2024-25",
                    "selected_player_id": 101,
                    "selected_player_name": "Avery Example",
                },
            )
            assert first.status_code == 200
            first_payload = first.json()
            assert first_payload["status"] == "ok", first_payload
            prior = first_payload["conversation_context"]
            assert prior["analysis_type"] == "opponent_splits"
            assert prior["players"] == [
                {"player_id": 101, "player_name": "Avery Example"}
            ]
            assert "opponent_splits_question" in prior

            # Simulate server restart / empty conversation store with browser-only recovery.
            main_module._season_conversation_store(SEASON)._conversations.clear()
            recovered = client.post(
                f"/api/agent/ask?season={SEASON}",
                json={
                    "question": "What about FT%?",
                    "conversation_id": "browser-only-splits",
                    "previous_context": prior,
                },
            )
            assert recovered.status_code == 200
            rec_payload = recovered.json()
            assert rec_payload["status"] == "ok", rec_payload
            assert rec_payload["ask_route"]["route"] == "opponent_splits"
            assert rec_payload["ask_route"]["source"] == "prior_route"
            assert rec_payload["opponent_splits"]["player"]["player_id"] == 101
            assert rec_payload["opponent_splits"]["display_metrics"] == ["ft_pct"]
            assert rec_payload["opponent_splits"]["scope"]["home_away"] == "AWAY"
            assert rec_payload["opponent_splits"]["scope"]["window"] == "last_n_games"
            assert rec_payload["opponent_splits"]["scope"]["n"] == 2
            assert rec_payload["claim_validation"]["status"] == "complete"

            # Tampered scope or mismatched player fails revalidation on recovery.
            bad_scope = deepcopy(prior)
            bad_scope["opponent_splits_question"] = (
                "How did Avery Example do in 2024-25?"
            )
            bad_resp = client.post(
                f"/api/agent/ask?season={SEASON}",
                json={
                    "question": "What about FT%?",
                    "conversation_id": "browser-only-bad-scope",
                    "previous_context": bad_scope,
                },
            )
            assert bad_resp.status_code == 400

            missing_player = deepcopy(prior)
            missing_player["players"] = [
                {"player_id": 999, "player_name": "Avery Example"}
            ]
            missing_resp = client.post(
                f"/api/agent/ask?season={SEASON}",
                json={
                    "question": "What about FT%?",
                    "conversation_id": "browser-only-missing-player",
                    "previous_context": missing_player,
                },
            )
            assert missing_resp.status_code == 200
            assert missing_resp.json()["status"] == "unsupported_coverage"
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(old)


@pytest.mark.parametrize(
    "clause",
    [
        "on weekends",
        "on weekdays",
        "in weekday games",
        "in games he started",
        "in games she started",
        "in games they started",
        "when he started",
        "in starts",
        "off the bench",
        "in Monday games",
    ],
)
def test_unconsumed_game_selection_clauses_return_unsupported_scope(
    split_agent, clause
):
    question = f"Show Avery Example's opponent splits {clause} in 2024-25"
    result = split_agent.answer(question)
    assert result["status"] == "unsupported_scope", (clause, result)
    assert result["ask_route"]["route"] == "opponent_splits"
    assert not result.get("opponent_splits")


@pytest.mark.parametrize(
    "question",
    [
        "Show Avery Example's maximum points by opponent in 2024-25",
        "Show Avery Example's max points by opponent in 2024-25",
        "Show Avery Example's highest points by opponent in 2024-25",
        "Show Avery Example's minimum points by opponent in 2024-25",
        "Show Avery Example's min points by opponent in 2024-25",
        "Show Avery Example's lowest assists by opponent in 2024-25",
        "Show Avery Example's median points by opponent in 2024-25",
        "Show Avery Example's std points by opponent in 2024-25",
        "Show Avery Example's assists and usage rate by opponent in 2024-25",
        "Show Avery Example's usage rate by opponent in 2024-25",
        "Show Avery Example's USG% by opponent in 2024-25",
        "Show Avery Example's PER by opponent in 2024-25",
        "Show Avery Example's BPM by opponent in 2024-25",
        "Show Avery Example's VORP by opponent in 2024-25",
        "Show Avery Example's win shares by opponent in 2024-25",
        "Show Avery Example's WS by opponent in 2024-25",
        "Show Avery Example's net rating by opponent in 2024-25",
        "Show Avery Example's rebounds per 36 minutes by opponent in 2024-25",
        "Show Avery Example's AST% by opponent in 2024-25",
    ],
)
def test_unsupported_aggregations_and_metrics_return_unsupported_scope(
    split_agent, question
):
    result = split_agent.answer(question)
    assert result["status"] == "unsupported_scope", (question, result)
    assert result["ask_route"]["route"] == "opponent_splits"
    assert not result.get("opponent_splits")


def test_unsupported_metric_or_aggregation_followup_stays_refused(split_agent):
    first = split_agent.answer(QUESTION, conversation_id="unsup-followup")
    assert first["status"] == "ok"
    for followup in (
        "What about usage rate?",
        "What about assists and usage rate?",
        "What about max points?",
    ):
        reply = split_agent.answer(followup, conversation_id="unsup-followup")
        assert reply["status"] == "unsupported_scope", (followup, reply)
        assert reply["ask_route"]["route"] == "opponent_splits"


@pytest.mark.parametrize(
    "calendar_clause",
    [
        "in the 2025 playoffs or 2024 playoffs",
        "in 2024-25 or 2023-24",
        "from 2025-01-03 through 2025-01-10 and last week",
        "from 2025-01-03 through 2025-01-10 or last week",
        "from 2025-01-03 through 2025-01-10 in January 2025",
        "in 2024-25 last 2 games or last week",
    ],
)
def test_competing_season_or_window_expressions_require_clarification(
    split_agent, calendar_clause
):
    result = split_agent.answer(
        f"Show Avery Example's opponent splits {calendar_clause}"
    )
    assert result["status"] == "clarification_required", (calendar_clause, result)
    assert not result.get("opponent_splits")


def test_per_game_metric_alias_followup_overrides_total_aggregation(facts, monkeypatch):
    rows = [{**row, "min": 30} for row in facts.rows]
    config = replace(settings(), ask_claim_enforcement="enforce")
    repository = frozen_repository(config, {"rows": rows}, "complete")
    agent = StatsAgent(
        config, repository, conversation_store=InMemoryConversationStore()
    )
    monkeypatch.setattr(agent, "_get_client", lambda *a, **k: pytest.fail("No model"))
    monkeypatch.setattr(
        "app.agent.service.answer_legacy_repository",
        lambda *a, **k: pytest.fail("No legacy"),
    )

    first = agent.answer(
        "Show Avery Example's total points by opponent in 2024-25",
        conversation_id="mpg-override",
    )
    assert first["status"] == "ok"
    assert first["opponent_splits"]["aggregation"] == "total"

    mpg = agent.answer("What about MPG?", conversation_id="mpg-override")
    assert mpg["status"] == "ok", mpg
    assert mpg["opponent_splits"]["aggregation"] == "average"
    assert mpg["opponent_splits"]["display_metrics"] == ["min"]

    still_avg = agent.answer("What about steals?", conversation_id="mpg-override")
    assert still_avg["status"] == "ok", still_avg
    assert still_avg["opponent_splits"]["aggregation"] == "average"


def test_catalog_alias_longest_match_and_followup_conjunctions(facts, monkeypatch):
    rows = [{**row, "min": 30} for row in facts.rows]
    config = replace(settings(), ask_claim_enforcement="enforce")
    repository = frozen_repository(config, {"rows": rows}, "complete")
    agent = StatsAgent(
        config, repository, conversation_store=InMemoryConversationStore()
    )
    monkeypatch.setattr(agent, "_get_client", lambda *a, **k: pytest.fail("No model"))
    monkeypatch.setattr(
        "app.agent.service.answer_legacy_repository",
        lambda *a, **k: pytest.fail("No legacy"),
    )

    per36 = agent.answer(
        "Show Avery Example's PTS per 36 minutes by opponent in 2024-25",
        conversation_id="catalog-aliases",
    )
    assert per36["status"] == "ok", per36
    assert per36["opponent_splits"]["display_metrics"] == ["pts_per36"]

    multi = agent.answer("What about 3PA and PTS?", conversation_id="catalog-aliases")
    assert multi["status"] == "ok", multi
    assert multi["ask_route"]["route"] == "opponent_splits"
    assert multi["ask_route"]["source"] == "prior_route"
    assert multi["opponent_splits"]["display_metrics"] == ["pts", "fg3a"]


def test_last_n_games_followup_preserves_resolved_as_of_when_new_games_arrive(
    facts, monkeypatch
):
    from app.repository import BigQueryWarehouseRepository

    current_rows = list(facts.rows)
    config = replace(settings(), ask_claim_enforcement="enforce")

    def load_evidence(seasons):
        through = {
            (SEASON, "Regular Season"): max(
                r["game_date"]
                for r in current_rows
                if r["season_type"] == "Regular Season"
            ),
            (SEASON, "Playoffs"): "2025-05-10",
        }
        return (
            {"capture": {}},
            Evidence(
                list(current_rows),
                frozenset(through),
                "fixture",
                "fixture-v1",
                through,
                complete=True,
            ),
        )

    repository = BigQueryWarehouseRepository(config, client=SimpleNamespace())
    repository._governed_warehouse = SimpleNamespace(
        load=load_evidence,
        load_for_request=lambda seasons, *, rich=False: load_evidence(seasons),
    )

    agent = StatsAgent(
        config, repository, conversation_store=InMemoryConversationStore()
    )
    monkeypatch.setattr(agent, "_get_client", lambda *a, **k: pytest.fail("No model"))
    monkeypatch.setattr(
        "app.agent.service.answer_legacy_repository",
        lambda *a, **k: pytest.fail("No legacy"),
    )

    first = agent.answer(
        "Show Avery Example's opponent splits in his last 2 games in 2024-25",
        conversation_id="last2-asof",
    )
    assert first["status"] == "ok", first
    assert first["opponent_splits"]["scope"]["as_of"] == "2025-01-12"
    assert first["conversation_context"]["scope"]["as_of"] == "2025-01-12"
    original_game_ids = sorted(
        first["opponent_splits"]["baseline"]["metrics"]["pts"]["game_ids"]
    )
    assert original_game_ids == ["04", "05"]

    # Simulate a newer game arriving after the first turn.
    current_rows.append(
        {
            **facts.rows[0],
            "game_id": "09",
            "game_date": "2025-01-20",
            "opponent_abbr": "CHI",
            "pts": 40,
        }
    )

    followup = agent.answer("What about FT%?", conversation_id="last2-asof")
    assert followup["status"] == "ok", followup
    assert followup["opponent_splits"]["scope"]["as_of"] == "2025-01-12"
    assert (
        sorted(followup["opponent_splits"]["baseline"]["metrics"]["ft_pct"]["game_ids"])
        == original_game_ids
    )


def test_browser_recovery_requires_exact_player_id_agreement(facts):
    from app import main as main_module
    from app.main import app, get_agent_client, get_repository, get_settings
    from app.repository import BigQueryWarehouseRepository
    from fastapi.testclient import TestClient

    jr_rows = [
        {
            **row,
            "game_id": f"jr-{row['game_id']}",
            "player_id": 202,
            "player_name": "Avery Example Jr.",
            "team_abbr": "MIA",
        }
        for row in facts.rows[:2]
    ]
    cfg = replace(
        settings(SEASON), ask_claim_enforcement="enforce", agent_history_enabled=False
    )
    repo = BigQueryWarehouseRepository(cfg, client=SimpleNamespace())
    repo._governed_warehouse = SimpleNamespace(
        load=lambda seasons: (
            {"capture": {}},
            replace(facts, rows=[*facts.rows, *jr_rows]),
        )
    )
    old = dict(app.dependency_overrides)
    app.dependency_overrides.update(
        {
            get_settings: lambda: cfg,
            get_repository: lambda: repo,
            get_agent_client: lambda: object(),
        }
    )
    main_module._season_conversation_store(SEASON)._conversations.clear()
    try:
        # Direct parser check: selected player_id must match resolved player_id.
        with pytest.raises(SemanticError, match="Selected player does not match"):
            parse_opponent_splits(
                "Show Avery Example Jr.'s opponent splits in 2024-25",
                [PLAYER, {"player_id": 202, "player_name": "Avery Example Jr."}],
                SEASON,
                player_id=101,
            )

        with TestClient(app) as client:
            spoofed = {
                "analysis_type": "opponent_splits",
                "question": "Show Avery Example Jr.'s opponent splits in 2024-25",
                "players": [{"player_id": 101, "player_name": "Avery Example"}],
                "scope": {"season": SEASON, "as_of": "2025-01-12"},
                "opponent_splits_question": "Show Avery Example Jr.'s opponent splits in 2024-25",
            }
            resp = client.post(
                f"/api/agent/ask?season={SEASON}",
                json={
                    "question": "What about FT%?",
                    "conversation_id": "jr-mismatch",
                    "previous_context": spoofed,
                },
            )
            assert resp.status_code == 400

            # Browser-recovered relative window preserves original as_of cutoff.
            prior_last2 = {
                "analysis_type": "opponent_splits",
                "question": "Show Avery Example's opponent splits in his last 2 games in 2024-25",
                "players": [{"player_id": 101, "player_name": "Avery Example"}],
                "scope": {
                    "season": SEASON,
                    "window": "last_n_games",
                    "n": 2,
                    "as_of": "2025-01-09",
                },
                "opponent_splits_question": "Show Avery Example's opponent splits in his last 2 games (2024-25) Regular Season",
            }
            recovered = client.post(
                f"/api/agent/ask?season={SEASON}",
                json={
                    "question": "What about FT%?",
                    "conversation_id": "browser-last2-asof",
                    "previous_context": prior_last2,
                },
            )
            assert recovered.status_code == 200, recovered.text
            rec_payload = recovered.json()
            assert rec_payload["status"] == "ok", rec_payload
            assert rec_payload["opponent_splits"]["scope"]["as_of"] == "2025-01-09"
            assert sorted(
                rec_payload["opponent_splits"]["baseline"]["metrics"]["ft_pct"][
                    "game_ids"
                ]
            ) == ["03", "04"]
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(old)


@pytest.mark.parametrize(
    ("phrase", "expected_metric", "expected_bos_value"),
    [
        ("3-point shooting percentage", "fg3_pct", 2 / 10),
        ("three-point shooting percentage", "fg3_pct", 2 / 10),
        ("3pt shooting percentage", "fg3_pct", 2 / 10),
        ("free-throw shooting percentage", "ft_pct", 3 / 9),
        ("free throw percentage", "ft_pct", 3 / 9),
        ("field-goal shooting percentage", "fg_pct", 9 / 25),
    ],
)
def test_compound_shooting_metrics_resolve_to_specific_metric(
    split_agent, phrase, expected_metric, expected_bos_value
):
    result = split_agent.answer(f"Show Avery Example's {phrase} by opponent in 2024-25")
    assert result["status"] == "ok", result
    assert result["opponent_splits"]["display_metrics"] == [expected_metric]
    bos = next(
        row
        for row in result["opponent_splits"]["opponents"]
        if row["opponent_abbr"] == "BOS"
    )
    assert bos["metrics"][expected_metric]["value"] == pytest.approx(expected_bos_value)


@pytest.mark.parametrize(
    "unsupported_phrase",
    [
        "assist percentage",
        "rebound percentage",
        "turnover percentage",
        "steal percentage",
        "block percentage",
        "usage percentage",
        "offensive rebound percentage",
        "free throw attempt rate",
        "2-point shooting percentage",
        "box plus-minus",
        "assist-to-turnover ratio",
    ],
)
def test_unsupported_compound_rates_rejected_before_component_match(
    split_agent, unsupported_phrase
):
    result = split_agent.answer(
        f"Show Avery Example's {unsupported_phrase} by opponent in 2024-25"
    )
    assert result["status"] == "unsupported_scope", (unsupported_phrase, result)
    assert result["ask_route"]["route"] == "opponent_splits"
    assert not result.get("opponent_splits")

    first = split_agent.answer(QUESTION, conversation_id=f"unsup-{unsupported_phrase}")
    assert first["status"] == "ok"
    followup = split_agent.answer(
        f"What about {unsupported_phrase}?",
        conversation_id=f"unsup-{unsupported_phrase}",
    )
    assert followup["status"] == "unsupported_scope", (unsupported_phrase, followup)


def test_slash_date_ranges_normalize_and_unresolved_ranges_require_clarification(
    split_agent,
):
    result = split_agent.answer(
        "Show Avery Example's opponent splits from 2025/01/03 through 2025/01/05"
    )
    assert result["status"] == "ok", result
    scope = result["opponent_splits"]["scope"]
    assert scope["window"] == "date_range"
    assert scope["start_date"] == "2025-01-03"
    assert scope["as_of"] == "2025-01-05"
    assert sorted(
        result["opponent_splits"]["baseline"]["metrics"]["pts"]["game_ids"]
    ) == [
        "01",
        "02",
    ]

    between_slash = split_agent.answer(
        "Show Avery Example's opponent splits between 2025/01/03 and 2025/01/05"
    )
    assert between_slash["status"] == "ok", between_slash
    assert between_slash["opponent_splits"]["scope"]["start_date"] == "2025-01-03"
    assert between_slash["opponent_splits"]["scope"]["as_of"] == "2025-01-05"

    for unresolved in (
        "from 2025-01-03 until 2025-01-05",
        "from 2025/1/3 through 2025/1/5",
        "from 01/03/2025 to 01/05/2025",
        "between January 3 and January 10",
    ):
        clarify = split_agent.answer(
            f"Show Avery Example's opponent splits {unresolved}"
        )
        assert clarify["status"] == "clarification_required", (unresolved, clarify)
        assert not clarify.get("opponent_splits")


@pytest.mark.parametrize(
    "season_phrase",
    [
        "in the 1999-00 season",
        "in 1999-2000",
        "in the 1998 playoffs",
        "in the 1999 season",
        "in the 2016 season",
        "for the season of 2016",
        "for 2016",
        "(2016)",
        "in 2024-25 and 2016",
        "in the 2030-31 season",
    ],
)
def test_unavailable_seasons_do_not_fall_back_to_selected_season(
    split_agent, season_phrase
):
    result = split_agent.answer(f"Show Avery Example's opponent splits {season_phrase}")
    assert result["status"] == "unsupported_coverage", (season_phrase, result)
    assert not result.get("opponent_splits")


@pytest.mark.parametrize(
    "phrase", ["in the 2025 season", "for the season of 2025", "for 2025", "(2025)"]
)
def test_bare_year_does_not_default_to_the_selected_season(split_agent, phrase):
    result = split_agent.answer(f"Show Avery Example's opponent splits {phrase}")
    assert result["status"] == "clarification_required", (phrase, result)
    assert not result.get("opponent_splits")


@pytest.mark.parametrize(
    ("phrase", "phase", "window"),
    [
        ("in 2024-2025", "Regular Season", "season_to_date"),
        ("in 2025 NBA Playoffs", "Playoffs", "season_to_date"),
        ("in the post-season of 2025", "Playoffs", "season_to_date"),
        ("in the 2025 Conference Semifinals", "Playoffs", "season_to_date"),
        ("in Jan. 2025", "Regular Season", "date_range"),
        ("from 2025/01/03 through 2025/01/05", "Regular Season", "date_range"),
    ],
)
def test_years_in_resolved_date_and_season_expressions_are_preserved(
    split_agent, phrase, phase, window
):
    result = split_agent.answer(f"Show Avery Example's opponent splits {phrase}")
    assert result["status"] == "ok", (phrase, result)
    scope = result["opponent_splits"]["scope"]
    assert (scope["season"], scope["season_type"], scope["window"]) == (
        SEASON,
        phase,
        window,
    )


@pytest.mark.parametrize(
    "multi_season_phrase",
    [
        "over the last two seasons",
        "past 3 seasons",
        "across multiple seasons",
        "in both seasons",
        "across seasons",
        "career",
        "all-time",
    ],
)
def test_multi_season_expressions_do_not_fall_back_to_selected_season(
    split_agent, multi_season_phrase
):
    result = split_agent.answer(
        f"Show Avery Example's opponent splits {multi_season_phrase}"
    )
    assert result["status"] in {"unsupported_scope", "clarification_required"}, (
        multi_season_phrase,
        result,
    )
    assert not result.get("opponent_splits")


@pytest.mark.parametrize(
    "threshold_clause",
    [
        "among opponents he played at least 3 times",
        "among opponents with minimum 3 games",
        "with minimum 3 matchups",
        "played 3+ times",
        "with >= 3 games",
        "with at least 3 games",
    ],
)
def test_opponent_appearance_thresholds_rejected_before_scoring(
    split_agent, threshold_clause
):
    result = split_agent.answer(
        f"Show the toughest opponent for Avery Example in 2024-25 {threshold_clause}"
    )
    assert result["status"] == "unsupported_scope", (threshold_clause, result)
    assert result["ask_route"]["route"] == "opponent_splits"
    assert not result.get("opponent_splits")


def test_venue_normalization_win_loss_rejection_and_unresolved_venue_clarification(
    split_agent,
):
    for phrase, expected_venue, expected_ids in [
        ("away only", "AWAY", ["02", "03"]),
        ("away from home", "AWAY", ["02", "03"]),
        ("road only", "AWAY", ["02", "03"]),
        ("home only", "HOME", ["01", "04", "05"]),
    ]:
        result = split_agent.answer(
            f"Show Avery Example's opponent splits {phrase} in 2024-25"
        )
        assert result["status"] == "ok", (phrase, result)
        assert result["opponent_splits"]["scope"]["home_away"] == expected_venue
        assert (
            sorted(result["opponent_splits"]["baseline"]["metrics"]["pts"]["game_ids"])
            == expected_ids
        )

    away_from_home_vs = split_agent.answer(
        "How did Avery Example perform away from home versus every team in 2024-25?"
    )
    assert away_from_home_vs["status"] == "ok", away_from_home_vs
    assert away_from_home_vs["ask_route"]["route"] == "opponent_splits"
    assert away_from_home_vs["opponent_splits"]["scope"]["home_away"] == "AWAY"

    for outcome_phrase in (
        "in away wins",
        "in home losses",
        "in road wins",
        "away wins",
    ):
        rejected = split_agent.answer(
            f"Show Avery Example's opponent splits {outcome_phrase} in 2024-25"
        )
        assert rejected["status"] == "unsupported_scope", (outcome_phrase, rejected)
        assert not rejected.get("opponent_splits")

    for unresolved_venue in (
        "home or away",
        "on a neutral site",
        "on road",
        "at home and away",
    ):
        clarify = split_agent.answer(
            f"Show Avery Example's opponent splits {unresolved_venue} in 2024-25"
        )
        assert clarify["status"] == "clarification_required", (
            unresolved_venue,
            clarify,
        )
        assert not clarify.get("opponent_splits")
