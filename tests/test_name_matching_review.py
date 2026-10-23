"""Independent-review regressions: identity, claim enforcement and source scope."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from app.agent.claims import finalize
from app.agent.name_matching import prepare_names, run_name_matching
from app.agent.player_suggestions import resolve_player_name
from tests.test_availability import bundle  # noqa: F401
from tests.test_name_matching_routes import ask, preview  # noqa: F401


@pytest.mark.parametrize("transport", ["", "/stream"])
def test_appearance_disclosure_is_bound_and_tampering_still_fails(preview, transport):  # noqa: F811
    client, _, _, _ = preview
    text = "How many games did Avrey Finch play in the 2025-26 regular season?"
    payload = ask(client, text, transport)
    assert payload["status"] == "ok"
    assert "Using Avery Finch" in payload["answer"]
    assert payload["claim_validation"]["errors"] == []
    tampered = deepcopy(payload)
    tampered["answer"] = tampered["answer"].replace(
        "games played", "million games played"
    )
    assert finalize(tampered, mode="enforce")["status"] == "error"
    first = ask(
        client,
        text.replace("Avrey Finch", "Avery"),
        transport,
        conversation_id="claim-select",
    )
    choice = first["clarification_options"][0]
    selected = ask(
        client,
        "Avery Finch",
        transport,
        conversation_id="claim-select",
        selected_player_id=choice["player_id"],
        selected_player_name=choice["player_name"],
    )
    assert selected["status"] == "ok"
    assert "as you selected" in selected["answer"]
    assert selected["claim_validation"]["errors"] == []


@pytest.mark.parametrize(
    "name",
    ["Michael Porter Jr.", "Jaren Jackson Jr.", "Trey Murphy III", "Dereck Lively II"],
)
def test_suffix_omission_resolves_only_unambiguous_base(name):
    base = " ".join(name.split()[:-1])
    result = resolve_player_name(base, [{"player_id": 1, "player_name": name}])
    assert result["status"] == "ok"
    assert result["resolution"]["method"] == "name_suffix"
    competing = [
        {"player_id": 1, "player_name": name},
        {"player_id": 2, "player_name": base},
    ]
    assert resolve_player_name(base, competing)["status"] == "clarification_required"
    assert resolve_player_name(name, competing)["matches"][0]["player_id"] == 1


@pytest.mark.parametrize("transport", ["", "/stream"])
@pytest.mark.parametrize(
    "question",
    [
        "How has Jalen been playing in the 2025-26 regular season?",
        "Jalen's performance in the 2025-26 regular season",
    ],
)
def test_realistic_partial_overview_wording_clarifies(preview, transport, question):  # noqa: F811
    client, evidence, _, _ = preview
    for row in evidence.rows:
        row["player_name"] = (
            "Jalen Duren" if row["player_id"] == 8101 else "Jalen Brunson"
        )
    result = ask(client, question, transport)
    assert result["status"] == "clarification_required", result
    assert len(result["clarification_options"]) == 2
    assert not result["tables"]


def test_non_name_prose_and_curated_study_aliases():
    roster = [
        {"player_id": 1, "player_name": "Stephen Curry"},
        {"player_id": 2, "player_name": "Avery Finch"},
    ]
    assert (
        prepare_names("Steph threes in 2025-26", roster)[0]
        == "Stephen Curry threes in 2025-26"
    )
    for text in [
        "Assist leaders this season",
        "Avery Finch points on Christmas Day 2025",
        "Avery Finch points on Thanksgiving Day",
    ]:
        assert prepare_names(text, roster)[0] == text


@pytest.mark.parametrize("status", ["no_observations", "unsupported_coverage"])
def test_non_success_keeps_identity_disclosure(status):
    def callback(text, selected):
        prepare_names(text, [{"player_id": 1, "player_name": "Avery Finch"}])
        return {"status": status, "answer": "No verified observations in this scope."}

    result = run_name_matching(
        SimpleNamespace(conversation_store=None),
        "Avrey Finch points",
        None,
        None,
        callback,
    )
    assert "Using Avery Finch" in result["answer"]
    assert result["name_resolutions"][0]["player_name"] == "Avery Finch"


@pytest.mark.parametrize("transport", ["", "/stream"])
def test_identity_error_keeps_source_metadata_and_tool_completion(preview, transport):  # noqa: F811
    from app.main import get_repository

    client, evidence, _, _ = preview
    repo = client.app.dependency_overrides[get_repository]()
    repo._governed_warehouse.load = lambda seasons: (
        {
            "capture": {"sql": "PRIVATE", "row_count": 4},
            "coverage_notes": ["Synthetic partial coverage."],
            "review_metadata": {"partial": True},
        },
        evidence,
    )
    result = ask(client, "Tom Brady points per game in 2025-26", transport)
    assert result["status"] == "not_found"
    assert "Synthetic partial coverage." in result["assumptions"]
    assert result["source_coverage"] == {"partial": True}
    assert result["source_query"] == {"row_count": 4}
    assert result["tool_calls"][0]["status"] == "not_found"
    assert result["semantic_plan"]["model_calls"] == 0


def test_different_player_reply_supersedes_pending_question():
    from app.agent.conversation import InMemoryConversationStore

    store = InMemoryConversationStore()
    store.set_pending_clarification(
        "different",
        question="Avery points",
        query_plan={"name_matching": True, "mention": "Avery"},
        candidates=[{"player_id": 1, "player_name": "Avery Finch"}],
    )
    seen = []

    def callback(text, selected):
        seen.append(text)
        return {"status": "ok", "answer": text}

    run_name_matching(
        SimpleNamespace(conversation_store=store),
        "Blake Reed",
        None,
        "different",
        callback,
    )
    assert seen == ["Blake Reed"]
    assert store.get_pending_clarification("different") is None


def test_legacy_fallback_extracts_names_without_question_or_team_words():
    from app.agent.planner import _extract_player_mentions

    for question in [
        "How did Jalen Duren play against the Lakers?",
        "Did Jalen Duren play against the Lakers?",
    ]:
        assert [name.lower() for name in _extract_player_mentions(question)] == [
            "jalen duren"
        ]


def test_selected_duplicate_is_revalidated_after_season_reload(monkeypatch):
    from app.agent.conversation import InMemoryConversationStore
    from app.agent.semantic_answer import SemanticAsk
    from tests.test_comparison_wording import facts

    source = facts()
    source.rows.extend(
        {**r, "player_id": 9999} for r in list(source.rows) if r["player_id"] == 8101
    )
    loads, executed = [], []

    def load(seasons):
        loads.append(seasons)
        return {"capture": {}}, source

    def planner(*args, **kwargs):
        return {
            "status": "query",
            "model_calls": 0,
            "message": "",
            "queries": [
                {
                    "metric": "pts",
                    "operation": "summary",
                    "aggregation": "average",
                    "season": "2025-26",
                    "seasons": ["2024-25", "2025-26"],
                    "season_type": "Regular Season",
                    "window": "date_range",
                    "start_date": "2024-11-01",
                    "as_of": "2025-11-30",
                    "player_name": "Avery Finch",
                }
            ],
        }

    def execute(plan, evidence, players):
        executed.append([p["player_id"] for p in players])
        return {
            "status": "unsupported_coverage",
            "message": "Synthetic stop after roster validation.",
        }

    monkeypatch.setattr("app.agent.semantic_answer.plan_question", planner)
    monkeypatch.setattr("app.agent.semantic_answer.execute_plan", execute)
    service = SemanticAsk(
        SimpleNamespace(season="2025-26", agent_conversation_max_turns=6),
        SimpleNamespace(load=load),
        InMemoryConversationStore(),
    )
    first = service.answer(
        "Avery Finch points per game",
        client=None,
        model="fixture",
        conversation_id="reload",
    )
    assert first["status"] == "clarification_required"
    result = service.answer(
        "Avery Finch",
        client=None,
        model="fixture",
        conversation_id="reload",
        selected_player={"player_id": 8101, "player_name": "Avery Finch"},
    )
    assert result["status"] == "unsupported_coverage", result
    assert any("2024-25" in seasons for seasons in loads)
    assert executed and 8101 in executed[0] and 9999 not in executed[0]


def test_availability_choice_uses_candidate_season_team():
    from app.agent.name_matching import (
        PlayerNameError,
        identity_payload,
        player_display_name,
    )

    player = {
        "player_id": 1,
        "player_name": "Avery Finch",
        "team_abbr": "NEW",
        "observed_teams": ["OLD"],
    }
    payload = identity_payload(
        PlayerNameError(
            "Avery", {"status": "clarification_required", "matches": [player]}
        ),
        "Avery points",
        "fixture",
    )
    assert payload["clarification_options"][0]["team_abbr"] == "OLD"
    assert player_display_name(player) == "Avery Finch · OLD"


def test_legacy_identity_error_reports_actual_model_attempts():
    from app.agent.name_matching import PlayerNameError

    trace = SimpleNamespace(model_calls=1, outcome=None)

    def callback(question, selected):
        raise PlayerNameError("Tom Brady", {"status": "not_found", "matches": []})

    payload = run_name_matching(
        SimpleNamespace(conversation_store=None),
        "Tom Brady",
        None,
        None,
        callback,
        trace=trace,
    )
    assert payload["semantic_plan"]["model_calls"] == 1


def test_study_accepts_curated_nickname(tmp_path):
    from tests.test_teammate_ask import setup, study

    data = study()
    data["scope"]["focal_player_name"] = "Stephen Curry"
    agent, client = setup(tmp_path, data=data)
    result = agent.answer(
        "Using the teammate study, is Steph's assist difference significant when Other Player is out?"
    )
    assert result["study_status"] == "answered", result
    assert result["tables"]
    assert "Stephen Curry" in str(client.requests)


def test_nonidentity_clarification_keeps_visible_coverage_note(preview, monkeypatch):  # noqa: F811
    from app.main import get_repository

    client, evidence, _, _ = preview
    repo = client.app.dependency_overrides[get_repository]()
    repo._governed_warehouse.load = lambda seasons: (
        {"capture": {}, "coverage_notes": ["Synthetic partial coverage."]},
        evidence,
    )
    monkeypatch.setattr(
        "app.agent.semantic_answer.plan_question",
        lambda *a, **k: {
            "status": "clarification_required",
            "model_calls": 0,
            "message": "Choose a metric.",
            "queries": [],
        },
    )
    result = ask(client, "Avery Finch points per game in 2025-26", "")
    assert result["status"] == "clarification_required", result
    assert "Synthetic partial coverage." in result["answer"]


@pytest.mark.parametrize(
    "question, expected",
    [
        (
            "Michael Porter dunks per game in 2025-26",
            "Michael Porter Jr. dunks per game in 2025-26",
        ),
        (
            "Michael Porter unusualmetric per game",
            "Michael Porter Jr. unusualmetric per game",
        ),
        ("Jaren Jackson blocked shots", "Jaren Jackson Jr. blocked shots"),
    ],
)
def test_suffix_rewrite_never_consumes_metric_words(question, expected):
    roster = [
        {"player_id": 1, "player_name": "Michael Porter Jr."},
        {"player_id": 2, "player_name": "Jaren Jackson Jr."},
    ]
    assert prepare_names(question, roster)[0] == expected


@pytest.mark.parametrize(
    "question, expected",
    [
        (
            "Show box scores for Avery Finch in 2025-26",
            "Show box scores for Avery Finch in 2025-26",
        ),
        (
            "Is there any game where Avery Finch scored 30 points?",
            "Is there any game where Avery Finch scored 30 points?",
        ),
        ("Steph dunks this season", "Stephen Curry dunks this season"),
    ],
)
def test_query_grammar_surrounding_names_is_preserved(question, expected):
    roster = [
        {"player_id": 1, "player_name": "Avery Finch"},
        {"player_id": 2, "player_name": "Stephen Curry"},
    ]
    assert prepare_names(question, roster)[0] == expected


@pytest.mark.parametrize("transport", ["", "/stream"])
def test_identity_resume_preserves_raw_relative_date(preview, transport, monkeypatch):  # noqa: F811
    client, _, store, _ = preview
    from datetime import date

    monkeypatch.setattr(
        "app.agent.time_defaults.semantic_today", lambda: date(2026, 10, 22)
    )
    monkeypatch.setattr(
        "app.agent.semantic_answer.plan_question",
        lambda *a, **k: {
            "status": "query",
            "model_calls": 0,
            "message": "",
            "queries": [
                {
                    "metric": "pts",
                    "operation": "summary",
                    "aggregation": "average",
                    "season": "2025-26",
                    "season_type": "Regular Season",
                    "window": "season_to_date",
                    "player_name": "Avery Finch",
                }
            ],
        },
    )
    question = "Avery points per game last season"
    first = ask(client, question, transport, conversation_id="raw-date")
    assert first["status"] == "clarification_required", first
    assert store.get_pending_clarification("raw-date").question == question
    selected = first["clarification_options"][0]
    result = ask(
        client,
        selected["player_name"],
        transport,
        conversation_id="raw-date",
        selected_player_id=selected["player_id"],
        selected_player_name=selected["player_name"],
    )
    assert result["status"] == "ok", result
    assert any("last season" in a for a in result["assumptions"]), result


def test_rookie_absent_from_older_roster_keeps_no_observations(monkeypatch):
    from app.agent.semantic_answer import SemanticAsk
    from tests.test_comparison_wording import facts

    current, older = facts(), facts()
    current.rows[:] = [
        r
        for r in current.rows
        if not (r["player_id"] == 8101 and r["season_type"] == "Playoffs")
    ]
    older.rows[:] = [
        {**r, "season": "2024-25"} for r in older.rows if r["player_id"] != 8101
    ]
    from dataclasses import replace

    older = replace(
        older,
        covered_scopes=frozenset(
            ("2024-25", phase) for phase in ("Regular Season", "Playoffs")
        ),
        data_through={
            ("2024-25", phase): "2025-05-01" for phase in ("Regular Season", "Playoffs")
        },
        rows=[
            {**r, "game_date": str(int(r["game_date"][:4]) - 1) + r["game_date"][4:]}
            for r in older.rows
        ],
    )
    loads = []

    def load(seasons):
        loads.append(seasons)
        return {"capture": {}}, current if "2025-26" in seasons else older

    def planner(*args, **kwargs):
        return {
            "status": "query",
            "model_calls": 0,
            "message": "",
            "queries": [
                {
                    "metric": "pts",
                    "operation": "summary",
                    "aggregation": "average",
                    "season": "2025-26",
                    "season_type": "Playoffs",
                    "window": "season_to_date",
                    "player_name": "Avery Finch",
                }
            ],
        }

    monkeypatch.setattr("app.agent.semantic_answer.plan_question", planner)
    service = SemanticAsk(
        SimpleNamespace(season="2025-26", agent_conversation_max_turns=6),
        SimpleNamespace(load=load),
    )
    result = service.answer(
        "Avery Finch playoff points per game", client=None, model="fixture"
    )
    assert result["status"] == "no_observations", result
    assert ["2024-25"] in loads
    assert "No matching player" not in result["answer"]
