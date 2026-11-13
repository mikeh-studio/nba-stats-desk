"""Actual JSON/SSE Ask routes preserve literal game populations and arithmetic."""

from copy import deepcopy
from datetime import date

import pytest
from app import main
from app.agent.conversation import InMemoryConversationStore
from app.agent.game_filters import extract_game_filters
from app.agent.semantics import SemanticError
from fastapi.testclient import TestClient
from scripts.ask_eval.fixtures import frozen_repository
from tests.test_reference_followup_aggregation import SOURCE, post, settings


def source():
    result = deepcopy(SOURCE)
    for row in result["rows"]:
        index = int(row["game_id"][1:])
        row.update(
            wl="W" if (index in {0, 1, 4, 6}) == (row["team_abbr"] == "AAA") else "L",
            oreb=index % 3,
            dreb=row["reb"] - min(index % 3, row["reb"]),
            pf=index % 5,
        )
    return result


def client(monkeypatch, snapshot=None):
    config = settings(ask_claim_enforcement="enforce")
    repository = frozen_repository(config, snapshot or source(), "complete")
    store = InMemoryConversationStore()
    monkeypatch.setattr(main, "_season_conversation_store", lambda season: store)
    monkeypatch.setitem(
        main.app.dependency_overrides, main.get_settings, lambda: config
    )
    monkeypatch.setitem(
        main.app.dependency_overrides, main.get_repository, lambda: repository
    )
    monkeypatch.setitem(
        main.app.dependency_overrides, main.get_agent_client, lambda: object()
    )
    return TestClient(
        main.app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)
    )


def appearances(predicate=lambda row: True, player=101):
    return [
        row
        for row in source()["rows"]
        if row["season_type"] == "Regular Season"
        and (player is None or row["player_id"] == player)
        and predicate(row)
    ]


@pytest.mark.parametrize("transport", ["json", "sse"])
@pytest.mark.parametrize(
    "clause,predicate",
    [
        ("at home", lambda row: row["home_away"] == "HOME"),
        ("on the road", lambda row: row["home_away"] == "AWAY"),
        ("in wins", lambda row: row["wl"] == "W"),
        ("in losses", lambda row: row["wl"] == "L"),
        ("on Mondays", lambda row: date.fromisoformat(row["game_date"]).weekday() == 0),
        ("against BBB", lambda row: row["opponent_abbr"] == "BBB"),
        ("for AAA", lambda row: row["team_abbr"] == "AAA"),
        ("in games with at least 4 assists", lambda row: row["ast"] >= 4),
        ("in games with fewer than 4 assists", lambda row: row["ast"] < 4),
        (
            "at home in wins",
            lambda row: row["home_away"] == "HOME" and row["wl"] == "W",
        ),
    ],
)
def test_scalar_filtered_population_and_values(
    monkeypatch, transport, clause, predicate
):
    with client(monkeypatch) as http:
        payload = post(
            http, transport, "Avery Example assists per game in 2024-25 " + clause, None
        )
    assert payload["status"] == "ok", payload
    assert payload["ask_route"]["route"] == "governed_metrics"
    assert payload["semantic_plan"]["model_calls"] == 0
    selected = appearances(predicate)
    result = payload["semantic_evidence"]["rows"][0]
    assert result["value"] == pytest.approx(
        sum(row["ast"] for row in selected) / len(selected)
    )
    assert result["observed_games"] == len(selected)
    assert payload["claim_validation"]["status"] != "invalid"


@pytest.mark.parametrize("transport", ["json", "sse"])
@pytest.mark.parametrize(
    "clause,predicate",
    [
        ("in wins", lambda row: row["wl"] == "W"),
        ("at home", lambda row: row["home_away"] == "HOME"),
        ("on Mondays", lambda row: date.fromisoformat(row["game_date"]).weekday() == 0),
        ("in games with at least 4 assists", lambda row: row["ast"] >= 4),
    ],
)
def test_league_baseline_filters_player_and_every_peer(
    monkeypatch, transport, clause, predicate
):
    with client(monkeypatch) as http:
        payload = post(
            http,
            transport,
            "Compare Avery Example assists against league average in 2024-25 " + clause,
            None,
        )
    assert payload["status"] == "ok", payload
    own, peers = appearances(predicate), appearances(predicate, None)
    evidence = payload["semantic_evidence"]
    assert evidence["player"]["value"] == pytest.approx(
        sum(row["ast"] for row in own) / len(own)
    )
    assert evidence["baseline"]["value"] == pytest.approx(
        sum(row["ast"] for row in peers) / len(peers)
    )
    assert evidence["membership"]["count"] == len(peers)
    assert (
        evidence["baseline_scope"]["population"]
        == "all recorded player appearances, including the focal player"
    )


