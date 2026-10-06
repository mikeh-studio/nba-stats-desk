from types import SimpleNamespace

import pytest
from app.agent.award_lookup import (
    CATALOG,
    load_catalog,
    resolve_award_question,
    wants_award,
)
from app.agent.conversation import InMemoryConversationStore
from app.agent.semantic_answer import SemanticAsk
from app.agent.semantics import COMPONENTS, Evidence, SemanticError
from tests.test_semantic_migration import settings


def warehouse():
    calls = []

    def load(seasons):
        calls.append(seasons)
        rows = []
        coverage = {}
        for season in seasons:
            day = f"{int(season[:4]) + 1}-01-01"
            coverage[(season, "Regular Season")] = day
            people = {
                record["player_id"]: record["player_name"]
                for award in CATALOG.values()
                for record in [award["records"].get(season)]
                if record
            }
            for player_id, player_name, points in [
                *[(pid, name, 20) for pid, name in people.items()],
                (999, "Other Player", 80),
            ]:
                rows.append(
                    dict(
                        dict.fromkeys(COMPONENTS, 0),
                        season=season,
                        season_type="Regular Season",
                        game_id="game-" + season,
                        game_date=day,
                        player_id=player_id,
                        player_name=player_name,
                        team_abbr="AAA",
                        opponent_abbr="BBB",
                        home_away="HOME",
                        pts=points,
                        min=30,
                        fgm=10,
                        fga=20,
                    )
                )
        return {"capture": {}}, Evidence(
            rows,
            frozenset(coverage),
            "synthetic",
            "award-test",
            coverage,
            complete=True,
        )

    return SimpleNamespace(load=load, calls=calls)


def test_original_question_looks_up_winner_then_computes_stats_without_model():
    source = warehouse()
    store = InMemoryConversationStore()
    store.set_pending_clarification(
        "award", question="Who won rookie of the year? How did he play?", query_plan={}
    )
    agent = SemanticAsk(settings("2025-26"), source, store)
    result = agent.answer(
        "Who won rookie of the year? How did he play?",
        client=None,
        model="none",
        conversation_id="award",
    )
    assert result["status"] == "ok"
    assert "Cooper Flagg won the 2025-26" in result["answer"]
    assert result["player_profile"]["player"]["player_id"] == 1642843
    assert result["semantic_plan"]["model_calls"] == 0
    assert result["award_evidence"]["source_url"].startswith("https://www.nba.com/")
    assert result["conversation_context"]["players"][0]["player_id"] == 1642843
    assert source.calls == [["2025-26", "2024-25"]]
    assert store.get_pending_clarification("award") is None


@pytest.mark.parametrize(
    "wording,season",
    [
        ("Who won Rookie of the Year in 2024-25? How did he play?", "2024-25"),
        ("Who was the 2024 ROY winner? Show his performance.", "2023-24"),
        ("Who won ROTY last season? How did he perform?", "2024-25"),
    ],
)
def test_explicit_season_wins(wording, season):
    source = warehouse()
    result = SemanticAsk(settings("2025-26"), source).answer(
        wording, client=None, model="none"
    )
    assert result["status"] == "ok"
    assert result["award_evidence"]["season"] == season
    assert len(source.calls) == 1 and source.calls[0][0] == season


@pytest.mark.parametrize(
    "question",
    [
        "Who won rookie of the year in 2026-27?",
        "Who will win rookie of the year?",
        "Who won rookie of the year? How did he play against BOS?",
        "Who won rookie of the year in 2024-25 and 2023-24?",
        "Who won WNBA rookie of the year?",
        "Who won rookie of the year? Why did he win?",
    ],
)
def test_no_guessing_or_silently_dropped_conditions(question):
    with pytest.raises(SemanticError):
        resolve_award_question(question, "2025-26")


def test_missing_stats_still_reports_verified_award_without_fallback():
    def missing(seasons):
        assert seasons[0] == "2024-25"
        raise SemanticError("unsupported_coverage", "Season source unavailable")

    result = SemanticAsk(settings("2025-26"), SimpleNamespace(load=missing)).answer(
        "Who won rookie of the year in 2024-25? How did he play?",
        client=None,
        model="none",
    )
    assert "Stephon Castle won" in result["answer"]
    assert result["answerability"] == "partial"
    assert result["semantic_evidence"] is None


@pytest.mark.parametrize(
    "question",
    [
        "Who won rookie of the year? How did he play?",
        "Who won MVP? How did he play?",
        "Who won DPOY? How did he play?",
        "Who won Clutch Player of the Year? How did he play?",
        "Who won Finals MVP?",
    ],
)
def test_award_json_and_stream_endpoints(question):
    import json

    from app.main import app, get_agent_client, get_repository, get_settings
    from app.repository import BigQueryWarehouseRepository
    from fastapi.testclient import TestClient

    config = settings("2025-26")
    repo = BigQueryWarehouseRepository(config, client=SimpleNamespace())
    repo._governed_warehouse = warehouse()
    old = dict(app.dependency_overrides)
    app.dependency_overrides.update(
        {
            get_settings: lambda: config,
            get_repository: lambda: repo,
            get_agent_client: lambda: object(),
        }
    )
    try:
        with TestClient(app) as client:
            body = {"question": question}
            direct = client.post("/api/agent/ask?season=2025-26", json=body)
            assert direct.status_code == 200
            payload = direct.json()
            streamed = client.post("/api/agent/ask/stream?season=2025-26", json=body)
            assert streamed.status_code == 200
            events = [
                json.loads(line[6:])
                for line in streamed.text.splitlines()
                if line.startswith("data: ")
            ]
            final = next(event for event in events if event.get("type") == "final")
            result = final.get("payload", final.get("response", final))
            assert result["status"] == payload["status"] == "ok"
            assert result["award_evidence"] == payload["award_evidence"]
            assert result["semantic_evidence"] == payload["semantic_evidence"]
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(old)


