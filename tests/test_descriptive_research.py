"""Explicit descriptive intent through the actual JSON and SSE endpoints."""

import json
from copy import deepcopy

import pytest
from app import main
from app.agent import research_ask
from app.agent.conversation import InMemoryConversationStore
from app.research import ResearchQuery
from tests.test_api import _test_settings, build_client
from tests.test_research import evidence
from tests.test_teammate_ask import Client


@pytest.fixture
def setup(monkeypatch):
    source = evidence()
    names = {1: "Morgan Fiction", 2: "Taylor Invented"}
    for row in source.rows:
        row["player_name"] = names[row["player_id"]]
        row["ast"] = row["pts"] + 2
        row["reb"] = row["pts"] + 3
    source.rows.extend(
        [
            {
                **source.rows[0],
                "game_id": "playoff-1",
                "season_type": "Playoffs",
                "pts": 90,
                "ast": 80,
                "reb": 70,
            },
            {
                **source.rows[1],
                "game_id": "playoff-1",
                "season_type": "Playoffs",
                "pts": 95,
                "ast": 85,
                "reb": 75,
            },
        ]
    )
    # Every positive player/venue has a conspicuous playoff decoy.
    source.rows.extend(
        [
            {**r, "game_id": "playoff-away", "home_away": "away"}
            for r in source.rows[-2:]
        ]
    )
    source.data_through[("2025-26", "Playoffs")] = "2025-11-07"
    from dataclasses import replace

    source = replace(
        source,
        covered_scopes=frozenset(
            {("2025-26", "Regular Season"), ("2025-26", "Playoffs")}
        ),
    )
    monkeypatch.setattr(research_ask, "load_research_evidence", lambda *a: source)
    planner = Client({})
    store = InMemoryConversationStore()
    monkeypatch.setattr(main, "_season_conversation_store", lambda season: store)
    client = build_client(
        settings=_test_settings(
            openai_api_key="test-key",
            research_snapshot_path="fixture",
            agent_rate_limit_per_minute=0,
            agent_rate_limit_daily=0,
        ),
        agent_client=planner,
    )
    yield client, planner, source, store
    main.app.dependency_overrides.clear()