@pytest.mark.parametrize("transport", ["json", "sse"])
@pytest.mark.parametrize(
    "component,clause",
    [("wl", "in wins"), ("home_away", "at home"), ("ast", "with at least 4 assists")],
)
def test_missing_filter_evidence_withholds_answer(
    monkeypatch, transport, component, clause
):
    snapshot = source()
    snapshot["rows"][0][component] = None
    with client(monkeypatch, snapshot) as http:
        payload = post(
            http, transport, "Avery Example points per game in 2024-25 " + clause, None
        )
    assert payload["status"] == "incomplete_evidence", payload
    assert payload["semantic_evidence"] is None
    assert payload["tables"] == []


@pytest.mark.parametrize("transport", ["json", "sse"])
def test_scorecard_uses_filtered_samples_and_same_peer_cohort(monkeypatch, transport):
    with client(monkeypatch) as http:
        payload = post(
            http,
            transport,
            "Compare Avery Example and Blair Sample in the 2024-25 regular season in wins",
            None,
        )
    assert payload["status"] == "ok", payload
    evidence = payload["semantic_evidence"]
    assert evidence["scope"]["game_result"] == "W"
    metric = next(row for row in evidence["metrics"] if row["key"] == "ast")
    for side, player in (("left", 101), ("right", 202)):
        rows = appearances(lambda row: row["wl"] == "W", player)
        assert metric[side]["value"] == pytest.approx(
            sum(row["ast"] for row in rows) / len(rows)
        )
        assert metric[side]["observed_games"] == len(rows)
    assert "in wins" in evidence["context_question"]


@pytest.mark.parametrize("transport", ["json", "sse"])
def test_metric_followup_retains_filter_and_validated_total(monkeypatch, transport):
    with client(monkeypatch) as http:
        first = post(
            http,
            transport,
            "Avery Example total points in 2024-25 in wins",
            "filter-followup",
        )
        assert first["status"] == "ok", first
        second = post(http, transport, "What about assists?", "filter-followup")
    assert second["status"] == "ok", second
    evidence = second["semantic_evidence"]
    assert evidence["scope"]["game_result"] == "W"
    assert evidence["scope"]["aggregation"] == "total"
    assert evidence["rows"][0]["value"] == sum(
        row["ast"] for row in appearances(lambda row: row["wl"] == "W")
    )


@pytest.mark.parametrize(
    "clause",
    [
        "not in wins",
        "except at home",
        "in wins or at home",
        "with at least 4 assists or fewer than 2 rebounds",
        "in wins in losses",
    ],
)
def test_negated_disjunctive_and_conflicting_predicates_withhold(clause):
    with pytest.raises(SemanticError):
        extract_game_filters("Avery Example assists in 2024-25 " + clause)


def test_local_filters_are_not_promoted_to_shared_scope():
    with pytest.raises(SemanticError):
        extract_game_filters(
            "Compare Avery Example in wins and Jordan Sample in losses"
        )
    with pytest.raises(SemanticError):
        extract_game_filters("Compare Avery Example in wins with league average")
    assert (
        extract_game_filters(
            "Compare Avery Example in wins and Jordan Sample in wins"
        ).query_fields["game_result"]
        == "W"
    )


def test_source_names_are_masked_before_literal_predicates():
    player = {"player_name": "Wins Only", "player_id": 123}
    parsed = extract_game_filters(
        "Compare Wins Only assists against league average", [player]
    )
    assert parsed.cleaned_question == "Compare Wins Only assists against league average"
    assert parsed.query_fields == {}


@pytest.mark.parametrize("transport", ["json", "sse"])
def test_recovered_scope_retains_filters_and_shape_without_aggregation_authority(
    monkeypatch, transport
):
    with client(monkeypatch) as http:
        first = post(
            http,
            transport,
            "Avery Example total points in 2024-25 in wins",
            "original-filter-scope",
        )
        assert first["status"] == "ok", first
        scope = first["semantic_evidence"]["scope"]
        previous = {
            "question": "Avery Example total points in 2024-25 in wins",
            "players": [{"player_id": 101, "player_name": "Avery Example"}],
            "scope": scope,
            "metrics": ["pts"],
        }
        second = post(
            http,
            transport,
            "What about assists per game?",
            "browser-filter-scope",
            previous_context=previous,
        )
    assert second["status"] == "ok", second
    actual_scope = second["semantic_evidence"]["scope"]
    assert actual_scope["game_result"] == "W"
    assert actual_scope["aggregation"] == "average"
    assert actual_scope["as_of"] == scope["as_of"]
    assert actual_scope["window"] == scope["window"]


