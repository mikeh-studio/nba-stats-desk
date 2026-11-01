import hashlib
import json
from dataclasses import replace
from datetime import date
from types import SimpleNamespace

import pytest
from app.agent.espn_review_policy import ESPNReviewPolicy, PinnedESPNSource
from app.agent.semantic_serving import source_players
from app.agent.semantic_source import snapshot_evidence
from app.agent.semantics import Query, SemanticError, run_query
from app.agent.source_policy import SourcePolicy
from scripts.espn_corrections import normalize, validate
from scripts.espn_sources import event_game
from tests.test_runtime_policy import policy as runtime

from test_espn_dev_source import bundle


def current_bundle():
    b = bundle()
    payload = json.loads(b["games"][0]["payload"])["input"]
    payload["header"]["season"]["year"] = 2027
    payload["header"]["competitions"][0]["date"] = "2026-10-22T23:00Z"
    game = event_game(payload["header"], "2026-27", date(2026, 10, 22))
    c = normalize(payload, game, "box")
    b["games"][0].update(
        season="2026-27",
        payload=json.dumps(c),
        box_revision=validate(c),
        result_revision=c["result_revision"],
    )
    return b


def policy(data=None, **changes):
    raw = json.dumps(current_bundle() if data is None else data)
    args = dict(
        relations=(
            (
                "2026-27",
                "test-project.nba_gamebook_dev_espn_only.espn_layer_player_box_scores",
            ),
        ),
        phases=("Regular Season",),
        snapshot_at="2026-10-28T23:00:00+00:00",
        allowed_player_ids=frozenset({11}),
        bundle_json=raw,
        bundle_sha256=hashlib.sha256(raw.encode()).hexdigest(),
    )
    args.update(changes)
    return ESPNReviewPolicy(**args)


def settings(**changes):
    args = dict(
        agent_history_enabled=False,
        agent_trace_enabled=False,
        ask_local_espn_dev=True,
        ask_serving_mode="off",
        agent_position_context_path=None,
    )
    args.update(changes)
    return SimpleNamespace(**args)


def test_pin_is_stable_partial_and_uses_no_warehouse_or_profile_access():
    p = policy()
    source = PinnedESPNSource(object(), project="test-project", source_policy=p)
    a = source.capture(["2026-27"])
    assert a == source.capture(["2026-27"])
    assert a["capture"]["query_count"] == 0
    evidence = snapshot_evidence(a)
    assert not evidence.cohort_complete and evidence.rows[0]["game_id"].startswith(
        "espn:"
    )
    assert [r["player_id"] for r in source_players(evidence, source_policy=p)] == [11]
    with pytest.raises(SemanticError):
        run_query(
            evidence,
            Query(
                metric="pts",
                season="2026-27",
                aggregation="average",
                operation="rank",
                min_games=1,
            ),
        )


@pytest.mark.parametrize(
    "changes",
    [
        dict(bundle_sha256="0" * 64),
        dict(
            relations=(
                (
                    "2025-26",
                    "test-project.nba_gamebook_dev_espn_only.espn_layer_player_box_scores",
                ),
            )
        ),
        dict(relations=(("2026-27", "test-project.nba_gold.fct_player_game_stats"),)),
        dict(phases=("Playoffs",)),
        dict(max_rows=1),
    ],
)
def test_wrong_scope_checksum_relation_and_caps_fail(changes):
    with pytest.raises(SemanticError):
        policy(**changes)


@pytest.mark.parametrize(
    "defect", ["unaccepted", "dependency", "hash", "identity", "wrong_season"]
)
def test_pin_revalidates_publication_dependencies_and_identity(defect):
    b = current_bundle()
    if defect == "unaccepted":
        b["games"][0]["box_status"] = "stale"
    if defect == "dependency":
        b["games"][0]["result_revision"] = "wrong"
    if defect == "hash":
        b["games"][0]["box_revision"] = "wrong"
    if defect == "identity":
        b["mappings"][1]["nba_player_id"] = "10"
    if defect == "wrong_season":
        b["games"][0]["season"] = "2025-26"
    with pytest.raises(SemanticError):
        policy(b)


