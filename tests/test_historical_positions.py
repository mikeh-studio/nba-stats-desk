"""Synthetic historical-source and hybrid-membership regressions."""

import gzip
import hashlib
import json
from copy import deepcopy

import pytest
from app.agent.historical_positions import digest, roster_records
from app.agent.overview_positions import validated_context
from scripts.build_position_context import build
from tests.test_overview_positions import compare, fixture


def archive():
    return dict(
        fetched="2025-07-01T00:00:00+00:00",
        rosters={
            str(t): dict(
                resource="commonteamroster",
                parameters=dict(Season="2024-25", TeamID=t, LeagueID="00"),
                resultSets=[
                    dict(
                        name="CommonTeamRoster",
                        headers=[
                            "TeamID",
                            "SEASON",
                            "LeagueID",
                            "PLAYER_ID",
                            "POSITION",
                        ],
                        rowSet=[[t, "2024", "00", t, "G"]],
                    )
                ],
            )
            for t in range(1, 31)
        },
    )


def test_source_normalization_and_exact_id():
    records = roster_records(
        archive(), "2024-25", "https://example.org/archive", "a" * 64
    )
    assert len(records) == 30
    assert records[0]["player_id"] == 1
    assert records[0]["position"] == "G"
    assert records[0]["source_locator"] == "TeamID=1;PLAYER_ID=1"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda x: x["rosters"].pop("1"),
        lambda x: x["rosters"]["1"]["parameters"].update(Season="2023-24"),
        lambda x: x["rosters"]["1"]["parameters"].update(TeamID=31),
        lambda x: x["rosters"]["1"]["resultSets"][0]["rowSet"][0].__setitem__(
            1, "2023"
        ),
        lambda x: x["rosters"]["1"]["resultSets"][0]["rowSet"][0].__setitem__(3, 2),
        lambda x: x["rosters"]["1"]["resultSets"][0]["rowSet"][0].__setitem__(
            4, "unknown"
        ),
    ],
)
def test_bad_source_fails_closed(mutation):
    raw = archive()
    mutation(raw)
    with pytest.raises(ValueError):
        roster_records(raw, "2024-25", "https://example.org", "a" * 64)


@pytest.mark.parametrize(
    "position,question,group",
    [
        ("F-G", "among wings", "Wing"),
        ("G-F", "among guards", "Guard"),
        ("F-C", "among big men", "Big"),
        ("C-F", "among wings", "Wing"),
        ("F", "among wings", "Wing"),
        ("PF", "among wings", "Wing"),
    ],
)
def test_listed_hybrids_never_guessed(position, question, group):
    p = compare(position=position, question=question)["peer_comparison"]
    assert (p["kind"], p["group"], p["cohort_size"]) == ("position", group, 20)
    assert p["classified_players"] == p["eligible_players"] == 20
    assert p["membership_policy"] == "listed_positions_overlap"


def test_unrequested_hybrid_has_no_invented_primary():
    assert (
        compare(position="F-G")["peer_comparison"]["fallback_reason"]
        == "ambiguous_position"
    )


def test_hybrid_member_counts_once_and_does_not_select_better_group():
    def mutate(g, c):
        for rec in c["records"][10:]:
            rec["position"] = "C"

    p = compare(position="F-C", question="among wings", mutate=mutate)[
        "peer_comparison"
    ]
    assert (p["group"], p["cohort_size"], p["rank"]) == ("Wing", 10, 10)


def test_tamper_and_legacy_mapping_fail_closed():
    _, c = fixture()
    c["records"][0]["position"] = "C"
    assert validated_context(c) == ({}, set())
    c["sha256"] = digest(c)
    c["mapping_version"] = "position_groups/1"
    assert validated_context(c) == ({}, set())


def test_builder_checks_retained_bytes_and_duplicate_supplements(tmp_path):
    data = gzip.compress(json.dumps(archive()).encode())
    (tmp_path / "roster.gz").write_bytes(data)
    m = dict(
        archives=[
            dict(
                path="roster.gz",
                season="2024-25",
                url="https://example.org/archive",
                sha256=hashlib.sha256(data).hexdigest(),
            )
        ],
        reviewed_seasons=["2024-25"],
    )
    c = build(m, tmp_path)
    assert len(validated_context(c)[0]) == 30
    bad = deepcopy(m)
    bad["archives"][0]["sha256"] = "b" * 64
    with pytest.raises(ValueError, match="checksum"):
        build(bad, tmp_path)
    supplemental = json.dumps([c["records"][0]]).encode()
    (tmp_path / "supplement.json").write_bytes(supplemental)
    m["supplements"] = dict(
        path="supplement.json", sha256=hashlib.sha256(supplemental).hexdigest()
    )
    with pytest.raises(ValueError, match="duplicate"):
        build(m, tmp_path)


def test_configured_private_bundle_enters_warehouse_snapshot(tmp_path):
    from app.agent.semantic_source import BigQuerySemanticSource, snapshot_evidence
    from tests.test_semantic_source import Client

    _, context = fixture()
    path = tmp_path / "positions.json"
    path.write_text(json.dumps(context))
    snapshot = BigQuerySemanticSource(
        Client(), project="test-project", position_context_path=str(path)
    ).capture(["2024-25"])
    evidence = snapshot_evidence(snapshot)
    assert evidence.position_context == context
    assert validated_context(evidence.position_context)[0][("2024-25", 1)] == ("Guard",)
