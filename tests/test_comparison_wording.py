"""Comparison routing and identity retention with independently authored facts."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from app.agent.player_comparison import comparison_sides
from app.agent.semantics import COMPONENTS, Evidence
from fastapi.testclient import TestClient
from tests.support.semantic import settings


@pytest.mark.parametrize(
    "question",
    [
        "How does Avery Finch compare to Blake Reed?",
        "Avery Finch compared with Blake Reed in 2025-26",
        "Compare Avery Finch to Blake Reed",
        "How did Avery Finch play last season? How it compares to Blake Reed?",
        "How did Avery Finch played last season? How he compare to Blake Reed?",
    ],
)
def test_conversational_comparison_selects_two_sides(question):
    from app.agent.routes import semantic_shape

    sides = comparison_sides(question)
    assert sides and "Avery Finch" in sides[0] and "Blake Reed" in sides[1]
    assert semantic_shape(question) == "player_comparison"


@pytest.mark.parametrize(
    "baseline",
    [
        "last season",
        "his previous 5 games",
        "2024-25",
        "November 2025",
        "November",
        "his career average",
        "the league average",
        "his rookie season",
        "his best season",
        "an average player",
        "a year ago",
    ],
)
def test_conversational_temporal_baselines_are_not_player_comparisons(baseline):
    assert comparison_sides(f"How does Avery Finch compare to {baseline}?") is None


def facts():
    rows = []
    for pid, name, team, values in [
        (8101, "Avery Finch", "AAA", [(10, 2, 3), (20, 4, 5), (30, 6, 7)]),
        (8202, "Blake Reed", "BBB", [(6, 8, 2), (12, 4, 4), (18, 6, 6)]),
    ]:
        for i, (pts, reb, ast) in enumerate(values):
            rows.append(
                {
                    **dict.fromkeys(COMPONENTS, 0),
                    "season": "2025-26",
                    "season_type": "Regular Season" if i < 2 else "Playoffs",
                    "game_id": f"fictional-{i}",
                    "game_date": ["2025-11-01", "2025-11-02", "2026-05-01"][i],
                    "player_id": pid,
                    "player_name": name,
                    "team_abbr": team,
                    "opponent_abbr": "BBB" if team == "AAA" else "AAA",
                    "pts": pts,
                    "reb": reb,
                    "ast": ast,
                    "fgm": pts // 2,
                    "fga": 20,
                    "min": 30,
                }
            )
    scopes = frozenset(("2025-26", p) for p in ("Regular Season", "Playoffs"))
    return Evidence(
        rows,
        scopes,
        "fictional",
        "comparison-wording",
        dict.fromkeys(scopes, "2026-05-01"),
        complete=True,
    )


@pytest.mark.parametrize("suffix", ["", "/stream"])
@pytest.mark.parametrize(
    "variant", ["both", "grammar", "regular", "missing", "ambiguous", "same"]
)
def test_comparison_wording_at_actual_endpoints(suffix, variant):
    from app.main import app, get_agent_client, get_repository, get_settings
    from app.repository import BigQueryWarehouseRepository

    evidence = facts()
    right = (
        "Unknown Player"
        if variant == "missing"
        else "Avery Finch"
        if variant == "same"
        else "Blake Reed"
    )
    if variant == "ambiguous":
        evidence.rows.extend(
            {**row, "player_id": 8303}
            for row in list(evidence.rows)
            if row["player_id"] == 8202
        )
    question = f"How did Avery Finch play last season? How it compares to {right}?"
    if variant == "grammar":
        question = f"How did Avery Finch played last season? How he compare to {right}?"
    if variant == "regular":
        question = "How does Avery Finch compare with Blake Reed in the 2025-26 regular season?"
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
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/agent/ask" + suffix, json={"question": question}
            )
            assert response.status_code == 200
            if suffix:
                events = [
                    json.loads(line[6:])
                    for line in response.text.splitlines()
                    if line.startswith("data: ")
                ]
                data = next(e["payload"] for e in events if e.get("type") == "final")
            else:
                data = response.json()
            assert data["ask_route"]["route"] == "player_comparison"
            if variant in {"missing", "ambiguous", "same"}:
                assert data["status"] == (
                    "not_found" if variant == "missing" else "clarification_required"
                )
                assert not data["tables"]
                return
            assert data["status"] == "ok"
            assert [p["player"]["player_id"] for p in data["player_profiles"]] == [
                8101,
                8202,
            ]
            ev = data["semantic_evidence"]
            assert ev["kind"] == "player_comparison"
            assert ev["scope"]["seasons"] == ["2025-26"]
            assert ev["scope"]["start"] == "2025-07-01"
            assert ev["scope"]["end"] == "2026-06-30"
            assert ev["scope"]["phases"] == (
                ["Regular Season"]
                if variant == "regular"
                else ["Regular Season", "Playoffs"]
            )
            metrics = {m["key"]: m for m in ev["metrics"]}
            assert metrics["pts"]["left"]["value"] == (
                15 if variant == "regular" else 20
            )
            assert metrics["pts"]["right"]["value"] == (
                9 if variant == "regular" else 12
            )
            assert metrics["pts"]["delta"] == (6 if variant == "regular" else 8)
            assert metrics["ast"]["delta"] == 1
            assert metrics["pts"]["left"]["valid_games"] == (
                2 if variant == "regular" else 3
            )
            assert data["semantic_plan"]["model_calls"] == 0
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


def test_fox_spelling_variants_only_resolve_to_observed_identity():
    from app.agent.performance_overview import resolve_overview_player
    from app.agent.semantic_serving import source_players

    evidence = facts()
    for row in evidence.rows:
        if row["player_id"] == 8202:
            row["player_name"] = "De'Aaron Fox"
    players = source_players(evidence)
    assert [
        p["player_id"]
        for p in resolve_overview_player("Dearon Fox", players, evidence.rows)
    ] == [8202]
    players = source_players(
        replace(evidence, rows=[r for r in evidence.rows if r["player_id"] != 8202])
    )
    assert not resolve_overview_player("Dearon Fox", players, evidence.rows)