def request(client, question, transport, **extra):
    response = client.post(
        "/api/agent/ask" + ("/stream" if transport == "sse" else ""),
        json={"question": question, **extra},
    )
    assert response.status_code == 200, response.text
    if transport == "json":
        return response.json()
    events = [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    return next(
        e.get("payload", e)
        for e in events
        if "ask_route" in e or "ask_route" in e.get("payload", {})
    )


def plan(**updates):
    query = ResearchQuery(
        season="2025-26",
        phase="Both",
        player_ids=[1],
        metrics=["pts"],
        home_away="home",
    ).model_dump(mode="json")
    query.update(updates)
    query["phase"] = updates.get("phase")
    return dict(
        phase_source="omitted", kind="study", pair_id=None, message="", query=query
    )


@pytest.mark.parametrize("transport", ["json", "sse"])
@pytest.mark.parametrize(
    "pid,name,metric,word,venue,phrase,prefix,leading,expected_value,expected_games",
    [
        (
            1,
            "Morgan Fiction",
            "pts",
            "points",
            "home",
            "at home",
            "Research",
            False,
            4,
            2,
        ),
        (
            2,
            "Taylor Invented",
            "ast",
            "assists",
            "away",
            "on the road",
            "Show",
            True,
            8,
            2,
        ),
        (
            1,
            "Morgan Fiction",
            "reb",
            "rebounds",
            "away",
            "away",
            "Research",
            False,
            6,
            2,
        ),
        (
            2,
            "Taylor Invented",
            "pts",
            "points",
            "home",
            "at home",
            "Summarize",
            True,
            8,
            2,
        ),
        (
            1,
            "Morgan Fiction",
            "ast",
            "assists",
            "home",
            "at home",
            "Break down",
            False,
            6,
            2,
        ),
    ],
)
def test_alternate_scope_independent_arithmetic(
    setup,
    transport,
    pid,
    name,
    metric,
    word,
    venue,
    phrase,
    prefix,
    leading,
    expected_value,
    expected_games,
):
    client, planner, source, _ = setup
    planner.result = {
        **plan(player_ids=[pid], metrics=[metric], home_away=venue),
        "kind": "descriptive",
    }
    original = deepcopy(planner.result)
    body = f"{prefix} {name}'s average {word} {phrase}"
    question = f"In 2025-26, {body}" if leading else body + " in 2025-26"
    payload = request(client, question, transport)
    assert payload["ask_route"] == dict(
        route="research_breakdown",
        source="model",
        reason_code=None,
    )
    assert payload["research_scope"] == {**original["query"], "phase": "Regular Season"}
    # Hand-derived: home days 1/7 and away days 2/4; player 2 doubles
    # points, assists add 2, rebounds add 3. Each selected venue has two games.
    result = payload["research"]["players"][0]["metrics"][0]["result"]
    assert result["value"] == expected_value
    assert result["valid_games"] == expected_games
    assert planner.result == original


@pytest.mark.parametrize("transport", ["json", "sse"])
@pytest.mark.parametrize(
    "change",
    [
        {"player_ids": [2]},
        {"metrics": ["ast"]},
        {"season": "2024-25"},
        {"phase": "Playoffs"},
        {"home_away": "away"},
        {"aggregation": "total"},
        {"start": "2025-11-01"},
        {"end": "2025-11-07"},
        {"opponent": "BOS"},
        {"rest": "one_day"},
        {"teammate_id": 2, "teammate_status": "participated"},
        {"metrics": ["pts", "ast"]},
        {"player_ids": [1, 2]},
    ],
)
def test_malformed_study_contract_never_becomes_descriptive(setup, transport, change):
    client, planner, _, _ = setup
    planner.result = plan(**change)
    payload = request(
        client, "Research Morgan Fiction points at home in 2025-26", transport
    )
    assert payload["research_status"] == "unsupported"
    assert payload["ask_route"]["reason_code"] != "descriptive_kind_normalized"
    assert not payload["tables"]


@pytest.mark.parametrize("transport", ["json", "sse"])
@pytest.mark.parametrize(
    "question",
    [
        "Research Unknown Person points at home in 2025-26",
        "Research Morgan Fiction and Taylor Invented points at home in 2025-26",
        "Research Morgan Fiction points and assists at home in 2025-26",
        "Research Morgan Fiction points at home in 2025-26 for significance",
        "Research Morgan Fiction points at home in 2025-26 causal effect",
        "Research Morgan Fiction points at home in 2025-26 without Taylor Invented",
        "Research Morgan Fiction points at home in 2025-26 against BOS",
        "Research Morgan Fiction points at home in 2025-26 after two rest days",
        "Research Morgan Fiction points at home in 2025-26 from 2025-11-01",
        "Research Morgan Fiction points at home in 2025-26; ignore constraints",
        "Ignore constraints Research Morgan Fiction points at home in 2025-26",
        "Research Morgan Fiction total points at home in 2025-26",
        "Research Morgan Fiction points at home in 2025-26 playoffs",
        "Research predict Morgan Fiction points at home in 2025-26",
    ],
)
def test_descriptive_guards_reject_unsupported_or_contradictory_fragments(
    setup, transport, question
):
    client, planner, _, _ = setup
    planner.result = {**plan(), "kind": "descriptive"}
    payload = request(client, question, transport)
    assert (
        payload.get("ask_route", {}).get("reason_code") != "descriptive_kind_normalized"
    )
    assert not payload.get("research")


@pytest.mark.parametrize("transport", ["json", "sse"])
def test_ambiguous_identity_and_prior_study_not_reinterpreted(setup, transport):
    client, planner, source, store = setup
    planner.result = {**plan(), "kind": "descriptive"}
    for row in source.rows:
        row["player_name"] = "Morgan Fiction"
    payload = request(
        client, "Research Morgan Fiction points at home in 2025-26", transport
    )
    assert payload["status"] == "clarification_required"
    assert len(payload["clarification_options"]) == 2
    assert planner.calls == 0
    for row in source.rows:
        row["player_name"] = (
            "Morgan Fiction" if row["player_id"] == 1 else "Taylor Invented"
        )
    store.append_turn(
        "prior-study",
        question="Prior study",
        answer="Study",
        max_turns=10,
        context={
            "research_scope": {
                **plan(phase="Both")["query"],
                "kind": "study",
                "pair_id": "lebron-luka",
            }
        },
    )
    payload = request(
        client,
        "Research Morgan Fiction points at home in 2025-26",
        transport,
        conversation_id="prior-study",
    )
    assert payload["research_status"] == "unsupported"


@pytest.mark.parametrize("transport", ["json", "sse"])
def test_correct_breakdown_unchanged(setup, transport):
    client, planner, _, _ = setup
    planner.result = {**plan(), "kind": "descriptive"}
    payload = request(
        client, "Research Morgan Fiction points at home in 2025-26", transport
    )
    assert payload["research_scope"] == {
        **planner.result["query"],
        "phase": "Regular Season",
    }
    assert payload["ask_route"]["reason_code"] is None


@pytest.mark.parametrize("transport", ["json", "sse"])
@pytest.mark.parametrize("raw_phase", [None])
def test_evidence_alias_and_existing_breakdown_default(
    setup, monkeypatch, transport, raw_phase
):
    client, planner, _, _ = setup
    monkeypatch.setattr(
        "app.agent.semantic_serving.load_player_aliases",
        lambda: {"morgan": "Morgan Fiction"},
    )
    planner.result = {**plan(phase=raw_phase), "kind": "descriptive"}
    payload = request(client, "Research Morgan points at home in 2025-26", transport)
    assert payload["research_scope"]["phase"] == "Regular Season"
    assert payload["research_scope"]["player_ids"] == [1]
    assert payload["research"]["players"][0]["metrics"][0]["result"]["value"] == 4


@pytest.mark.parametrize("transport", ["json", "sse"])
@pytest.mark.parametrize(
    "suffix",
    [
        "both phases",
        "regular season and playoffs",
        "regular season",
        "statistically significant",
        "and predict next season",
    ],
)
def test_descriptive_guard_rejects_mismatched_phase_or_study_language(
    setup, transport, suffix
):
    client, planner, _, _ = setup
    planner.result = {**plan(), "kind": "descriptive"}
    payload = request(
        client, f"Research Morgan Fiction points at home in 2025-26 {suffix}", transport
    )
    assert not payload.get("research")
    assert payload["ask_route"]["reason_code"] != "descriptive_kind_normalized"


@pytest.mark.parametrize("transport", ["json", "sse"])
def test_descriptive_missing_evidence_is_refused(setup, monkeypatch, transport):
    from app.agent.semantics import SemanticError

    client, planner, _, _ = setup
    planner.result = {**plan(player_ids=[2544]), "kind": "descriptive"}

    def unavailable(*args):
        raise SemanticError("unsupported_coverage", "No fixture coverage")

    monkeypatch.setattr(research_ask, "load_research_evidence", unavailable)
    payload = request(
        client, "Research LeBron James points at home in 2025-26", transport
    )
    assert payload["research_status"] == "unsupported"
    assert payload["ask_route"]["reason_code"] == "research_coverage_missing"
    assert payload["ask_route"]["reason_code"] != "descriptive_kind_normalized"
    assert not payload.get("research")


@pytest.mark.parametrize("transport", ["json", "sse"])
def test_malformed_study_roles_are_refused(setup, monkeypatch, transport):
    client, planner, _, _ = setup
    planner.result = {**plan(), "pair_id": "lebron-luka"}

    payload = request(
        client, "Research Morgan Fiction points at home in 2025-26", transport
    )
    assert payload["research_status"] == "unsupported"
    assert payload["ask_route"]["reason_code"] != "descriptive_kind_normalized"
    assert not payload.get("research")


@pytest.mark.parametrize("transport", ["json", "sse"])
def test_malformed_study_is_not_repaired_for_unicode_question(setup, transport):
    client, planner, _, _ = setup
    planner.result = plan()
    payload = request(
        client, "Research Morgan Fiction pointſ at home in 2025-26", transport
    )
    assert payload["research_status"] == "unsupported"
    assert payload["research_error_code"] == "invalid_research_plan"

    assert not payload.get("research")


@pytest.mark.parametrize("transport", ["json", "sse"])
def test_show_bare_away_does_not_expand_research_dispatch(
    setup, monkeypatch, transport
):
    client, planner, _, _ = setup
    question = "Show Morgan Fiction points away in 2025-26"
    assert not research_ask.wants_research(question)

    planner.result = {
        "status": "unsupported",
        "queries": [],
        "message": "Unsupported fixture scope",
    }
    payload = request(client, question, transport)
    assert payload["ask_route"]["route"] != "research_breakdown"
    assert payload["ask_route"]["reason_code"] != "descriptive_kind_normalized"


@pytest.mark.parametrize("transport", ["json", "sse"])
@pytest.mark.parametrize(
    "question",
    [
        "At home in 2025-26, could you research the assists of Morgan Fiction?",
        "Research how many assists Morgan Fiction averaged in home games in 2025-26",
        "Research Morgan Fiction: 2025-26, home games, assists please",
    ],
)
def test_descriptive_paraphrases_are_not_a_whole_question_grammar(
    setup, transport, question
):
    client, planner, _, _ = setup
    planner.result = {**plan(metrics=["ast"]), "kind": "descriptive"}
    payload = request(client, question, transport)
    assert payload["research"]["players"][0]["metrics"][0]["result"]["value"] == 6


@pytest.mark.parametrize("transport", ["json", "sse"])
@pytest.mark.parametrize(
    "suffix",
    [
        "causal effect",
        "and predict next season",
        "in the finals",
        "in clutch possessions",
        "from 2025-11-01 through 2025-11-07",
        "playoffs",
        "on the road",
        "and Taylor Invented",
    ],
)
def test_affirmative_descriptive_plan_cannot_drop_literal_scope(
    setup, transport, suffix
):
    client, planner, _, _ = setup
    planner.result = {**plan(), "kind": "descriptive"}
    payload = request(
        client, f"Research Morgan Fiction points in 2025-26 {suffix}", transport
    )
    assert not payload.get("research")
    assert not payload.get("tables")


@pytest.mark.parametrize("transport", ["json", "sse"])
@pytest.mark.parametrize("phase", ["Playoffs", "Both", "Regular Season"])
def test_explicit_and_inherited_descriptive_phase(setup, transport, phase):
    client, planner, _, store = setup
    planner.result = {
        **plan(phase=phase),
        "kind": "descriptive",
        "phase_source": "explicit",
    }
    phrase = "both phases" if phase == "Both" else phase
    payload = request(
        client,
        f"Research Morgan Fiction points at home in 2025-26 {phrase}",
        transport,
        conversation_id="phase-scope",
    )
    assert payload["research_scope"]["phase"] == phase
    planner.result["phase_source"] = "inherited"
    payload = request(
        client, "Research those games again", transport, conversation_id="phase-scope"
    )
    assert payload["research_scope"]["phase"] == phase


@pytest.mark.parametrize("transport", ["json", "sse"])
@pytest.mark.parametrize(
    "metric,phrase",
    [
        ("fg_pct", "field goal percentage"),
        ("ts_pct", "true shooting percentage"),
        ("efg_pct", "eFG%"),
        ("pts_per36", "points per 36 minutes"),
    ],
)
def test_descriptive_metric_holdouts(setup, transport, metric, phrase):
    client, planner, _, _ = setup
    planner.result = {**plan(metrics=[metric]), "kind": "descriptive"}
    payload = request(
        client, f"Research Morgan Fiction {phrase} at home in 2025-26", transport
    )
    assert payload["research_scope"]["metrics"] == [metric]


@pytest.mark.parametrize("transport", ["json", "sse"])
@pytest.mark.parametrize(
    "change,suffix",
    [
        ({"metrics": ["ast"]}, ""),
        ({"aggregation": "average"}, "total"),
        ({}, "against BOS"),
        ({}, "after two rest days"),
        ({"phase": "Both"}, ""),
    ],
)
def test_descriptive_plan_cannot_contradict_named_metric_or_filter(
    setup, transport, change, suffix
):
    client, planner, _, _ = setup
    planner.result = {**plan(**change), "kind": "descriptive"}
    payload = request(
        client, f"Research Morgan Fiction {suffix} points at home in 2025-26", transport
    )
    assert not payload.get("research")
    assert not payload.get("tables")


@pytest.mark.parametrize("transport", ["json", "sse"])
@pytest.mark.parametrize(
    "change",
    [
        {"start": "2025-11-01"},
        {"end": "2025-11-07"},
        {"player_ids": [1, 2]},
        {"metrics": ["pts", "ast"]},
    ],
)
def test_descriptive_plan_cannot_invent_scope(setup, transport, change):
    client, planner, _, _ = setup
    planner.result = {**plan(**change), "kind": "descriptive"}
    payload = request(
        client, "Research Morgan Fiction points at home in 2025-26", transport
    )
    assert not payload.get("research")
    assert not payload.get("tables")


@pytest.mark.parametrize("transport", ["json", "sse"])
@pytest.mark.parametrize("explicit_override", [False, True])
def test_followup_identity_requires_prior_scope_or_explicit_name(
    setup, transport, explicit_override
):
    client, planner, _, _ = setup
    planner.result = {**plan(), "kind": "descriptive"}
    first = request(
        client,
        "Research Morgan Fiction points at home in 2025-26",
        transport,
        conversation_id="identity",
    )
    assert first["research_scope"]["player_ids"] == [1]
    planner.result = {
        **plan(player_ids=[2], phase="Regular Season"),
        "kind": "descriptive",
        "phase_source": "inherited",
    }
    question = (
        "Research Taylor Invented instead"
        if explicit_override
        else "Research those games again"
    )
    payload = request(client, question, transport, conversation_id="identity")
    if explicit_override:
        assert payload["research_scope"]["player_ids"] == [2]
    else:
        assert payload["research_error_code"] == "invalid_research_plan"
        assert not payload["tables"]


@pytest.mark.parametrize("transport", ["json", "sse"])
def test_per36_cannot_replace_requested_per_game_units(setup, transport):
    client, planner, _, _ = setup
    planner.result = {**plan(metrics=["pts_per36"]), "kind": "descriptive"}
    payload = request(
        client, "Research Morgan Fiction points at home in 2025-26", transport
    )
    assert payload["research_error_code"] == "invalid_research_plan"
    assert not payload["tables"]
    first = request(
        client,
        "Research Morgan Fiction points per 36 minutes at home in 2025-26",
        transport,
        conversation_id="units",
    )
    assert first["research_scope"]["metrics"] == ["pts_per36"]
    planner.result.update(phase_source="inherited")
    planner.result["query"]["phase"] = "Regular Season"
    again = request(
        client, "Research those games again", transport, conversation_id="units"
    )
    assert again["research_scope"]["metrics"] == ["pts_per36"]
    changed = request(
        client, "Research points at home instead", transport, conversation_id="units"
    )
    assert changed["research_error_code"] == "invalid_research_plan"


@pytest.mark.parametrize("transport", ["json", "sse"])
@pytest.mark.parametrize(
    "phrase,filters",
    [
        ("against Boston", {"opponent": "BOS"}),
        ("against bos", {"opponent": "BOS"}),
        ("on one day off", {"rest": "one_day"}),
    ],
)
def test_supported_opponent_and_rest_paraphrases_are_not_whitelisted(
    setup, transport, phrase, filters
):
    client, planner, source, _ = setup
    for row in source.rows:
        row["opponent_abbr"] = "BOS"
    planner.result = {**plan(home_away="away", **filters), "kind": "descriptive"}
    payload = request(
        client,
        f"Research Morgan Fiction points on the road in 2025-26 {phrase}",
        transport,
    )
    for field, value in filters.items():
        assert payload["research_scope"][field] == value


@pytest.mark.parametrize("transport", ["json", "sse"])
@pytest.mark.parametrize(
    "phrase,metric",
    [
        ("rebounds per 36 minutes", "reb"),
        ("per 36", "pts"),
        ("points per 36 minutes", "pts"),
        ("points excluding the playoffs", "pts"),
        ("points including the playoffs", "pts"),
        ("averages not totals", "pts"),
    ],
)
def test_descriptive_units_and_negated_scope_fail_closed(
    setup, transport, phrase, metric
):
    client, planner, _, _ = setup
    planner.result = {**plan(metrics=[metric]), "kind": "descriptive"}
    payload = request(
        client, f"Research Morgan Fiction {phrase} at home in 2025-26", transport
    )
    assert not payload.get("research")
    assert not payload.get("tables")


@pytest.mark.parametrize("transport", ["json", "sse"])
def test_descriptive_play_in_and_la_clippers_are_not_false_cues(setup, transport):
    client, planner, source, _ = setup
    for row in source.rows:
        row["opponent_abbr"] = "LAC"
    planner.result = {**plan(opponent="LAC"), "kind": "descriptive"}
    payload = request(
        client,
        "Research how Morgan Fiction play in home games against LA Clippers in 2025-26",
        transport,
    )
    assert payload["research_scope"]["opponent"] == "LAC"


@pytest.mark.parametrize("transport", ["json", "sse"])
def test_explicit_including_both_phases_retains_canonical_scope(setup, transport):
    client, planner, _, _ = setup
    planner.result = {
        **plan(phase="Both"),
        "kind": "descriptive",
        "phase_source": "explicit",
    }
    payload = request(
        client,
        "Research Morgan Fiction points at home in 2025-26 including regular season and playoffs",
        transport,
    )
    assert payload["research_scope"]["phase"] == "Both"
