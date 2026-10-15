"""Independent fixtures for peer membership, rank policy and season provenance."""

from copy import deepcopy
from datetime import date

import pytest
from app.agent.overview_percentiles import add_percentiles
from app.agent.overview_positions import (
    MAPPING_VERSION,
    add_position_comparisons,
    comparison_text,
    peer_question,
    requested_group,
    standing,
    validated_context,
)
from app.agent.performance_overview import (
    build_overview,
    overview_scope,
    wants_overview,
)
from app.agent.semantics import COMPONENTS, Evidence, SemanticError


def fixture(n=20, position="PG"):
    grouped = {
        i: [
            {
                **dict.fromkeys(COMPONENTS, 0),
                "season": "2024-25",
                "season_type": "Regular Season",
                "player_id": i,
                "player_name": f"Example {i}",
                "game_id": str(d),
                "game_date": f"2024-11-{d:02}",
                "pts": i,
                "ast": i,
                "reb": i,
                "min": 1,
            }
            for d in range(1, 6)
        ]
        for i in range(1, n + 1)
    }
    context = dict(
        mapping_version=MAPPING_VERSION,
        approved_seasons=["2024-25"],
        records=[
            dict(
                player_id=i,
                season="2024-25",
                position=position,
                provenance="season_roster",
                source_sha256="a" * 64,
                source_date="2025-06-30",
                source_locator="synthetic roster",
                source_ref="https://example.org/season-2024",
            )
            for i in grouped
        ],
    )
    from app.agent.historical_positions import digest

    context["sha256"] = digest(context)
    return grouped, context


def compare(n=20, mutate=None, question="", position="PG"):
    grouped, context = fixture(n, position)
    if mutate:
        mutate(grouped, context)
        from app.agent.historical_positions import digest

        context["sha256"] = digest(context)
    # Own displayed values are irrelevant to membership: recomputed raw rows.
    m = [
        dict(
            key="pts", value=1, valid_games=5, missing_component_games=0, components={}
        )
    ]
    add_percentiles(m, grouped, 1, 5)
    legacy = deepcopy(m[0])
    scope = dict(
        start=date(2024, 7, 1), end=date(2025, 6, 30), phases=["Regular Season"]
    )
    add_position_comparisons(m, grouped, 1, scope, context, question)
    assert {k: v for k, v in m[0].items() if k != "peer_comparison"} == legacy
    return m[0]


@pytest.mark.parametrize(
    "n,kind,mode",
    [
        (1, "league", "withheld"),
        (2, "league", "withheld"),
        (9, "league", "withheld"),
        (10, "position", "ordinal"),
        (19, "position", "ordinal"),
        (20, "position", "percentile"),
    ],
)
def test_size_boundaries_and_distribution(n, kind, mode):
    p = compare(n)["peer_comparison"]
    assert (p["kind"], p["mode"], p["cohort_size"]) == (kind, mode, n)
    assert p["distribution"]["among_players"]["summary"]["n"] == n
    assert sum(b["count"] for b in p["distribution"]["among_players"]["bins"]) == n
    assert p["rank"] == n
    assert p["percentile"] == (0 if n > 1 else None)


@pytest.mark.parametrize(
    "missing,expected", [(0, "position"), (1, "position"), (2, "league")]
)
def test_coverage_threshold(missing, expected):
    def mutate(g, c):
        for r in c["records"][-missing:] if missing else []:
            r["position"] = "Forward"

    assert compare(mutate=mutate)["peer_comparison"]["kind"] == expected


@pytest.mark.parametrize(
    "position", ["Forward", "Guard-Forward", "Forward-Center", None, []]
)
def test_ambiguous_primary_never_guessed(position):
    p = compare(position=position)["peer_comparison"]
    assert p["kind"] == "league"
    assert p["fallback_reason"] == "ambiguous_position"


def test_historical_provenance_is_required_and_season_keyed():
    for field, value in [
        ("provenance", "current_profile"),
        ("season", "2025-26"),
        ("source_ref", ""),
    ]:

        def mutate(g, c):
            for r in c["records"]:
                r[field] = value

        assert compare(mutate=mutate)["peer_comparison"]["kind"] == "league"
    assert (
        compare(mutate=lambda g, c: c.update(approved_seasons=[]))["peer_comparison"][
            "fallback_reason"
        ]
        == "missing_historical_provenance"
    )


