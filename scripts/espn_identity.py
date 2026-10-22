"""Pinned, inert NBA player catalog and provider-separated identity crosswalk."""

from __future__ import annotations

import ast
import json
import re
import time
from importlib.metadata import version
from pathlib import Path

import requests
from nba_api.stats.static.players import get_players
from scripts.espn_sources import identifier, player_index
from scripts.fetch_gamebooks import now, write_json
from scripts.gamebook_sources import SourceUnavailable
from scripts.gamebook_store import require
from scripts.parse_gamebooks import key, sha

PIN_PATH = Path(__file__).resolve().parents[1] / "contracts/espn_player_catalog.json"


def with_installed_aliases(reference, installed):
    """Keep source-backed old spellings only for IDs present in the pinned catalog."""
    ids = {str(p["id"]) for p in reference}
    names = {(str(p["id"]), key(p["full_name"])) for p in reference}
    aliases = [
        {"id": p["id"], "full_name": p["full_name"]}
        for p in installed
        if str(p["id"]) in ids and (str(p["id"]), key(p["full_name"])) not in names
    ]
    return reference + aliases, aliases


def parse_catalog(body, pin):
    require(sha(body) == pin["sha256"], "Player catalog hash differs from pin")
    try:
        tree = ast.parse(body.decode("utf-8"))
    except SyntaxError as exc:
        raise ValueError("Invalid catalog syntax") from exc
    assignments = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "players" for t in node.targets)
    ]
    require(len(assignments) == 1, "Expected one players literal")
    # Never import or execute the downloaded module, including unrelated statements.
    rows = ast.literal_eval(assignments[0].value)
    require(isinstance(rows, list) and 1 <= len(rows) <= 10000, "Invalid catalog size")
    players, seen = [], set()
    for row in rows:
        require(isinstance(row, list) and len(row) == 5, "Invalid catalog row")
        pid, last, first, name, active = row
        require(
            type(pid) is int
            and 0 < pid < 10**10
            and pid not in seen
            and all(isinstance(v, str) for v in (last, first, name))
            and name.strip()
            and isinstance(active, bool),
            "Invalid/duplicate catalog player",
        )
        seen.add(pid)
        players.append({"id": pid, "full_name": name})
    return players


def fetch_catalog(directory, session):
    pin = json.loads(PIN_PATH.read_text())
    require(
        pin["repository"] == "swar/nba_api"
        and pin["path"] == "src/nba_api/stats/library/data.py"
        and re.fullmatch(r"[0-9a-f]{40}", pin["commit"]),
        "Invalid catalog pin",
    )
    url = f"https://raw.githubusercontent.com/{pin['repository']}/{pin['commit']}/{pin['path']}"
    directory.mkdir(parents=True)
    record = {"url": url, "started_at": now(), "status": "failed", "pin": pin}
    body = bytearray()
    started = time.monotonic()
    try:
        with session.get(
            url, timeout=(10, 20), stream=True, allow_redirects=False
        ) as r:
            record["http_status"] = r.status_code
            for chunk in r.iter_content(65536):
                require(len(body) + len(chunk) <= 2 * 1024 * 1024, "Catalog size bound")
                body.extend(chunk)
                require(time.monotonic() - started <= 40, "Catalog time bound")
            if r.status_code != 200:
                raise SourceUnavailable(f"Catalog HTTP {r.status_code}")
        players = parse_catalog(bytes(body), pin)
        players, aliases = with_installed_aliases(players, get_players())
        write_json(
            directory / "installed_aliases.json",
            {
                "source": "nba_api.stats.static.players",
                "package_version": version("nba_api"),
                "aliases": aliases,
            },
        )
        record.update(status="captured", players=len(players))
        return players, pin
    except requests.RequestException as exc:
        record["error_type"] = type(exc).__name__
        raise SourceUnavailable("Player catalog network failure") from exc
    finally:
        record.update(finished_at=now(), sha256=sha(body), bytes=len(body))
        (directory / "response.body").write_bytes(body)
        write_json(directory / "attempt.json", record)


def crosswalk(players, reference, pin):
    """Resolve exact unique names; reject conflicts with previously accepted IDs."""
    index = player_index(reference)
    mappings, unresolved = {}, {}
    for player in players:
        pid = identifier(player["source_player_id"])
        name = key(player["name"])
        ids = index.get(name, set())
        old = player["nba_player_id"]
        require(
            not old or ids == {old},
            "Existing NBA player mapping conflicts with catalog",
        )
        if len(ids) != 1:
            unresolved[pid] = {
                "source_player_id": pid,
                "name": player["name"],
                "reason": "ambiguous" if ids else "missing",
            }
            continue
        row = {
            "source_player_id": pid,
            "nba_player_id": next(iter(ids)),
            "normalized_name": name,
            "method": pin["method"],
            "catalog_sha256": pin["sha256"],
        }
        require(pid not in mappings or mappings[pid] == row, "ESPN identity conflict")
        mappings[pid] = row
    require(
        not set(mappings) & set(unresolved), "Conflicting resolved/unresolved identity"
    )
    require(
        len({p["nba_player_id"] for p in mappings.values()}) == len(mappings),
        "Multiple ESPN IDs map to one NBA player",
    )
    return list(mappings.values()), list(unresolved.values())


def refresh(store, season, output, session):
    """Enrich accepted dev facts without altering their validation/health status."""
    from scripts.gamebook_ingestion import archive_sources
    from scripts.gamebook_sources import season_bounds

    season_bounds(season)
    directory = output / "sources"
    directory.mkdir()
    try:
        reference, pin = fetch_catalog(directory / "catalog", session)
        rows = store.cloud.query(
            f"SELECT source_game_id,revision,payload FROM `{store.prefix}.espn_current_snapshots` WHERE JSON_VALUE(payload,'$.game.season')=@season LIMIT 1501",
            [store.cloud.parameter("season", season)],
        )
        require(0 < len(rows) <= 1500, "Empty/oversized source game set")
        write_json(directory / "snapshots.json", rows)
        players = [p for r in rows for p in json.loads(r["payload"])["players"]]
        mappings, unresolved = crosswalk(players, reference, pin)
        write_json(directory / "mappings.json", mappings)
        write_json(directory / "unresolved.json", unresolved)
    finally:
        evidence = archive_sources(store.cloud, directory)
    result = store.remember_players(mappings, evidence)
    report = {
        "season": season,
        "source_games": len(rows),
        "roster_rows": len(players),
        "resolved_players": len(mappings),
        "unresolved": unresolved,
        "evidence_uri": evidence,
        **result,
    }
    write_json(output / "identity_result.json", report)
    return report
