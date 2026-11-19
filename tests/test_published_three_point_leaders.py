from types import SimpleNamespace

import pytest
from app.agent.semantics import Evidence
from app.main import app, get_repository, get_settings
from app.repository import BigQueryWarehouseRepository
from app.serving.three_point_leaders import (
    asks_for_three_point_leaders,
    leader_answer,
    validate_leader_source,
)
from fastapi.testclient import TestClient
from tests.test_semantic_migration import settings

QUESTION = "Who is leading the league in 3PTs this season?"


def league():
    phase, season = "Regular Season", "2026-27"
    scope = dict(season=season, phase=phase, first="2026-10-20", last="2026-10-20")
    from scripts.build_synthetic_core_replay import synthetic_bundle

    accepted = synthetic_bundle()["accepted"]
    base = accepted["appearances"][0]
    games, rows = [], []
    clubs = ["A" + chr(65 + i // 26) + chr(65 + i % 26) for i in range(30)]
    for i in range(0, 30, 2):
        game_id = f"0022600{i:03d}"
        games.append(
            dict(game_id=game_id, home_team_abbr=clubs[i], away_team_abbr=clubs[i + 1])
        )
        for n in (i, i + 1):
            rows.append(
                dict(
                    base,
                    game_id=game_id,
                    game_date=scope["last"],
                    season=season,
                    season_type=phase,
                    player_id=1000 + n,
                    player_name=f"Player {n}",
                    team_abbr=clubs[n],
                    opponent_abbr=clubs[i + 1 if n == i else i],
                    fg3m=12 if n == 0 else 1,
                    fg3a=15,
                )
            )
    return (
        scope,
        dict(games=games, appearances=rows),
        dict(fct_player_game_stats=[dict(r) for r in rows]),
    )


def test_certification_checks_all_teams_dates_and_makes():
    scope, accepted, models = league()
    validate_leader_source(scope, accepted, models)
    with pytest.raises(ValueError, match="opening night"):
        validate_leader_source(dict(scope, first="2026-10-21"), accepted, models)
    models["fct_player_game_stats"][0]["fg3m"] -= 1
    with pytest.raises(ValueError, match="differ"):
        validate_leader_source(scope, accepted, models)
    with pytest.raises(ValueError, match="full league"):
        validate_leader_source(
            scope, dict(accepted, games=accepted["games"][:-1]), models
        )


@pytest.mark.parametrize(
    "question",
    [
        "Who is leading the league in rebounds this season?",
        "Who leads the NBA in 3PM in the playoffs?",
        "Who led the NBA in threes in 2024-25?",
    ],
)
def test_other_scopes_do_not_select_the_capability(question):
    assert not asks_for_three_point_leaders(question, "2026-27")


def test_general_ask_uses_published_totals_and_refuses_lost_authority(monkeypatch):
    scope, _, models = league()
    service_settings = settings(scope["season"])
    repo = BigQueryWarehouseRepository(service_settings, client=SimpleNamespace())
    pid = "pub_0123456789abcdef"
    proof = Evidence(
        rows=models["fct_player_game_stats"],
        covered_scopes=frozenset({(scope["season"], scope["phase"])}),
        source="validated weekly publication",
        snapshot_id=pid,
        data_through={(scope["season"], scope["phase"]): scope["last"]},
        complete=True,
        cohort_complete=False,
    )
    repo.active_three_point_leaders = lambda: leader_answer(
        proof, dict(publication_id=pid, scope=scope)
    )
    monkeypatch.setitem(
        app.dependency_overrides, get_settings, lambda: service_settings
    )
    monkeypatch.setitem(app.dependency_overrides, get_repository, lambda: repo)
    from app.agent.conversation import InMemoryConversationStore

    store = InMemoryConversationStore()
    monkeypatch.setattr("app.main._season_conversation_store", lambda _: store)
    with TestClient(app) as client:

        def ask():
            response = client.post(
                "/api/agent/ask",
                json={"question": QUESTION, "conversation_id": "leader-test"},
            )
            assert response.status_code == 200
            return response.json()

        result = ask()
        assert result["publication_id"] == pid and result["status"] == "ok"
        assert result["tables"][0]["rows"][0] == [1, "Player 0", "12.0", 12, 1]

        (turn,) = store.get_turns("leader-test", max_turns=1)
        assert turn.question == QUESTION
        assert turn.context["metrics"] == ["fg3m"] and turn.context["players"] == []
        assert turn.context["scope"]["as_of"] == scope["last"]

        def revoked():
            raise ValueError("revoked")

        repo.active_three_point_leaders = revoked
        result = ask()
        assert result["status"] == "unsupported_coverage" and not result["tables"]


def test_active_reader_requires_positive_authority_before_returning():
    from fastapi import FastAPI
    from scripts.serve_production_candidate import attach_publication

    app = FastAPI()
    envelope = dict(
        manifest=dict(
            publication_id="pub_0123456789abcdef", scope={"season": "2026-27"}
        ),
        binding={},
    )
    core = SimpleNamespace(
        release=lambda token: envelope,
        application=lambda token: SimpleNamespace(
            state=SimpleNamespace(three_point_leaders=lambda: {"status": "ok"})
        ),
        authority=SimpleNamespace(check=lambda *_: False),
    )
    from app.serving.production_workers import PublicationWorkers
    from google.api_core.exceptions import ServiceUnavailable

    core.workers = PublicationWorkers()
    attach_publication(app, core, lambda: "production")
    with TestClient(app) as client:
        with pytest.raises(ValueError, match="authority"):
            app.state.active_three_point_leaders()
        core.authority.check = lambda *_: True
        assert app.state.active_three_point_leaders() == {"status": "ok"}

        async def exhausted():
            async with core.workers.requests:
                import anyio

                return await anyio.to_thread.run_sync(
                    app.state.active_three_point_leaders
                )

        core.workers.requests.total_tokens = 1
        with pytest.raises(ValueError, match="unavailable"):
            client.portal.call(exhausted)

        def cold_failure(*_):
            raise ServiceUnavailable("upstream internal detail")

        core.application = cold_failure
        with pytest.raises(ValueError, match="^Publication reader unavailable$"):
            app.state.active_three_point_leaders()


def test_rejects_boolean_in_materialized_three_point_counts():
    scope, accepted, models = league()
    models["fct_player_game_stats"][1]["fg3m"] = True
    with pytest.raises(ValueError, match="differ"):
        validate_leader_source(scope, accepted, models)
