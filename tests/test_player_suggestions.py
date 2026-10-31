"""Fictional identity/evidence tests for confirmation before statistics."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from app.agent.player_suggestions import match_player_name, suggest_players
from fastapi.testclient import TestClient
from tests.support.semantic import settings
from tests.test_comparison_wording import facts


def test_suggestions_are_ranked_bounded_and_do_not_match_question_words():
    players = [
        {"player_id": i, "player_name": f"Avery Finch{suffix}", "aliases": []}
        for i, suffix in enumerate(["", "a", "b", "c", "d", "e", "f"], 1)
    ]
    suggested = suggest_players("How did Avrey Finch play last season?", players)
    assert len(suggested) == 5
    assert suggested[0]["player_id"] == 1
    assert suggested == suggest_players(
        "How did Avrey Finch play last season?", list(reversed(players))
    )
    assert not suggest_players("How did he play last season?", players)
    assert not suggest_players("Qzxv Zqxx performance", players)
    assert not suggest_players("Avrey Finch", [])


def test_minor_typo_policy_and_competing_identities():
    players = [
        {"player_id": 1, "player_name": "Jalen Duren"},
        {"player_id": 2, "player_name": "Jalen Brunson"},
        {"player_id": 3, "player_name": "Tony Bradley"},
    ]
    result = match_player_name("How did Jalen Duran play last season?", players)
    assert result["status"] == "ok"
    assert result["resolution"]["player_id"] == 1
    assert "Using Jalen Duren" in result["notice"]
    assert match_player_name("Jalen", players)["status"] == "clarification_required"
    assert match_player_name("Tom Brady", players)["status"] == "not_found"
    assert match_player_name("Jalen Zqxv", players)["status"] == "not_found"
    for extra in ["Jalen Duren", "Jalen Duron"]:
        competing = players + [{"player_id": 4, "player_name": extra}]
        assert (
            match_player_name("Jalen Duran", competing)["status"]
            == "clarification_required"
        )
    assert match_player_name("Jalen Duran", players[1:])["status"] == "not_found"


def test_stale_confirmation_does_not_silently_switch_to_new_typo_match():
    from app.agent.player_comparison import build_comparison, comparison_scope
    from app.agent.semantic_serving import source_players

    evidence = facts()
    question = "Avrey Finch vs Blake Reed in 2025-26"
    result = build_comparison(
        question,
        evidence,
        source_players(evidence),
        comparison_scope(question, "2026-27"),
        choices={"0": 999999},
    )
    assert result["status"] == "clarification_required"
    assert not result["tables"]


@pytest.mark.parametrize("suffix", ["", "/stream"])
@pytest.mark.parametrize("kind", ["overview", "comparison", "missing"])
def test_auto_resolve_and_unavailable_endpoints(suffix, kind):
    from app.main import app, get_agent_client, get_repository, get_settings
    from app.repository import BigQueryWarehouseRepository

    evidence = facts()
    config = replace(settings("2026-27"), performance_cache_prewarm_enabled=False)
    repo = BigQueryWarehouseRepository(config, client=SimpleNamespace())
    repo._governed_warehouse = SimpleNamespace(
        load=lambda seasons: ({"capture": {}}, evidence)
    )
    previous = dict(app.dependency_overrides)
    app.dependency_overrides.update(
        {
            get_settings: lambda: config,
            get_repository: lambda: repo,
            get_agent_client: lambda: object(),
        }
    )
    question = "How did Avrey Finch perform in the 2025-26 regular season?"
    if kind == "comparison":
        question = (
            "How did Avrey Finch played last season? How he compare to Blake Reed?"
        )
    elif kind == "missing":
        question = "How did Unrelated Person perform in the 2025-26 regular season?"
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/agent/ask" + suffix, json={"question": question}
            )
            assert response.status_code == 200
            data = (
                response.json()
                if not suffix
                else next(
                    e["payload"]
                    for e in [
                        json.loads(line[6:])
                        for line in response.text.splitlines()
                        if line.startswith("data: ")
                    ]
                    if e.get("type") == "final"
                )
            )
            assert data["semantic_plan"]["model_calls"] == 0
            if kind == "missing":
                assert data["status"] == "not_found"
                assert "No matching player is available" in data["answer"]
                assert not data["tables"] and not data["clarification_options"]
                assert data["semantic_evidence"] is None
                return
            assert data["status"] == "ok"
            assert "Using Avery Finch" in data["answer"]
            assert any("Using Avery Finch" in s for s in data["scope_assumptions"])
            assert data["name_resolutions"][0]["player_id"] == 8101
            if kind == "comparison":
                assert [p["player"]["player_id"] for p in data["player_profiles"]] == [
                    8101,
                    8202,
                ]
            else:
                assert data["player_profile"]["player"]["player_id"] == 8101
            assert data["semantic_evidence"]["scope"]["start"] == "2025-07-01"
            assert data["semantic_evidence"]["scope"]["end"] == "2026-06-30"
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


@pytest.mark.parametrize("suffix", ["", "/stream"])
@pytest.mark.parametrize(
    "kind", ["overview", "comparison", "both_typos", "ambiguous", "typed"]
)
def test_suggestion_confirmation_at_endpoints(suffix, kind):
    from app.main import app, get_agent_client, get_repository, get_settings
    from app.repository import BigQueryWarehouseRepository

    evidence = facts()
    if kind == "ambiguous":
        evidence.rows.extend(
            {**r, "player_id": 8303, "player_name": "Avery French"}
            for r in list(evidence.rows)
            if r["player_id"] == 8101
        )
    question = "How did Avri Finch perform in the 2025-26 regular season?"
    if kind == "ambiguous":
        question = "How did Avery perform in the 2025-26 regular season?"
    if kind in {"comparison", "both_typos", "typed"}:
        right = "Blak Ried" if kind == "both_typos" else "Blake Reed"
        question = f"How did Avri Finch played last season? How he compare to {right}?"
    config = replace(settings("2026-27"), performance_cache_prewarm_enabled=False)
    repo = BigQueryWarehouseRepository(config, client=SimpleNamespace())
    coverage_note = "Synthetic coverage disclosure."
    repo._governed_warehouse = SimpleNamespace(
        load=lambda seasons: (
            {
                "capture": {},
                "coverage_notes": [coverage_note],
                "scope_assumptions": [coverage_note],
            },
            evidence,
        )
    )
    previous = dict(app.dependency_overrides)
    app.dependency_overrides.update(
        {
            get_settings: lambda: config,
            get_repository: lambda: repo,
            get_agent_client: lambda: object(),
        }
    )
    try:
        with TestClient(app) as client:

            def ask(body):
                response = client.post("/api/agent/ask" + suffix, json=body)
                assert response.status_code == 200
                if not suffix:
                    return response.json()
                events = [
                    json.loads(line[6:])
                    for line in response.text.splitlines()
                    if line.startswith("data: ")
                ]
                return next(e["payload"] for e in events if e.get("type") == "final")

            first = ask({"question": question})
            assert first["status"] == "clarification_required"
            assert first["semantic_plan"]["model_calls"] == 0
            assert not first["tables"] and not first["charts"]
            assert first["semantic_evidence"] is None
            assert coverage_note not in first["answer"]
            assert not first.get("scope_assumptions")
            assert coverage_note in first["assumptions"]
            assert [p["player_id"] for p in first["clarification_options"]] == (
                [8101, 8303] if kind == "ambiguous" else [8101]
            )
            body = {
                "question": question,
                "conversation_id": first["conversation_id"],
                "selected_player_id": 8101,
                "selected_player_name": "Avery Finch",
            }
            if kind == "typed":
                body = {
                    "question": "Avery Finch",
                    "conversation_id": first["conversation_id"],
                }
            confirmed = ask(body)
            if kind == "both_typos":
                assert confirmed["status"] == "clarification_required"
                assert confirmed["comparison_choices"] == {"0": 8101}
                assert [p["player_id"] for p in confirmed["clarification_options"]] == [
                    8202
                ]
                assert not confirmed["tables"]
                confirmed = ask(
                    {
                        **body,
                        "selected_player_id": 8202,
                        "selected_player_name": "Blake Reed",
                    }
                )
            assert confirmed["status"] == "ok"
            assert coverage_note in confirmed["answer"]
            assert confirmed["semantic_plan"]["model_calls"] == 0
            ev = confirmed["semantic_evidence"]
            assert ev["scope"]["start"] == "2025-07-01"
            assert ev["scope"]["end"] == "2026-06-30"
            if kind in {"comparison", "both_typos", "typed"}:
                assert [
                    p["player"]["player_id"] for p in confirmed["player_profiles"]
                ] == [8101, 8202]
                assert ev["scope"]["phases"] == ["Regular Season", "Playoffs"]
                pts = next(m for m in ev["metrics"] if m["key"] == "pts")
                assert pts["left"]["value"] == 20 and pts["right"]["value"] == 12
            else:
                assert confirmed["player_profile"]["player"]["player_id"] == 8101
                assert ev["scope"]["phases"] == ["Regular Season"]
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


@pytest.mark.parametrize("comparison", [False, True])
@pytest.mark.parametrize(
    "name,expected",
    [
        ("LeBron Jams", "ok"),
        ("LeBron Zqxv", "not_found"),
    ],
)
def test_short_alias_cannot_discard_unknown_surname(comparison, name, expected):
    from app.agent.performance_overview import build_overview, overview_scope
    from app.agent.player_comparison import build_comparison, comparison_scope
    from app.agent.semantic_serving import source_players

    evidence = facts()
    for row in evidence.rows:
        if row["player_id"] == 8101:
            row["player_name"] = "LeBron James"
    players = source_players(evidence)
    question = f"How did {name} perform in the 2025-26 regular season?"
    if comparison:
        question = f"{name} vs Blake Reed in the 2025-26 regular season"
        result = build_comparison(
            question, evidence, players, comparison_scope(question, "2025-26")
        )
    else:
        result = build_overview(
            question, evidence, players, overview_scope(question, "2025-26")
        )
    assert result["status"] == expected
    if expected == "ok":
        assert "Using LeBron James" in result["answer"]
    else:
        assert not result["tables"]


def test_short_alias_collision_and_selection_use_credible_candidates():
    from app.agent.performance_overview import resolve_overview_player

    players = [
        {"player_id": 1, "player_name": "Jalen Duren", "aliases": ["Jalen"]},
        {"player_id": 2, "player_name": "Jalen Brunson", "aliases": []},
        {"player_id": 3, "player_name": "Blake Reed", "aliases": []},
    ]
    assert len(resolve_overview_player("Jalen performance", players, [])) == 2
    assert not resolve_overview_player(
        "Jalen Duran performance", players, [], {"player_id": 3}
    )
