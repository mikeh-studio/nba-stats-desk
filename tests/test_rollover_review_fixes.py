"""Identity-aware fallback and parsed archive dependency regressions."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from app import main
from app.agent.semantics import SemanticError
from app.availability import load_availability
from app.seasons import DEFAULT_SEASON
from scripts.archive_season_copy import archive_mapping, ordered_views, rebind_view
from tests.test_api import _test_settings, build_client
from tests.test_availability import bundle as bundle
from tests.test_research import evidence
from tests.test_teammate_ask import Client


def latest_evidence(old, player_ids):
    rows = [
        {
            **r,
            "season": DEFAULT_SEASON,
            "game_date": str(r["game_date"]).replace("2025", "2026"),
        }
        for r in old.rows
        if r["player_id"] in player_ids
    ]
    return replace(
        old,
        rows=rows,
        covered_scopes=frozenset({(DEFAULT_SEASON, "Regular Season")}),
        data_through={(DEFAULT_SEASON, "Regular Season"): "2026-11-07"},
    )


@pytest.mark.parametrize("ids", [[1], [1, 2]])
def test_research_http_fallback_requires_every_requested_player(monkeypatch, ids):
    old = evidence()
    newest = latest_evidence(old, {2})
    calls = []

    def load(settings, repo, season):
        calls.append(season)
        return newest if season == DEFAULT_SEASON else old

    monkeypatch.setattr(main, "load_research_evidence", load)
    client = build_client(settings=_test_settings(research_snapshot_path="fixture"))
    try:
        response = client.post(
            "/api/research/breakdown",
            json={"player_ids": ids, "metrics": ["pts"], "home_away": "home"},
        )
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["scope"]["season"] == "2025-26"
        assert result["scope"]["home_away"] == "home"
        assert [p["player_id"] for p in result["players"]] == ids
        assert all(p["games"] for p in result["players"])
        assert result["season_fallback"] == {
            "requested": DEFAULT_SEASON,
            "used": "2025-26",
        }
        assert calls == [DEFAULT_SEASON, "2025-26"]
    finally:
        main.app.dependency_overrides.clear()


@pytest.mark.parametrize("explicit", ["body", "url", "dates"])
def test_research_identity_gap_preserves_explicit_scope(monkeypatch, explicit):
    old = evidence()
    calls = []

    def load(settings, repo, season):
        calls.append(season)
        return latest_evidence(old, {2}) if season == DEFAULT_SEASON else old

    monkeypatch.setattr(main, "load_research_evidence", load)
    client = build_client(settings=_test_settings(research_snapshot_path="fixture"))
    payload = {"player_ids": [1], "metrics": ["pts"]}
    if explicit == "body":
        payload["season"] = DEFAULT_SEASON
    if explicit == "dates":
        payload.update(start="2026-11-01", end="2026-11-07")
    url = "/api/research/breakdown" + (
        f"?season={DEFAULT_SEASON}" if explicit == "url" else ""
    )
    try:
        assert client.post(url, json=payload).status_code == 422
        assert calls == [DEFAULT_SEASON]
    finally:
        main.app.dependency_overrides.clear()


@pytest.mark.parametrize("code", ["access_denied", "invalid_evidence"])
def test_research_endpoint_source_errors_never_fall_back(monkeypatch, code):
    calls = []

    def load(settings, repo, season):
        calls.append(season)
        raise SemanticError(code, "Synthetic source failure")

    monkeypatch.setattr(main, "load_research_evidence", load)
    client = build_client(settings=_test_settings(research_snapshot_path="fixture"))
    try:
        assert (
            client.post("/api/research/breakdown", json={"player_ids": [1]}).status_code
            == 422
        )
        assert calls == [DEFAULT_SEASON]
    finally:
        main.app.dependency_overrides.clear()


@pytest.mark.parametrize("filters", ["", " against BOS at home"])
@pytest.mark.parametrize("new_player", [999, 811])
@pytest.mark.parametrize("transport", ["/api/agent/ask", "/api/agent/ask/stream"])
def test_pair_fallback_ignores_unrelated_or_unverified_new_season(
    bundle, monkeypatch, new_player, transport, filters
):
    import json

    from app.agent import availability_ask

    loaded = list(load_availability(str(bundle[0])))
    old = loaded[1]
    newest = {
        **old.rows[0],
        "season": DEFAULT_SEASON,
        "game_date": "2026-11-01",
        "game_id": "new-game",
        "player_id": new_player,
        "player_name": "Unrelated Player" if new_player == 999 else "Avery Finch",
    }
    loaded[1] = replace(
        old,
        rows=[*old.rows, newest],
        covered_scopes=old.covered_scopes | {(DEFAULT_SEASON, "Regular Season")},
        data_through={
            **old.data_through,
            (DEFAULT_SEASON, "Regular Season"): "2026-11-01",
        },
    )
    loaded[2] = {
        **loaded[2],
        (DEFAULT_SEASON, "new-game", "ATL"): {
            **next(iter(loaded[2].values())),
            "season": DEFAULT_SEASON,
            "game_id": "new-game",
            "game_date": "2026-11-01",
            "scheduled_start_utc": "2026-11-01T20:00:00Z",
        },
    }
    loaded[4] = {**loaded[4], (DEFAULT_SEASON, "new-game", new_player): newest}
    monkeypatch.setattr(
        availability_ask, "load_availability", lambda *args: tuple(loaded)
    )
    model = Client({})
    client = build_client(
        settings=_test_settings(
            openai_api_key="fixture",
            research_availability_path=str(bundle[0]),
            agent_rate_limit_per_minute=0,
            agent_rate_limit_daily=0,
        ),
        agent_client=model,
    )
    try:
        question = (
            "How did Avery Finch play while Blake Reed was out in the regular season"
            + filters
            + "?"
        )
        response = client.post(transport, json={"question": question})
        assert response.status_code == 200
        result = (
            response.json()
            if not transport.endswith("stream")
            else next(
                json.loads(line[6:])["payload"]
                for line in response.text.splitlines()
                if line.startswith("data: ")
                and json.loads(line[6:]).get("type") == "final"
            )
        )
        assert result["status"] == "ok", result
        assert result["availability_scope"]["season"] == "2025-26"
        assert result["availability_scope"]["phase"] == "Regular Season"
        if filters:
            assert result["availability_scope"]["opponent"] == "BOS"
            assert result["availability_scope"]["home_away"] == "home"
        assert result["season_fallback"]["used"] == "2025-26"
        assert model.calls == 0
        explicit = client.post(
            "/api/agent/ask", json={"question": question + f" in {DEFAULT_SEASON}"}
        ).json()
        assert "season_fallback" not in explicit
        assert explicit.get("availability_scope", {}).get("season") != "2025-26"
    finally:
        main.app.dependency_overrides.clear()


def view_inventory(child_sql, parent_sql="SELECT 1 AS value"):
    mapping = archive_mapping("example-project", "2025-26")
    src, dest = "example-project.nba_gold", mapping["example-project.nba_gold"]
    return mapping, [
        (
            SimpleNamespace(table_id=name, table_type="VIEW", view_query=sql),
            "US",
            src,
            dest,
        )
        for name, sql in (("child", child_sql), ("parent", parent_sql))
    ]


@pytest.mark.parametrize(
    "reference",
    [
        "nba_gold.parent",
        "`nba_gold.parent`",
        "`example-project`.nba_gold.parent",
        "`example-project.nba_gold.parent`",
    ],
)
def test_archive_orders_every_supported_reference_form(reference):
    mapping, inventory = view_inventory(f"SELECT * FROM {reference}")
    ordered = ordered_views(inventory, mapping)
    assert [name.rsplit(".", 1)[-1] for name, _ in ordered] == ["parent", "child"]
    assert "example-project.nba_gold_2025_26.parent" in ordered[-1][1]


def test_archive_ignores_cte_aliases_comments_and_literals_but_finds_nested_tables():
    mapping, inventory = view_inventory(
        "WITH p AS (SELECT * FROM nba_gold.parent) SELECT 'nba_gold.keep_literal' AS txt FROM p -- `external.dataset.fake`\n"
    )
    ordered = ordered_views(inventory, mapping)
    assert ordered[-1][0].endswith(".child")
    assert "'nba_gold.keep_literal'" in ordered[-1][1]
    assert "nba_gold_2025_26.parent" in ordered[-1][1]
    assert "nba_gold_2025_26.keep_literal" not in ordered[-1][1]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM other_project.dataset.live",
        "SELECT * FROM `other_project.dataset.live`",
        "SELECT * FROM nba_gold.missing",
        "SELECT * FROM nba_gold.parent; SELECT 2",
        "SELECT * FROM (",
    ],
)
def test_archive_rejects_external_unknown_or_unparseable_references_before_copy(sql):
    mapping, inventory = view_inventory(sql)
    with pytest.raises(ValueError):
        ordered_views(inventory, mapping)


def test_archive_detects_cycles_with_unquoted_references():
    mapping, inventory = view_inventory(
        "SELECT * FROM nba_gold.parent", "SELECT * FROM nba_gold.child"
    )
    with pytest.raises(ValueError, match="Cyclic"):
        ordered_views(inventory, mapping)


@pytest.mark.parametrize(
    "qualifier",
    [
        "nba_gold.parent",
        "`example-project`.nba_gold.parent",
        "`example-project.nba_gold.parent`",
    ],
)
def test_archive_rebinds_qualified_columns_with_their_table(qualifier):
    from sqlglot import exp, parse_one

    mapping = archive_mapping("example-project", "2025-26")
    rebound = rebind_view(f"SELECT {qualifier}.value FROM nba_gold.parent", mapping)
    query = parse_one(rebound, read="bigquery")
    table = query.find(exp.Table)
    assert table.catalog == "example-project"
    assert table.db == "nba_gold_2025_26"
    column = query.find(exp.Column)
    assert (
        column.db == "nba_gold_2025_26"
        or column.table == "example-project.nba_gold_2025_26.parent"
    )


def test_archive_keeps_alias_and_struct_column_qualifiers():
    mapping = archive_mapping("example-project", "2025-26")
    rebound = rebind_view(
        "SELECT t.value, t.nba_gold.parent FROM nba_gold.parent AS t", mapping
    )
    assert "t.value" in rebound
    assert "t.nba_gold.parent" in rebound
    assert "nba_gold_2025_26.parent" in rebound
