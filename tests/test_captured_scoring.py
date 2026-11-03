from dataclasses import replace

import pytest
from app.agent.captured_scoring import (
    answer_captured_scoring,
    scoring_request,
    scoring_summary,
)
from app.agent.espn_review_policy import PinnedESPNSource
from app.agent.semantic_answer import render_answer
from app.agent.semantic_serving import SemanticWarehouse
from app.agent.semantics import Query, SemanticError, run_query

from test_espn_review_policy import policy


@pytest.mark.parametrize(
    "text",
    [
        "Who are the top scorer this season",
        "Who are the top scorers this season?",
        "What are the top score this season",
        "Top 50 scorers this season",
    ],
)
def test_common_scoring_language_requests_metric_and_source_choice(text):
    args = scoring_request(text, "2026-27")
    assert args and args["aggregation"] is None and not args["captured"]


@pytest.mark.parametrize(
    "text",
    [
        "Top scorers last season",
        "Top scorers this season in playoffs",
        "Top scorers this season for GSW",
        "Top scorers excluding Curry this season",
        "Top scorers this season per 36",
        "Top scorers in captured ESPN games 2025-26 regular season",
        "Top scorers this season for rookies",
    ],
)
def test_unsupported_filters_or_scope_are_never_discarded(text):
    assert scoring_request(text, "2026-27") is None


def source():
    p = policy(allowed_player_ids={10, 11})
    return SemanticWarehouse(
        PinnedESPNSource(object(), project="test-project", source_policy=p)
    )


def test_choose_average_or_total_and_keep_provenance_and_no_league_rank():
    wh = source()
    first = answer_captured_scoring(
        "Who are the top scorer this season", wh, "2026-27", render_answer
    )
    assert first["status"] == "clarification_required" and len(first["followups"]) == 2
    assert first["source_coverage"]["provider"] == "espn"
    assert first["semantic_plan"]["model_calls"] == 0
    for followup, aggregation in zip(first["followups"], ["average", "total"]):
        response = answer_captured_scoring(followup, wh, "2026-27", render_answer)
        assert response["status"] == "ok"
        e = response["semantic_evidence"]
        assert e["scope"]["aggregation"] == aggregation
        assert e["rows"][0]["player_id"] == 11
        assert e["rows"][0]["value"] == 3
        assert all(r["rank"] is None and r["percentile"] is None for r in e["rows"])
        assert response["captured_scoring"]["coverage"] == "captured_games_only"
        assert e["scope"]["min_games"] == 1  # early season does not require five games
        assert "does not establish the NBA league leader" in response["answer"]
        assert response["source_coverage"]["excluded_identity_rows"] == 1


def test_identity_allowlist_and_ordinary_league_rank_guard_remain():
    wh = SemanticWarehouse(
        PinnedESPNSource(object(), project="test-project", source_policy=policy())
    )
    with pytest.raises(SemanticError, match="identities"):
        answer_captured_scoring(
            "Who are the top scorer this season", wh, "2026-27", render_answer
        )
    _, e = source().load(["2026-27"])
    with pytest.raises(SemanticError, match="complete cohort"):
        run_query(
            e,
            Query(
                metric="pts",
                season="2026-27",
                aggregation="average",
                operation="rank",
                min_games=1,
            ),
        )


def test_scoring_orders_all_captured_players_and_preserves_raw_ties_and_missingness():
    _, e = source().load(["2026-27"])
    rows = []
    base = e.rows[0]
    # More than the ordinary summary display limit: never truncate IDs first.
    for n in range(125):
        rows.append(
            {**base, "player_id": n + 1, "game_id": "espn:shared", "pts": float(n)}
        )
    rows[-2]["pts"] = 124.04
    rows[-1]["pts"] = 124.01
    rows[0]["pts"] = None
    result = scoring_summary(replace(e, rows=rows), "2026-27", "average", 10)
    assert [r["player_id"] for r in result["rows"][:2]] == [124, 125]
    assert result["rows"][0]["display_value"] == result["rows"][1]["display_value"]
    assert result["cohort"]["min_games"] == 1
    assert result["cohort"]["excluded_players"] == 1


def test_average_qualification_excludes_small_sample_outlier():
    _, e = source().load(["2026-27"])
    base = e.rows[0]
    rows = [{**base, "player_id": 10, "game_id": "espn:0", "pts": 100}]
    rows += [
        {**base, "player_id": 11, "game_id": f"espn:{n}", "pts": 20} for n in range(5)
    ]
    result = scoring_summary(replace(e, rows=rows), "2026-27", "average", 10)
    assert result["cohort"]["min_games"] == 4
    assert [r["player_id"] for r in result["rows"]] == [11]
    totals = scoring_summary(replace(e, rows=rows), "2026-27", "total", 10)
    assert totals["cohort"]["min_games"] == 1
    assert [r["value"] for r in totals["rows"]] == [100, 100]


def test_source_scoring_ui_json_sse_choices_and_claims(installed, monkeypatch):
    import json
    from dataclasses import replace

    from app.agent import intelligence as host
    from app.agent.runtime_policy import configure_runtime_policy
    from app.repository import BigQueryWarehouseRepository
    from tests.test_runtime_policy import policy as runtime

    client, authority, sent, _, main = installed
    config = replace(
        main.app.dependency_overrides[main.get_settings](),
        ask_local_espn_dev=True,
        ask_claim_enforcement="enforce",
        performance_cache_prewarm_enabled=False,
    )
    p = policy(allowed_player_ids={10, 11})
    configure_runtime_policy(runtime(authority, source_policy=p))
    repo = BigQueryWarehouseRepository(config, client=object())
    main.app.dependency_overrides[main.get_settings] = lambda: config
    main.app.dependency_overrides[main.get_repository] = lambda: repo
    monkeypatch.setattr(
        host,
        "plan_question",
        lambda *a, **k: pytest.fail("Narrow source choice needs no model"),
    )
    response = client.post(
        "/api/agent/ask", json={"question": "Who are the top scorer this season"}
    )
    assert response.status_code == 200, response.text
    first = response.json()
    assert first["status"] == "clarification_required" and first["followups"]
    selected = {"question": first["followups"][0]}
    result = client.post("/api/agent/ask", json=selected)
    assert result.status_code == 200 and result.json()["status"] == "ok", result.text
    assert result.json()["tables"][0]["rows"][0][0] == "Blake Baker"
    stream = client.post("/api/agent/ask/stream", json=selected)
    final = next(
        json.loads(line[6:])["payload"]
        for line in stream.text.splitlines()
        if line.startswith("data: ") and json.loads(line[6:]).get("type") == "final"
    )
    assert final["status"] == "ok" and final["answer"] == result.json()["answer"]
    assert final["tables"] == result.json()["tables"]
    assert authority.attempts == 0 and not sent


from tests import test_scoped_ask_runtime as scoped_tests  # noqa: E402

installed = scoped_tests.installed


def test_all_players_share_the_capture_window_anchor():
    _, e = source().load(["2026-27"])
    rows = [{**e.rows[0], "game_date": "2026-10-23"}, e.rows[1]]
    e = replace(
        e, rows=rows, data_through={("2026-27", "Regular Season"): "2026-10-23"}
    )
    result = scoring_summary(e, "2026-27", "total", 10)
    assert result["scope"]["as_of"] == "2026-10-23"
    assert result["scope"]["window_end"] == "2026-10-23"
