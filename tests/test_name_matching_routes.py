"""Shared name policy through real JSON/SSE routes, using synthetic evidence."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from app.agent.conversation import InMemoryConversationStore
from app.agent.name_matching import PlayerNameError, prepare_names
from app.agent.player_suggestions import resolve_player_name
from fastapi.testclient import TestClient
from tests.test_availability import (
    bundle,  # noqa: F401 - shared synthetic snapshot fixture
)
from tests.test_comparison_wording import facts
from tests.test_semantic_migration import settings


@pytest.fixture
def preview(monkeypatch, bundle):  # noqa: F811 - imported pytest fixture
    from app import main
    from app.agent import research_ask
    from app.availability import load_availability
    from app.repository import BigQueryWarehouseRepository

    evidence = facts()
    for row in evidence.rows:
        row["home_away"] = "HOME" if row["game_id"] == "fictional-0" else "AWAY"
    config = replace(
        settings("2025-26"),
        performance_cache_prewarm_enabled=False,
        ask_claim_enforcement="enforce",
        research_availability_path=str(bundle[0]),
    )
    repo = BigQueryWarehouseRepository(config, client=SimpleNamespace())
    repo._governed_warehouse = SimpleNamespace(
        load=lambda seasons: ({"capture": {}}, evidence)
    )
    store = InMemoryConversationStore()
    monkeypatch.setattr(main, "_season_conversation_store", lambda season: store)
    monkeypatch.setattr(research_ask, "load_research_evidence", lambda *args: evidence)
    previous = dict(main.app.dependency_overrides)
    main.app.dependency_overrides.update(
        {
            main.get_settings: lambda: config,
            main.get_repository: lambda: repo,
            main.get_agent_client: lambda: object(),
        }
    )
    with TestClient(main.app) as client:
        yield client, evidence, store, load_availability(str(bundle[0]))
    main.app.dependency_overrides.clear()
    main.app.dependency_overrides.update(previous)


def ask(client, text, transport, **extra):
    response = client.post(
        "/api/agent/ask" + transport, json={"question": text, **extra}
    )
    assert response.status_code == 200, response.text
    if not transport:
        return response.json()
    events = [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    return next(e["payload"] for e in events if e.get("type") == "final")


ROUTES = [
    ("appearances", "How many games did {name} play in the 2025-26 regular season?"),
    (
        "player_split",
        "Compare {name} points home vs away in the 2025-26 regular season",
    ),
    (
        "league_baseline",
        "Compare {name} points against league average in the 2025-26 regular season",
    ),
    ("similarity", "Who is similar to {name} over the last 2 games in 2025-26?"),
    (
        "availability",
        "How did {name} play when Blake Reed was out in the 2025-26 regular season?",
    ),
]


@pytest.mark.parametrize("transport", ["", "/stream"])
@pytest.mark.parametrize("route,template", ROUTES)
def test_typo_preserves_answer_and_source_scope(preview, transport, route, template):
    client, _, _, _ = preview
    canonical = ask(client, template.format(name="Avery Finch"), transport)
    corrected = ask(client, template.format(name="Avrey Finch"), transport)
    assert corrected["claim_validation"]["errors"] == []
    assert corrected["ask_route"]["route"] == route
    assert corrected.get("status", "ok") == canonical.get("status", "ok")
    assert canonical["tables"], canonical["answer"]
    assert corrected["tables"] == canonical["tables"]
    assert corrected["semantic_evidence"] == canonical["semantic_evidence"]
    assert "Using Avery Finch" in corrected["answer"]
    assert corrected["name_resolutions"][0]["method"] == "minor_typo"


@pytest.mark.parametrize("transport", ["", "/stream"])
@pytest.mark.parametrize(
    "route,template",
    ROUTES
    + [
        ("governed_metrics", "{name} points per game in 2025-26"),
        ("research", "Research {name} points at home in 2025-26"),
    ],
)
@pytest.mark.parametrize(
    "name,status", [("Avery", "clarification_required"), ("Tom Brady", "not_found")]
)
def test_ambiguous_or_unavailable_blocks_before_model(
    preview, transport, route, template, name, status
):
    client, _, _, _ = preview
    payload = ask(client, template.format(name=name), transport)
    assert payload["ask_route"]["route"] == route
    assert payload["status"] == status, payload["answer"]
    assert not payload["tables"] and payload["semantic_evidence"] is None
    assert bool(payload["clarification_options"]) == (
        status == "clarification_required"
    )
    assert payload["semantic_plan"]["model_calls"] == 0


@pytest.mark.parametrize("transport", ["", "/stream"])
@pytest.mark.parametrize("route,template", ROUTES)
@pytest.mark.parametrize("typed", [False, True])
def test_clarification_resumes_original_analysis(
    preview, transport, route, template, typed
):
    client, _, store, _ = preview
    q = template.format(name="Avery")
    first = ask(client, q, transport, conversation_id="choose")
    choice = first["clarification_options"][0]
    extra = (
        {}
        if typed
        else {
            "selected_player_id": choice["player_id"],
            "selected_player_name": choice["player_name"],
        }
    )
    final = ask(
        client,
        choice["player_name"] if typed else q,
        transport,
        conversation_id="choose",
        **extra,
    )
    assert final["ask_route"]["route"] == route
    assert final["tables"], final["answer"]
    assert "as you selected" in final["answer"]
    assert store.get_pending_clarification("choose") is None


def test_duplicate_names_require_choice_with_distinct_ids(preview):
    client, evidence, _, _ = preview
    evidence.rows.extend(
        {**r, "player_id": 9999} for r in list(evidence.rows) if r["player_id"] == 8101
    )
    q = "How many games did Avery Finch play in 2025-26?"
    first = ask(client, q, "", conversation_id="duplicate")
    assert {p["player_id"] for p in first["clarification_options"]} == {8101, 9999}
    assert all(p["label"] == "Avery Finch" for p in first["clarification_options"])
    assert all(p["team_abbr"] == "AAA" for p in first["clarification_options"])
    final = ask(
        client,
        q,
        "",
        conversation_id="duplicate",
        selected_player_id=8101,
        selected_player_name="Avery Finch",
    )
    assert final["tables"]
    assert "Avery Finch · AAA" in final["answer"]
    assert "8101" not in final["answer"]


def test_alias_does_not_hide_full_typo_or_partial_competitor():
    players = [
        {"player_id": 1, "player_name": "LeBron James", "aliases": ["lebron"]},
        {"player_id": 2, "player_name": "Jalen Duren", "aliases": ["jalen"]},
        {"player_id": 3, "player_name": "Jalen Brunson"},
    ]
    text, _ = prepare_names("How many games did LeBron Jams play?", players)
    assert text == "How many games did LeBron James play?"
    assert resolve_player_name("Jalen", players)["status"] == "clarification_required"
    for name in ["Tom Brady", "tom brady", "Jalen Zqxv"]:
        with pytest.raises(PlayerNameError) as error:
            prepare_names(f"{name} points per game", players)
        assert error.value.result["status"] == "not_found"


def test_no_player_queries_and_team_metric_words_are_untouched():
    roster = [{"player_id": 1, "player_name": "Jalen Duren"}]
    for text in [
        "Who leads the NBA in points?",
        "Who won MVP in 2025-26?",
        "Show league average Fantasy Score against the Boston Celtics",
    ]:
        assert prepare_names(text, roster)[0] == text


@pytest.mark.parametrize("transport", ["", "/stream"])
@pytest.mark.parametrize("operation", ["summary", "game_log", "rank", "percentile"])
def test_planned_metrics_receive_canonical_identity(preview, transport, operation):
    from app import main
    from tests.test_semantic_migration import Model

    client, _, _, _ = preview
    model = Model(
        player_name="resolved_player_8101",
        operation=operation,
        min_games=1 if operation in {"rank", "percentile"} else None,
    )
    main.app.dependency_overrides[main.get_agent_client] = lambda: model
    question = "{name} points per game in the 2025-26 regular season"
    if operation == "game_log":
        question = "{name} points game log in the 2025-26 regular season"
    canonical = ask(client, question.format(name="Avery Finch"), transport)
    corrected = ask(client, question.format(name="Avrey Finch"), transport)
    assert corrected["status"] == "ok", corrected["answer"]
    assert corrected["tables"] == canonical["tables"]
    assert corrected["semantic_evidence"] == canonical["semantic_evidence"]
    assert "Using Avery Finch" in corrected["answer"]
    assert "Avrey" not in json.dumps(model.content)


@pytest.mark.parametrize("transport", ["", "/stream"])
def test_research_planner_receives_correction_and_preserves_query(
    preview, transport, monkeypatch
):
    from app import main
    from app.agent import research_ask
    from tests.test_descriptive_research import plan
    from tests.test_teammate_ask import Client

    client, evidence, _, _ = preview
    evidence = replace(
        evidence,
        rows=[{**r, "home_away": r["home_away"].lower()} for r in evidence.rows],
    )
    monkeypatch.setattr(research_ask, "load_research_evidence", lambda *args: evidence)
    model = Client({**plan(player_ids=[8101], metrics=["pts"]), "kind": "descriptive"})
    main.app.dependency_overrides[main.get_agent_client] = lambda: model
    template = "Research {name} points at home in 2025-26"
    canonical = ask(client, template.format(name="Avery Finch"), transport)
    corrected = ask(client, template.format(name="Avrey Finch"), transport)
    assert corrected["ask_route"]["route"] == "research_breakdown", corrected["answer"]
    assert corrected["tables"]
    assert corrected["tables"] == canonical["tables"]
    assert "Using Avery Finch" in corrected["answer"]
    assert "Avrey" not in json.dumps(model.requests[-1])


def test_study_names_resolve_before_model_and_keep_role_order(tmp_path):
    from tests.test_teammate_ask import setup, study

    data = study()
    data["scope"].update(focal_player_name="Avery Finch", teammate_name="Blake Reed")
    agent, model = setup(tmp_path, data=data)
    payload = agent.answer(
        "Using the teammate study, compare Avrey Finch assists when Blake Reed was out"
    )
    assert payload["study_status"] == "answered"
    assert "Using Avery Finch" in payload["answer"]
    assert model.calls == 1
    assert "Avrey" not in json.dumps(model.requests)
    for name, status in [
        ("Avery", "clarification_required"),
        ("Tom Brady", "not_found"),
    ]:
        payload = agent.answer(
            f"Using the teammate study, compare {name} assists when Blake Reed was out"
        )
        assert payload["status"] == status
        assert model.calls == 1
        assert not payload["tables"]


def test_tool_resolver_uses_same_policy_before_display_limit():
    from app.agent.player_resolver import PlayerResolver
    from app.agent.tools import StatsToolRunner

    players = [
        {"player_id": 1, "player_name": "Jalen Duren"},
        {"player_id": 2, "player_name": "Jalen Brunson"},
        {"player_id": 3, "player_name": "Tony Bradley"},
    ]
    repo = SimpleNamespace(
        list_agent_player_candidates=lambda **kw: list(players),
        search_players=lambda *a, **kw: list(players),
    )
    resolver = PlayerResolver(repo)
    assert resolver.resolve("Jalen Duran").player.player_id == 1
    assert resolver.resolve("Jalen").status == "ambiguous"
    assert resolver.resolve("Tom Brady").status == "not_found"
    tool = StatsToolRunner(repo)
    assert tool.resolve_player("Jalen Duran")["player"]["player_name"] == "Jalen Duren"
    assert tool.resolve_player("Jalen")["status"] == "ambiguous"
    assert tool.resolve_player("Tom Brady")["matches"] == []
    players.append({"player_id": 4, "player_name": "Jalen Duron"})
    assert resolver.resolve("Jalen Duran", limit=1).status == "ambiguous"
    assert tool.resolve_player("Jalen Duran", limit=1)["status"] == "ambiguous"