def test_duplicate_mapping_fails_closed():
    g, c = fixture()
    c["records"].append(c["records"][0])
    assert validated_context(c) == ({}, set())


def test_exact_ties_and_all_equal():
    cohort = {1: {"value": 5}, 2: {"value": 5}, 3: {"value": 7}, 4: {"value": 3}}
    assert standing(cohort, 1, "higher") == dict(
        rank=2, tied=True, all_equal=False, percentile=100 / 3
    )
    assert standing(cohort, 1, "lower")["rank"] == 2

    def mutate(g, c):
        for rows in g.values():
            for r in rows:
                r["pts"] = 1

    assert compare(mutate=mutate)["peer_comparison"]["mode"] == "withheld"


def test_missing_components_and_appearance_floor():
    def mutate(g, c):
        g[1][0]["pts"] = None

    assert compare(mutate=mutate)["peer_comparison"]["mode"] == "withheld"

    def mutate(g, c):
        g[1].pop()

    assert (
        compare(mutate=mutate)["peer_comparison"]["fallback_reason"]
        == "ineligible_sample"
    )


def test_no_favorable_cohort_switch_and_explicit_mismatch():
    def mutate(g, c):
        for r in c["records"][10:]:
            r["position"] = "C"
        for rows in list(g.values())[10:]:
            for r in rows:
                r["pts"] = 0

    p = compare(mutate=mutate)["peer_comparison"]
    assert (p["kind"], p["cohort_size"], p["percentile"]) == ("position", 10, 0)
    assert (
        compare(question="among big men")["peer_comparison"]["fallback_reason"]
        == "group_mismatch"
    )


def test_ordinal_uses_conventional_ties():
    m = compare(10)
    m["peer_comparison"].update(rank=4, tied=True)
    assert "tied 4th of 10" in comparison_text(m, "regular-season", "points")


def test_cross_season_cohort_not_pooled_into_position_rank():
    def mutate(g, c):
        g[1][0]["season"] = "2023-24"

    assert (
        compare(mutate=mutate)["peer_comparison"]["fallback_reason"]
        == "multiple_seasons"
    )


@pytest.mark.parametrize(
    "question",
    [
        "How did Jalen Brunson perform in the 2024–25 playoffs compared with his regular season and other guards?",
        "How unusual was Nikola Jokic's playmaking among big men in the 2023–24 regular season?",
        "How did Jayson Tatum's rebounding compare with other wings in the 2024–25 regular season?",
        "How did Stephen Curry's scoring in the 2025–26 regular season compare with the prior season and other guards?",
        "Compare Anthony Davis's first two appearances of 2023–24 with other big men.",
    ],
)
def test_five_questions_route_to_overview(question):
    assert requested_group(question)
    assert wants_overview(question)


def test_peer_clause_does_not_erase_named_opponent():
    q = "Compare Example One with Example Two among guards"
    assert "Example Two" in peer_question(q)
    assert not wants_overview(q)


def test_focused_overview_and_shared_sidebar():
    grouped, c = fixture()
    rows = [r for rs in grouped.values() for r in rs]
    e = Evidence(
        rows,
        frozenset({("2024-25", "Regular Season")}),
        "fixture",
        "hash",
        {("2024-25", "Regular Season"): "2025-06-30"},
        complete=True,
        position_context=c,
    )
    q = "How was Example 1's scoring performance in the 2024-25 regular season among guards?"
    out = build_overview(
        q,
        e,
        [{"player_id": 1, "player_name": "Example 1"}],
        overview_scope(q, "2024-25"),
    )
    ev = out["semantic_evidence"]
    assert ev["presentation"]["profile"] == "scoring"
    assert "qualifying regular-season guards" in out["answer"]
    assert "assists" not in out["answer"]
    assert {f["id"] for f in ev["insights"]} == {
        f["insight_id"] for f in ev["presentation"]["takeaways"]
    }


def test_private_tracking_distinguishes_demand_and_opportunity():
    from app.agent.overview_tracking import overview_events, requested_capabilities

    demand = requested_capabilities("How was Example among guards?")
    assert demand == [
        dict(
            kind="explicit_demand", capability="position_comparison", reason="requested"
        )
    ]
    m = compare()
    payload = dict(
        semantic_evidence=dict(presentation={"profile": "general"}, metrics=[m])
    )
    events = [
        e for e in overview_events(payload) if e["kind"] == "comparison_opportunity"
    ]
    assert len(events) == 1
    assert events[0]["reason"] == "position_available"
    assert (
        not {"question", "answer", "player_id", "rows", "conversation_id"}
        & events[0].keys()
    )
    assert not requested_capabilities("Do not compare with guards.")