@pytest.mark.parametrize("transport", ["json", "sse"])
def test_overview_filters_all_metrics_and_distributions(monkeypatch, transport):
    with client(monkeypatch) as http:
        payload = post(
            http,
            transport,
            "How did Avery Example perform in the 2024-25 regular season at home?",
            None,
        )
    assert payload["status"] == "ok", payload
    evidence = payload["semantic_evidence"]
    assert evidence["scope"]["home_away"] == "HOME"
    own = appearances(lambda row: row["home_away"] == "HOME")
    metric = next(row for row in evidence["metrics"] if row["key"] == "ast")
    assert metric["value"] == pytest.approx(sum(row["ast"] for row in own) / len(own))
    assert metric["observed_games"] == len(own)
    assert evidence["game_ids"] == [row["game_id"] for row in own]


@pytest.mark.parametrize(
    "rule",
    [
        "not in wins",
        "in wins or at home",
        "with at least 4 assists or fewer than 2 rebounds",
        "with at least 4 assists and with fewer than 2 assists",
    ],
)
def test_refusal_reaches_actual_route_without_model(monkeypatch, rule):
    with client(monkeypatch) as http:
        payload = post(
            http, "json", "Avery Example assists per game in 2024-25 " + rule, None
        )
    assert payload["status"] == "unsupported_scope", payload
    assert payload["semantic_evidence"] is None
    assert payload["tables"] == []


@pytest.mark.parametrize(
    "modifier", ["technical", "flagrant", "shooting", "offensive", "defensive"]
)
def test_personal_foul_metric_does_not_substitute_event_subtypes(monkeypatch, modifier):
    with client(monkeypatch) as http:
        payload = post(
            http, "json", f"Avery Example {modifier} fouls per game in 2024-25", None
        )
    assert payload["status"] == "unsupported_scope", payload
    assert payload["semantic_evidence"] is None


def test_filter_extraction_preserves_current_question_shape_markers():
    question = "Prior question: Avery Example points in wins\nCurrent question: Blair Sample assists in wins"
    cleaned = extract_game_filters(question).cleaned_question
    assert "\nCurrent question:" in cleaned


@pytest.mark.parametrize("transport", ["json", "sse"])
@pytest.mark.parametrize("clause", ["in wins", "at home"])
@pytest.mark.parametrize(
    "template",
    [
        "Compare Avery Example assists {clause} against the 2024-25 regular season league average.",
        "Compared to the league average {clause}, how did Avery Example perform in assists per game in 2024-25?",
        "Avery Example assists {clause}, the league average in 2024-25.",
    ],
)
def test_calendar_preposed_and_comma_operands_do_not_share_local_filter(
    monkeypatch, transport, clause, template
):
    with client(monkeypatch) as http:
        payload = post(http, transport, template.format(clause=clause), None)
    assert payload["status"] == "unsupported_scope", payload
    assert payload["semantic_evidence"] is None
    assert payload["tables"] == payload["charts"] == []
    assert payload["semantic_plan"]["model_calls"] == 0


@pytest.mark.parametrize("transport", ["json", "sse"])
@pytest.mark.parametrize(
    "clause,predicate",
    [
        ("in wins", lambda row: row["wl"] == "W"),
        ("at home", lambda row: row["home_away"] == "HOME"),
    ],
)
@pytest.mark.parametrize(
    "template",
    [
        "Compare Avery Example assists against the 2024-25 regular season league average {clause}.",
        "{clause}, compare Avery Example assists against the 2024-25 regular season league average.",
        "Compared to the league average, how did Avery Example perform in assists per game in 2024-25 {clause}?",
        "Compared to the league average {clause}, how did Avery Example perform in assists per game in 2024-25 {clause}?",
        "Avery Example assists, the league average in 2024-25 {clause}.",
        "Compare Avery Example assists {clause} against the 2024-25 regular season league average {clause}.",
    ],
)
def test_shared_global_trailing_and_repeated_filters_preserve_arithmetic(
    monkeypatch, transport, clause, predicate, template
):
    with client(monkeypatch) as http:
        payload = post(http, transport, template.format(clause=clause), None)
    assert payload["status"] == "ok", payload
    own, peers = appearances(predicate), appearances(predicate, None)
    evidence = payload["semantic_evidence"]
    assert evidence["player"]["value"] == pytest.approx(
        sum(row["ast"] for row in own) / len(own)
    )
    assert evidence["baseline"]["value"] == pytest.approx(
        sum(row["ast"] for row in peers) / len(peers)
    )
    assert evidence["player"]["observed_games"] == len(own)
    assert evidence["membership"]["count"] == len(peers)