@pytest.mark.parametrize("key", list(CATALOG))
@pytest.mark.parametrize("season", ["2023-24", "2024-25", "2025-26"])
def test_catalog_awards_share_lookup_and_performance_flow(key, season):
    entry = CATALOG[key]
    record = entry["records"][season]
    question = (
        f"Who won {entry['label']} in {season}? Show his regular-season performance."
    )
    result = SemanticAsk(settings("2025-26"), warehouse()).answer(
        question, client=None, model="none"
    )
    assert result["status"] == "ok", result["answer"]
    assert result["award_evidence"]["player_id"] == record["player_id"]
    assert result["award_evidence"]["award_key"] == key
    assert result["player_profile"]["player"]["player_id"] == record["player_id"]
    assert result["semantic_plan"]["model_calls"] == 0
    assert record["source_url"] in result["answer"]


@pytest.mark.parametrize(
    "key,alias", [(k, a) for k, e in CATALOG.items() for a in e["aliases"]]
)
def test_all_aliases_resolve_without_warehouse(key, alias):
    def unexpected(_):
        pytest.fail("Winner-only queries must not depend on performance coverage")

    result = SemanticAsk(settings("2025-26"), SimpleNamespace(load=unexpected)).answer(
        f"Who won {alias}?", client=None, model="none"
    )
    assert result["status"] == "ok"
    assert result["award_evidence"]["award_key"] == key
    assert result["semantic_evidence"] is None


@pytest.mark.parametrize(
    "question",
    [
        "Who won All-Star MVP?",
        "Who won Western Conference Finals MVP?",
        "Who won NBA Cup MVP?",
        "Who won Coach of the Year?",
        "Who won the Sportsmanship award?",
        "Who won the MVP and DPOY?",
        "Who will win MVP?",
        "Who deserved MVP?",
        "Who won MVP in 2027?",
        "Who won MVP? Why did he win?",
        "Who won MVP? How did he play against BOS?",
        "Who won MVP in 2024-2026?",
        "Who won WNBA MVP?",
    ],
)
def test_other_awards_and_conditions_never_become_player_clarifications(question):
    def unexpected(_):
        pytest.fail("Invalid requests must not load the warehouse")

    assert wants_award(question)
    result = SemanticAsk(settings("2025-26"), SimpleNamespace(load=unexpected)).answer(
        question, client=None, model="none"
    )
    assert result["status"] in (
        "unsupported_coverage",
        "unsupported_scope",
        "clarification_required",
    )
    assert result["clarification_options"] == []
    assert "provide" not in result["answer"].lower()


def test_finals_performance_is_not_silently_replaced_by_whole_playoffs():
    result = SemanticAsk(settings("2025-26"), warehouse()).answer(
        "Who won Finals MVP? How did he play?", client=None, model="none"
    )
    assert result["answerability"] == "partial"
    assert result["award_evidence"]["award_key"] == "finals_mvp"
    assert "Finals-only" in result["answer"]
    assert result["semantic_evidence"] is None


def test_lookup_identity_replaces_stale_player_and_supports_followup():
    store = InMemoryConversationStore()
    agent = SemanticAsk(settings("2025-26"), warehouse(), store)
    result = agent.answer(
        "Who won DPOY in 2024-25?",
        client=None,
        model="none",
        conversation_id="shared",
        selected_player={"player_id": 999, "player_name": "Other Player"},
    )
    assert result["conversation_context"]["players"][0]["player_id"] == 1630596
    result = agent.answer(
        "Show his performance", client=None, model="none", conversation_id="shared"
    )
    assert result["status"] == "ok", result["answer"]
    assert result["player_profile"]["player"]["player_id"] == 1630596


def test_catalog_rejects_duplicate_season_and_untrusted_source(tmp_path):
    import json
    from pathlib import Path

    data = json.loads(Path("app/agent/award_catalog.json").read_text())
    path = tmp_path / "catalog.json"
    data["awards"][0]["records"].append(data["awards"][0]["records"][0])
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        load_catalog(path)
    data["awards"][0]["records"].pop()
    data["awards"][0]["records"][0]["source_url"] = "https://example.com/unreviewed"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        load_catalog(path)


def test_finals_lookup_followup_does_not_invent_round_coverage():
    store = InMemoryConversationStore()
    agent = SemanticAsk(settings("2025-26"), warehouse(), store)
    agent.answer(
        "Who won Finals MVP?", client=None, model="none", conversation_id="finals"
    )
    result = agent.answer(
        "Show his performance", client=None, model="none", conversation_id="finals"
    )
    assert result["answerability"] == "partial"
    assert "Finals-only" in result["answer"]
    result = agent.answer(
        "Show his regular-season performance",
        client=None,
        model="none",
        conversation_id="finals",
    )
    assert result["status"] == "ok", result["answer"]
    assert result["player_profile"]["player"]["player_id"] == 1628973