def test_small_sample_has_no_repeated_sidebar():
    grouped, c = fixture()
    rows = [r for rs in grouped.values() for r in rs]
    e = Evidence(
        rows,
        frozenset({("2024-25", "Regular Season")}),
        "fixture",
        "hash",
        {("2024-25", "Regular Season"): "2025-06-30"},
        complete=True,
        position_context=c,
    )
    q = "Compare Example 1's first two appearances of 2024-25 with other guards."
    out = build_overview(
        q, e, [dict(player_id=1, player_name="Example 1")], overview_scope(q, "2024-25")
    )
    assert out["semantic_evidence"]["presentation"]["takeaways"] == []
    assert "peer reference shown" not in out["answer"]
    assert out["semantic_evidence"]["scope"]["start"] == "2024-11-01"
    assert out["semantic_evidence"]["scope"]["end"] == "2024-11-02"
    assert all(
        m.get("peer_comparison", {}).get("mode", "withheld") == "withheld"
        for m in out["semantic_evidence"]["metrics"]
    )


@pytest.mark.parametrize(
    "position,group", [("PG", "Guard"), ("SF", "Wing"), ("PF", "Wing"), ("C", "Big")]
)
def test_precise_positions_enable_all_groups(position, group):
    p = compare(position=position)["peer_comparison"]
    assert p["kind"] == "position" and p["group"] == group


def test_windowed_request_keeps_explicit_baseline_intent():
    q = "How was Example's scoring in January 2025 compared with the prior season and other guards?"
    with pytest.raises(SemanticError, match="current window alone"):
        overview_scope(q, "2024-25")


@pytest.mark.parametrize(
    "position,group,word",
    [("PG", "Guard", "guards"), ("SF", "Wing", "wings"), ("C", "Big", "big men")],
)
@pytest.mark.parametrize("suffix", ["", "/stream"])
@pytest.mark.parametrize("baseline_requested", [False, True])
def test_position_group_at_application_endpoints(
    position, group, word, suffix, baseline_requested
):
    import json
    from types import SimpleNamespace

    from app.main import app, get_agent_client, get_repository, get_settings
    from app.repository import BigQueryWarehouseRepository
    from fastapi.testclient import TestClient
    from tests.test_semantic_migration import settings

    grouped, c = fixture(position=position)
    rows = [r for rs in grouped.values() for r in rs]
    e = Evidence(
        rows,
        frozenset({("2024-25", "Regular Season")}),
        "synthetic position fixture",
        "hash",
        {("2024-25", "Regular Season"): "2025-06-30"},
        complete=True,
        position_context=c,
    )
    config = settings("2024-25")
    repo = BigQueryWarehouseRepository(config, client=SimpleNamespace())
    repo._governed_warehouse = SimpleNamespace(
        load=lambda seasons: ({"capture": {}}, e)
    )
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
            response = client.post(
                "/api/agent/ask" + suffix,
                json={
                    "question": "How was Example 1's playmaking in the 2024-25 regular season "
                    + (
                        f"compared with the prior season and other {word}?"
                        if baseline_requested
                        else f"among {word}?"
                    )
                },
            )
            assert response.status_code == 200
            p = (
                response.json()
                if not suffix
                else next(
                    json.loads(line[6:])["payload"]
                    for line in response.text.splitlines()
                    if line.startswith("data: ")
                    and json.loads(line[6:]).get("type") == "final"
                )
            )
            assert p["status"] == "ok"
            assert (
                p["semantic_evidence"]["scope"]["comparison_intent"][
                    "baseline_requested"
                ]
                is baseline_requested
            )
            assert p["semantic_evidence"]["request_fulfillment"]["status"] == "complete"
            assert "ranked" in p["answer"].split(".")[0]
            ast = next(
                m for m in p["semantic_evidence"]["metrics"] if m["key"] == "ast"
            )
            assert ast["peer_comparison"]["group"] == group
            assert ast["peer_comparison"]["cohort_size"] == 20
            assert ast["peer_comparison"]["kind"] == "position"
            assert ("bigs/centers" if word == "big men" else word) in p["answer"]
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(old)