@pytest.mark.parametrize("transport", ["json", "sse"])
@pytest.mark.parametrize("threshold", ["3.999999", "0.0000001234567"])
def test_fractional_threshold_is_exact_across_followup_and_browser_recovery(
    monkeypatch, transport, threshold
):
    question = (
        "Avery Example total points in 2024-25 with more than " + threshold + " assists"
    )
    with client(monkeypatch) as http:
        first = post(http, transport, question, "exact-filter-value")
        assert first["status"] == "ok", first
        second = post(
            http, transport, "What about assists per game?", "exact-filter-value"
        )
        scope = first["semantic_evidence"]["scope"]
        previous = {
            "question": question,
            "players": [{"player_id": 101, "player_name": "Avery Example"}],
            "scope": scope,
            "metrics": ["pts"],
        }
        recovered = post(
            http,
            transport,
            "What about assists per game?",
            "recovered-exact-filter-value",
            previous_context=previous,
        )
    own = appearances(lambda row: row["ast"] > float(threshold))
    assert first["semantic_evidence"]["rows"][0]["value"] == sum(
        row["pts"] for row in own
    )
    for payload in (first, second, recovered):
        assert payload["status"] == "ok", payload
        evidence = payload["semantic_evidence"]
        assert evidence["scope"]["stat_filters"][0]["value"] == float(threshold)
        assert evidence["rows"][0]["observed_games"] == len(own)
    for payload in (second, recovered):
        assert payload["semantic_evidence"]["rows"][0]["value"] == pytest.approx(
            sum(row["ast"] for row in own) / len(own)
        )


@pytest.mark.parametrize("transport", ["json", "sse"])
@pytest.mark.parametrize(
    "clause,predicate",
    [("", lambda row: True), (" at home", lambda row: row["home_away"] == "HOME")],
)
def test_complete_new_player_request_does_not_inherit_old_predicates(
    monkeypatch, transport, clause, predicate
):
    with client(monkeypatch) as http:
        first = post(
            http,
            transport,
            "Avery Example points per game in 2024-25 in wins",
            "fresh-player-filter",
        )
        assert first["status"] == "ok", first
        second = post(
            http,
            transport,
            "Blair Sample assists per game in 2024-25" + clause,
            "fresh-player-filter",
        )
    assert second["status"] == "ok", second
    evidence = second["semantic_evidence"]
    selected = appearances(predicate, 202)
    assert evidence["scope"]["player_id"] == 202
    assert evidence["scope"]["game_result"] is None
    assert evidence["rows"][0]["observed_games"] == len(selected)
    assert evidence["rows"][0]["value"] == pytest.approx(
        sum(row["ast"] for row in selected) / len(selected)
    )


@pytest.mark.parametrize("transport", ["json", "sse"])
def test_complete_same_player_overview_starts_without_prior_filters(
    monkeypatch, transport
):
    with client(monkeypatch) as http:
        first = post(
            http,
            transport,
            "Avery Example points per game in 2024-25 in wins",
            "fresh-overview-filter",
        )
        assert first["status"] == "ok", first
        second = post(
            http,
            transport,
            "How did Avery Example perform in the 2024-25 regular season?",
            "fresh-overview-filter",
        )
    assert second["status"] == "ok", second
    assert second["semantic_evidence"]["scope"].get("game_result") is None
    metric = next(
        row for row in second["semantic_evidence"]["metrics"] if row["key"] == "ast"
    )
    assert metric["observed_games"] == 6
    assert metric["value"] == pytest.approx(
        sum(row["ast"] for row in appearances()) / 6
    )


