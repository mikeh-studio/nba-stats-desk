"""Local, transactional NBA identity registry with evidence-backed aliases."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from uuid import uuid4


class ContractError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise ContractError(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def evidence(value):
    require(isinstance(value, dict), "Missing evidence receipt")
    require(
        set(value) == {"sha256", "reference", "captured_at", "kind"},
        "Evidence receipt fields differ",
    )
    require(
        isinstance(value["sha256"], str)
        and re.fullmatch(r"[a-f0-9]{64}", value["sha256"]),
        "Invalid evidence hash",
    )
    require(
        isinstance(value["reference"], str) and 0 < len(value["reference"]) <= 512,
        "Invalid evidence reference",
    )
    require(
        value["kind"]
        in (
            "archive_snapshot",
            "provider_capture",
            "reviewed_identity",
            "synthetic_fixture",
        ),
        "Unsupported evidence kind",
    )
    if value["captured_at"] is not None:
        require(
            isinstance(value["captured_at"], str)
            and datetime.fromisoformat(
                value["captured_at"].replace("Z", "+00:00")
            ).tzinfo,
            "Evidence time requires timezone",
        )
    return json.loads(canonical(value))


def scope(value):
    require(
        set(value) == {"league", "season", "phase", "date", "home", "away"},
        "Game scope fields differ",
    )
    require(
        value["league"] == "nba" and value["phase"] == "Regular Season",
        "Unsupported competition",
    )
    season = value["season"]
    require(
        isinstance(season, str) and re.fullmatch(r"20\d{2}-\d{2}", season),
        "Invalid season",
    )
    require(season[-2:] == str(int(season[:4]) + 1)[-2:], "Invalid season suffix")
    day = date.fromisoformat(value["date"])
    require(
        str(day) == value["date"]
        and (
            (day.year == int(season[:4]) and day.month >= 7)
            or (day.year == int(season[:4]) + 1 and day.month <= 6)
        ),
        "Invalid scoped date",
    )
    teams = set(
        "ATL BOS BKN CHA CHI CLE DAL DEN DET GSW HOU IND LAC LAL MEM MIA MIL MIN NOP NYK OKC ORL PHI PHX POR SAC SAS TOR UTA WAS".split()
    )
    require(
        value["home"] in teams
        and value["away"] in teams
        and value["home"] != value["away"],
        "Invalid game teams",
    )
    return json.loads(canonical(value))


def native_game_id(provider, native_id, game_scope):
    require(
        isinstance(native_id, str),
        "Native game IDs must retain their string representation",
    )
    pattern = (
        r"002" + game_scope["season"][2:4] + r"\d{5}"
        if provider == "nba_api"
        else r"[1-9]\d{0,19}"
    )
    require(
        provider in ("nba_api", "espn") and re.fullmatch(pattern, native_id),
        "Invalid native game ID/season/phase",
    )


def person_id(value):
    require(type(value) is int and 0 < value < 2**63, "Invalid NBA person ID")
    return value


class IdentityRegistry:
    def __init__(self, path=":memory:"):
        self.connection = sqlite3.connect(str(path), isolation_level=None, timeout=5)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        tables = {
            r[0]
            for r in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if tables:
            require(
                "identity_metadata" in tables,
                "Refusing a database owned by another application",
            )
            require(
                [
                    r[0]
                    for r in self.connection.execute(
                        "SELECT version FROM identity_metadata"
                    )
                ]
                == ["unified_identity/1"],
                "Registry version differs",
            )
        self.connection.executescript("""
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS identity_metadata(version TEXT PRIMARY KEY);
INSERT OR IGNORE INTO identity_metadata VALUES('unified_identity/1');
CREATE TABLE IF NOT EXISTS games(game_uid TEXT PRIMARY KEY,scope_version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS game_scopes(game_uid TEXT REFERENCES games,version INTEGER,scope TEXT NOT NULL,evidence TEXT NOT NULL,PRIMARY KEY(game_uid,version));
CREATE TABLE IF NOT EXISTS game_aliases(provider TEXT,native_id TEXT,game_uid TEXT NOT NULL REFERENCES games,evidence TEXT NOT NULL,PRIMARY KEY(provider,native_id));
CREATE TABLE IF NOT EXISTS players(player_id INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS player_aliases(provider TEXT,native_id TEXT,player_id INTEGER NOT NULL REFERENCES players,evidence TEXT NOT NULL,PRIMARY KEY(provider,native_id));
CREATE TABLE IF NOT EXISTS player_alias_corrections(provider TEXT,native_id TEXT,version INTEGER,previous_player_id INTEGER NOT NULL REFERENCES players,player_id INTEGER NOT NULL REFERENCES players,evidence TEXT NOT NULL,PRIMARY KEY(provider,native_id,version),FOREIGN KEY(provider,native_id) REFERENCES player_aliases(provider,native_id));
COMMIT;
""")
        require(
            [
                r[0]
                for r in self.connection.execute(
                    "SELECT version FROM identity_metadata"
                )
            ]
            == ["unified_identity/1"],
            "Registry version differs",
        )
        for table in (
            "game_scopes",
            "game_aliases",
            "player_aliases",
            "player_alias_corrections",
        ):
            for operation in ("UPDATE", "DELETE"):
                self.connection.execute(
                    f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{operation} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'immutable history'); END"
                )
        if str(path) != ":memory:":
            Path(path).chmod(0o600)

    @contextmanager
    def atomic(self):
        nested = self.connection.in_transaction
        savepoint = "s_" + uuid4().hex
        self.connection.execute(
            "SAVEPOINT " + savepoint if nested else "BEGIN IMMEDIATE"
        )
        try:
            yield
        except BaseException:
            if nested:
                self.connection.execute("ROLLBACK TO " + savepoint)
                self.connection.execute("RELEASE " + savepoint)
            else:
                self.connection.rollback()
            raise
        else:
            self.connection.execute(
                "RELEASE " + savepoint
            ) if nested else self.connection.commit()

    def close(self):
        self.connection.close()

    def catalog(self):
        """Inspectable offline identity snapshot; never an active release pointer."""
        aliases = [
            dict(row)
            for row in self.connection.execute(
                "SELECT provider,native_id,game_uid,evidence FROM game_aliases ORDER BY provider,native_id"
            )
        ]
        people = [
            dict(row)
            for row in self.connection.execute(
                "SELECT provider,native_id,player_id,evidence FROM player_aliases ORDER BY provider,native_id"
            )
        ]
        scopes = [
            dict(row)
            for row in self.connection.execute(
                "SELECT game_uid,version,scope,evidence FROM game_scopes ORDER BY game_uid,version"
            )
        ]
        for row in aliases + people + scopes:
            row["evidence"] = json.loads(row["evidence"])
        for row in scopes:
            row["scope"] = json.loads(row["scope"])
        corrections = [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM player_alias_corrections ORDER BY provider,native_id,version"
            )
        ]
        for row in corrections:
            row["evidence"] = json.loads(row["evidence"])
        result = {
            "schema_version": "unified_identity_catalog/1",
            "runtime_eligible": False,
            "game_aliases": aliases,
            "player_aliases": people,
            "game_scopes": scopes,
            "player_alias_corrections": corrections,
        }
        return {**result, "catalog_version": digest(result)}

    def lookup(self, provider, native_id):
        row = self.connection.execute(
            "SELECT game_uid FROM game_aliases WHERE provider=? AND native_id=?",
            (provider, native_id),
        ).fetchone()
        return row[0] if row else None

    def game_scope(self, uid):
        row = self.connection.execute(
            "SELECT s.scope,g.scope_version FROM games g JOIN game_scopes s ON g.game_uid=s.game_uid AND g.scope_version=s.version WHERE g.game_uid=?",
            (uid,),
        ).fetchone()
        require(row is not None, "Unknown game UID")
        return json.loads(row["scope"]), row["scope_version"]

    def register_game(self, provider, native_id, game_scope, receipt):
        game_scope = scope(game_scope)
        native_game_id(provider, native_id, game_scope)
        receipt = evidence(receipt)
        with self.atomic():
            uid = self.lookup(provider, native_id)
            if uid:
                require(
                    self.game_scope(uid)[0] == game_scope,
                    "Game scope changed; explicit reviewed scope revision required",
                )
                return uid
            # A matching date/team pair is a potential duplicate, never proof for an automatic merge.
            candidates = self.connection.execute(
                "SELECT scope FROM game_scopes"
            ).fetchall()
            require(
                not any(json.loads(r[0]) == game_scope for r in candidates),
                "Potential overlapping game requires an explicit alias link",
            )
            uid = "g_" + uuid4().hex
            self.connection.execute("INSERT INTO games VALUES(?,1)", (uid,))
            self.connection.execute(
                "INSERT INTO game_scopes VALUES(?,1,?,?)",
                (uid, canonical(game_scope), canonical(receipt)),
            )
            self.connection.execute(
                "INSERT INTO game_aliases VALUES(?,?,?,?)",
                (provider, native_id, uid, canonical(receipt)),
            )
            return uid

    def attach_game_alias(self, uid, provider, native_id, game_scope, receipt):
        game_scope = scope(game_scope)
        native_game_id(provider, native_id, game_scope)
        receipt = evidence(receipt)
        require(
            receipt["kind"] in ("reviewed_identity", "synthetic_fixture"),
            "Alias linking requires reviewed identity evidence",
        )
        with self.atomic():
            require(self.game_scope(uid)[0] == game_scope, "Alias scope differs")
            old = self.lookup(provider, native_id)
            require(old in (None, uid), "Native game ID already resolves elsewhere")
            self.connection.execute(
                "INSERT OR IGNORE INTO game_aliases VALUES(?,?,?,?)",
                (provider, native_id, uid, canonical(receipt)),
            )

    def revise_scope(self, uid, new_scope, expected_version, receipt):
        new_scope = scope(new_scope)
        receipt = evidence(receipt)
        require(
            receipt["kind"] in ("reviewed_identity", "synthetic_fixture"),
            "Scope revision requires review",
        )
        with self.atomic():
            old, version = self.game_scope(uid)
            require(version == expected_version, "Stale scope reviewer")
            require(
                all(
                    old[k] == new_scope[k]
                    for k in ("league", "season", "phase", "home", "away")
                ),
                "Scope revision may only change the scheduled date",
            )
            require(
                not self.connection.execute(
                    "SELECT 1 FROM game_scopes WHERE game_uid!=? AND scope=?",
                    (uid, canonical(new_scope)),
                ).fetchone(),
                "Revised scope overlaps another game",
            )
            if old == new_scope:
                return version
            self.connection.execute(
                "INSERT INTO game_scopes VALUES(?,?,?,?)",
                (uid, version + 1, canonical(new_scope), canonical(receipt)),
            )
            self.connection.execute(
                "UPDATE games SET scope_version=? WHERE game_uid=?", (version + 1, uid)
            )
            return version + 1

    def register_person(self, player_id):
        player_id = person_id(player_id)
        self.connection.execute("INSERT OR IGNORE INTO players VALUES(?)", (player_id,))
        return player_id

    def attach_person_alias(self, provider, native_id, player_id, receipt):
        require(
            provider in ("nba_api", "espn")
            and isinstance(native_id, str)
            and re.fullmatch(r"[1-9]\d{0,18}", native_id),
            "Invalid native person identity",
        )
        player_id = person_id(player_id)
        receipt = evidence(receipt)
        if provider == "nba_api":
            require(native_id == str(player_id), "NBA person identity differs")
        else:
            require(
                receipt["kind"] in ("reviewed_identity", "synthetic_fixture"),
                "Cross-provider player identity requires review",
            )
        with self.atomic():
            self.register_person(player_id)
            old = self.resolve_person(provider, native_id)
            require(
                old is None or old[0] == player_id,
                "Conflicting player alias; names never resolve identity",
            )
            self.connection.execute(
                "INSERT OR IGNORE INTO player_aliases VALUES(?,?,?,?)",
                (provider, native_id, player_id, canonical(receipt)),
            )

    def resolve_person(self, provider, native_id):
        """Return the current explicit binding and its version; never use names."""
        row = self.connection.execute(
            "SELECT player_id,version FROM player_alias_corrections WHERE provider=? AND native_id=? ORDER BY version DESC LIMIT 1",
            (provider, native_id),
        ).fetchone()
        if row:
            return row[0], row[1]
        row = self.connection.execute(
            "SELECT player_id FROM player_aliases WHERE provider=? AND native_id=?",
            (provider, native_id),
        ).fetchone()
        return (row[0], 1) if row else None

    def correct_person_alias(
        self, provider, native_id, player_id, expected_version, receipt
    ):
        """Append a reviewed ESPN correction; old facts keep their original IDs.

        ObservationStore holds stale affected current facts until complete game
        revisions are explicitly reprocessed against the corrected binding.
        """
        require(provider == "espn", "NBA person IDs cannot be reassigned")
        player_id = person_id(player_id)
        receipt = evidence(receipt)
        require(
            receipt["kind"] in ("reviewed_identity", "synthetic_fixture"),
            "Mapping correction requires review",
        )
        with self.atomic():
            previous = self.resolve_person(provider, native_id)
            require(
                previous is not None and previous[1] == expected_version,
                "Stale or absent mapping correction",
            )
            require(previous[0] != player_id, "Mapping correction must change identity")
            self.register_person(player_id)
            version = expected_version + 1
            self.connection.execute(
                "INSERT INTO player_alias_corrections VALUES(?,?,?,?,?,?)",
                (
                    provider,
                    native_id,
                    version,
                    previous[0],
                    player_id,
                    canonical(receipt),
                ),
            )
            return version