def test_runtime_opt_in_requires_exact_validated_policy_and_keeps_other_guards():
    runtime(source_policy=policy()).validate_settings(settings())
    with pytest.raises(ValueError):
        runtime().validate_settings(settings())
    for field, value in [
        ("agent_history_enabled", True),
        ("agent_trace_enabled", True),
        ("ask_serving_mode", "serve"),
        ("ask_local_espn_dev", False),
    ]:
        with pytest.raises(ValueError):
            runtime(source_policy=policy()).validate_settings(
                settings(**{field: value})
            )


def test_no_sql_scope_widening_or_warehouse_fallback():
    p = policy()
    with pytest.raises(SemanticError):
        p.authorize(
            sql="SELECT 1", config=None, seasons=["2026-27"], phases=["Regular Season"]
        )
    for seasons, phases in [
        (["2025-26"], ["Regular Season"]),
        (["2026-27"], ["Playoffs"]),
        (["2025-26", "2026-27"], ["Regular Season"]),
    ]:
        with pytest.raises(SemanticError):
            p.capture(seasons, phases)
    with pytest.raises(SemanticError):
        SourcePolicy(
            relations=p.relations,
            phases=p.phases,
            snapshot_at=p.snapshot_at,
            allowed_player_ids=p.allowed_player_ids,
            source_mode="espn_pinned_review",
        )


def test_real_routes_use_pinned_source_with_json_sse_parity_and_budget(
    installed, monkeypatch
):
    from app.agent import intelligence as host
    from app.agent import intelligence_contract as api
    from app.agent.runtime_policy import configure_runtime_policy
    from app.repository import BigQueryWarehouseRepository
    from tests.test_scoped_ask_runtime import Provider

    client, authority, sent, _, main = installed
    prior = main.app.dependency_overrides[main.get_settings]()
    config = replace(
        prior, ask_local_espn_dev=True, performance_cache_prewarm_enabled=False
    )
    p = policy()
    configure_runtime_policy(runtime(authority, source_policy=p))
    repo = BigQueryWarehouseRepository(config, client=object())
    main.app.dependency_overrides[main.get_settings] = lambda: config
    main.app.dependency_overrides[main.get_repository] = lambda: repo

    class CurrentProvider(Provider):
        async def plan(self, context, intent, *, prompts, budget, model):
            await model.generate_plan(
                system_prompt=prompts.planning_system, user_payload="{}"
            )
            return api.QueryPlan(
                "metrics",
                "query",
                (
                    api.QueryProposal(
                        "pts",
                        api.Scope("2026-27", "Regular Season"),
                        "summary",
                        "average",
                        player_name="Blake Baker",
                        aggregation_source="explicit",
                    ),
                ),
            )

    monkeypatch.setattr(host, "_load", lambda _: CurrentProvider())
    question = {
        "question": "Summarize Blake Baker scoring in the 2026-27 regular season using points per game"
    }
    normal = client.post("/api/agent/ask", json=question)
    assert normal.status_code == 200 and normal.json()["status"] == "ok", normal.text
    stream = client.post("/api/agent/ask/stream", json=question)
    final = next(
        json.loads(line[6:])["payload"]
        for line in stream.text.splitlines()
        if line.startswith("data: ") and json.loads(line[6:]).get("type") == "final"
    )
    assert final["status"] == "ok"
    assert normal.json()["answer"] == final["answer"]
    assert "ESPN" in final["answer"] and "3" in final["answer"]
    assert len(sent) == authority.attempts == 2
    exhausted = client.post("/api/agent/ask", json=question)
    assert exhausted.status_code == 502 and len(sent) == 2


from tests import test_scoped_ask_runtime as scoped_tests  # noqa: E402

installed = scoped_tests.installed