@pytest.mark.parametrize("transport", ["json", "sse"])
def test_new_identity_selection_preserves_only_the_new_request_filters(
    monkeypatch, transport
):
    snapshot = source()
    for row in snapshot["rows"]:
        if row["player_id"] == 303:
            row["player_name"] = "Blair Sample"
    with client(monkeypatch, snapshot) as http:
        first = post(
            http,
            transport,
            "Avery Example points per game in 2024-25 in wins",
            "fresh-selected-filter",
        )
        assert first["status"] == "ok", first
        pending = post(
            http,
            transport,
            "Blair Sample assists per game in 2024-25 at home",
            "fresh-selected-filter",
        )
        assert pending["status"] == "clarification_required", pending
        selected = post(
            http,
            transport,
            "Blair Sample",
            "fresh-selected-filter",
            selected_player_id=202,
            selected_player_name="Blair Sample",
        )
    assert selected["status"] == "ok", selected
    evidence = selected["semantic_evidence"]
    assert evidence["scope"]["game_result"] is None
    assert evidence["scope"]["home_away"] == "HOME"
    assert evidence["scope"]["player_id"] == 202
    assert evidence["rows"][0]["observed_games"] == 3


@pytest.mark.parametrize("transport", ["json", "sse"])
@pytest.mark.parametrize("clause", ["in wins", "at home"])
def test_infix_compared_does_not_promote_focal_filter_to_global(
    monkeypatch, transport, clause
):
    with client(monkeypatch) as http:
        payload = post(
            http,
            transport,
            f"Avery Example assists {clause} compared to league average in 2024-25",
            None,
        )
    assert payload["status"] == "unsupported_scope", payload
    assert payload["semantic_evidence"] is None
    assert payload["tables"] == payload["charts"] == []


@pytest.mark.parametrize("transport", ["json", "sse"])
@pytest.mark.parametrize(
    "template",
    [
        "Avery Example assists compared to league average in 2024-25 in wins",
        "In wins, Avery Example assists compared to league average in 2024-25",
    ],
)
def test_infix_compared_retains_explicit_trailing_or_leading_shared_filter(
    monkeypatch, transport, template
):
    with client(monkeypatch) as http:
        payload = post(http, transport, template, None)
    assert payload["status"] == "ok", payload
    peers = appearances(lambda row: row["wl"] == "W", None)
    assert payload["semantic_evidence"]["membership"]["count"] == len(peers)
    assert payload["semantic_evidence"]["baseline"]["value"] == pytest.approx(
        sum(row["ast"] for row in peers) / len(peers)
    )


@pytest.mark.parametrize("transport", ["json", "sse"])
@pytest.mark.parametrize("pronoun", ["their", "his"])
def test_pronouns_inside_new_named_request_predicate_do_not_inherit_context(
    monkeypatch, transport, pronoun
):
    with client(monkeypatch) as http:
        first = post(
            http,
            transport,
            "Avery Example points per game in 2024-25 at home",
            "predicate-pronoun-new-name",
        )
        assert first["status"] == "ok", first
        second = post(
            http,
            transport,
            f"Blair Sample assists per game in 2024-25 in games {pronoun} team won",
            "predicate-pronoun-new-name",
        )
    assert second["status"] == "ok", second
    evidence = second["semantic_evidence"]
    expected = appearances(lambda row: row["wl"] == "W", 202)
    assert evidence["scope"]["player_id"] == 202
    assert evidence["scope"]["game_result"] == "W"
    assert evidence["scope"]["home_away"] is None
    assert evidence["rows"][0]["observed_games"] == len(expected) == 3
    assert evidence["rows"][0]["value"] == pytest.approx(
        sum(row["ast"] for row in expected) / len(expected)
    )


@pytest.mark.parametrize("transport", ["json", "sse"])
@pytest.mark.parametrize(
    "threshold", ["3.9999999999999999", "0.000000123456789123456789"]
)
def test_unrepresentable_threshold_precision_requires_clarification(
    monkeypatch, transport, threshold
):
    with client(monkeypatch) as http:
        payload = post(
            http,
            transport,
            f"Avery Example total points in 2024-25 with more than {threshold} assists",
            None,
        )
    assert payload["status"] == "clarification_required", payload
    assert "precision" in payload["answer"]
    assert payload["semantic_evidence"] is None
    assert payload["tables"] == payload["charts"] == []


@pytest.mark.parametrize(
    "question",
    [
        "How many games did each player play?",
        "How many games did both players play this season?",
        "How did these two perform?",
        "What about both of them?",
    ],
)
def test_pair_reference_cues_retain_genuine_analysis_continuity(question):
    from app.agent.followup import inherits_analysis

    assert inherits_analysis(question)


@pytest.mark.parametrize("pronoun", ["their", "his"])
def test_team_result_pronoun_is_not_an_analysis_reference(pronoun):
    from app.agent.followup import inherits_analysis

    assert not inherits_analysis(
        f"Blair Sample assists per game in 2024-25 in games {pronoun} team won"
    )
