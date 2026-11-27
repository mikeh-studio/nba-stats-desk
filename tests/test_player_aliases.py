"""Cohort-bound aliases preserve the shared identity clarification policy."""

from types import SimpleNamespace

import pytest
from app.agent.alias_builder import (
    build_programmatic_aliases,
    player_aliases_for_cohort,
)
from app.agent.name_matching import PlayerNameError, prepare_names
from app.agent.player_resolver import PlayerResolver
from app.agent.semantic_serving import players_from_rows


def roster(*names):
    return [
        {"player_id": player_id, "player_name": name}
        for player_id, name in enumerate(names, start=1)
    ]


@pytest.mark.parametrize(
    "name,alias",
    [
        ("Nikola Jokić", "nikola jokic"),
        ("Luka Dončić", "luka doncic"),
        ("De'Aaron Fox", "deaaron fox"),
        ("D’Angelo Russell", "dangelo russell"),
        ("Shai Gilgeous-Alexander", "shai gilgeous alexander"),
        ("Karl-Anthony Towns", "karl anthony towns"),
        ("Victor Wembanyama", "wembanyama"),
        ("Giannis Antetokounmpo", "antetokounmpo"),
    ],
)
def test_full_name_folding_and_unique_surnames(name, alias):
    players = roster(name)
    assert build_programmatic_aliases(players, overrides={})[alias] == name
    assert prepare_names(f"{alias} points per game", players)[0] == (
        f"{name} points per game"
    )


@pytest.mark.parametrize("suffix", ["Jr.", "Sr.", "II", "III", "IV"])
def test_suffix_removed_only_when_base_is_unique(suffix):
    name = f"Avery Finch {suffix}"
    aliases = build_programmatic_aliases(roster(name), overrides={})
    assert aliases["avery finch"] == name
    assert aliases["finch"] == name
    competing = roster(name, "Avery Finch")
    aliases = build_programmatic_aliases(competing, overrides={})
    assert "avery finch" not in aliases
    assert "finch" not in aliases
    with pytest.raises(PlayerNameError) as error:
        prepare_names("Avery Finch assists", competing)
    assert error.value.result["status"] == "clarification_required"


@pytest.mark.parametrize(
    "first,second,alias",
    [
        ("Stephen Curry", "Seth Curry", "curry"),
        ("Jalen Williams", "Jaylin Williams", "williams"),
        ("Draymond Green", "Jalen Green", "green"),
        ("Jamal Murray", "Dejounte Murray", "murray"),
        ("Klay Thompson", "Amen Thompson", "thompson"),
        ("Avery Li", "Blake Li", "li"),
    ],
)
def test_shared_surnames_still_clarify_in_ask(first, second, alias):
    players = players_from_rows(roster(first, second))
    assert alias not in build_programmatic_aliases(players)
    assert all(alias not in p["aliases"] for p in players)
    with pytest.raises(PlayerNameError) as error:
        prepare_names(f"{alias} points per game", players)
    assert error.value.result["status"] == "clarification_required"
    assert {p["player_name"] for p in error.value.result["matches"]} == {first, second}


def test_curated_nicknames_merge_without_hiding_other_identities():
    players = roster(
        "Nikola Jokić",
        "Victor Wembanyama",
        "Shai Gilgeous-Alexander",
        "Kevin Durant",
        "Anthony Edwards",
        "LeBron James",
        "James Harden",
        "Stephen Curry",
        "Seth Curry",
    )
    expected = dict(
        zip(
            ("joker", "wemby", "sga", "kd", "ant", "bron", "the beard", "steph"),
            (p["player_name"] for p in players[:8]),
            strict=True,
        )
    )
    aliases = build_programmatic_aliases(players)
    assert {key: aliases[key] for key in expected} == expected
    assert aliases == build_programmatic_aliases(reversed(players))
    assert "curry" not in aliases
    assert "joker" not in build_programmatic_aliases(
        [*players, {"player_id": 10, "player_name": "Nikola Jokic"}]
    )
    overrides = {
        "Curry": "Seth Curry",
        "jokic": "James Harden",
        "unobserved": "Avery Finch",
    }
    assert not set(build_programmatic_aliases(players, overrides=overrides)) & {
        "curry",
        "jokic",
        "unobserved",
    }
    assert "avery" not in build_programmatic_aliases(
        roster("Avery Finch"), overrides={"avery": "Absent Player"}
    )


def test_governed_rows_and_repository_resolver_share_aliases():
    players = roster("Luka Dončić", "Luka Dončić", "Stephen Curry", "Seth Curry")
    players[1]["player_id"] = players[0]["player_id"]
    governed = players_from_rows(iter(players))
    assert len(governed) == 3
    assert {"luka doncic", "doncic"} <= set(governed[0]["aliases"])
    assert prepare_names("Doncic assists", governed)[0] == "Luka Dončić assists"
    assert player_aliases_for_cohort(governed) == governed

    repo = SimpleNamespace(
        list_agent_player_candidates=lambda **_: players,
        search_players=lambda *_, **__: [],
    )
    resolver = PlayerResolver(repo)
    assert resolver.resolve("Doncic").player.player_name == "Luka Dončić"
    assert resolver.resolve("steph").player.player_name == "Stephen Curry"
    assert resolver.resolve("curry").status == "ambiguous"
