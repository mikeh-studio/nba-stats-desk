"""Append-only local observations and guarded game pointers; no serving integration."""

from __future__ import annotations

import hashlib
import json
import math

from .historical import normalize
from .identity import IdentityRegistry, canonical, digest, evidence, require


def parse_json(body):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "Duplicate JSON key")
            result[key] = value
        return result

    def constant(value):
        raise ValueError("Nonfinite JSON number")

    def finite_float(text):
        value = float(text)
        require(math.isfinite(value), "Nonfinite JSON number")
        return value

    return json.loads(
        body, object_pairs_hook=pairs, parse_constant=constant, parse_float=finite_float
    )


class ObservationStore(IdentityRegistry):
    def __init__(self, path=":memory:"):
        super().__init__(path)
        self.connection.executescript("""
CREATE TABLE IF NOT EXISTS fact_revisions(game_uid TEXT REFERENCES games,revision TEXT,payload TEXT NOT NULL,PRIMARY KEY(game_uid,revision));
CREATE TABLE IF NOT EXISTS observations(observation_id TEXT PRIMARY KEY,game_uid TEXT REFERENCES games,revision TEXT NOT NULL,payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS current_games(game_uid TEXT PRIMARY KEY REFERENCES games,revision TEXT NOT NULL,observation_id TEXT NOT NULL REFERENCES observations);
CREATE TABLE IF NOT EXISTS current_espn_games(game_uid TEXT PRIMARY KEY REFERENCES games,revision TEXT NOT NULL,observation_id TEXT NOT NULL REFERENCES observations);
CREATE TABLE IF NOT EXISTS observation_events(event_id INTEGER PRIMARY KEY,game_uid TEXT,previous_revision TEXT,revision TEXT,observation_id TEXT,status TEXT);
CREATE TABLE IF NOT EXISTS quarantined_inputs(input_id TEXT PRIMARY KEY,provider TEXT,source_game_id TEXT,payload TEXT NOT NULL,receipt TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS quarantine_resolutions(input_id TEXT PRIMARY KEY REFERENCES quarantined_inputs,observation_id TEXT NOT NULL REFERENCES observations,review_sha256 TEXT NOT NULL);
""")
        for table in (
            "fact_revisions",
            "observations",
            "observation_events",
            "game_scopes",
            "game_aliases",
            "player_aliases",
            "player_alias_corrections",
            "quarantined_inputs",
            "quarantine_resolutions",
        ):
            for operation in ("UPDATE", "DELETE"):
                self.connection.execute(
                    f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{operation} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'immutable history'); END"
                )

    def current_revision(self, uid, provider=None):
        if provider is None:
            rows = self.connection.execute(
                "SELECT revision FROM current_games WHERE game_uid=? UNION ALL SELECT revision FROM current_espn_games WHERE game_uid=?",
                (uid, uid),
            ).fetchall()
            require(len(rows) <= 1, "Provider selection required for overlapping game")
            return rows[0][0] if rows else None
        require(provider in ("nba_api", "espn"), "Unknown current provider")
        table = "current_games" if provider == "nba_api" else "current_espn_games"
        row = self.connection.execute(
            f"SELECT revision FROM {table} WHERE game_uid=?", (uid,)
        ).fetchone()
        return row[0] if row else None

    def ingest_bytes(self, body, receipt, *, expected_revision=None):
        require(
            isinstance(body, bytes) and 0 < len(body) <= 1024 * 1024,
            "Game capture exceeds size bound",
        )
        receipt = evidence(receipt)
        require(
            hashlib.sha256(body).hexdigest() == receipt["sha256"],
            "Input bytes differ from evidence hash",
        )
        payload = parse_json(body)
        with self.atomic():
            if payload.get("provider") == "espn":
                from .espn import normalize as normalize_espn

                observation = normalize_espn(payload, self, receipt)
            else:
                observation = normalize(payload, self, receipt)
            provider = observation["semantic"]["provider"]
            table = "current_games" if provider == "nba_api" else "current_espn_games"
            uid = observation["semantic"]["game_uid"]
            revision = observation["fact_revision"]
            oid = observation["observation_id"]
            previous = self.current_revision(uid, provider)
            require(
                expected_revision in (None, previous), "Stale expected game revision"
            )
            status = "recorded"
            if previous is not None and revision != previous:
                require(
                    expected_revision == previous,
                    "Correction requires expected prior revision",
                )
                require(
                    observation["membership"]["level"] == "provider_declared_complete",
                    "Replacement needs complete participant evidence",
                )
                require(
                    self.connection.execute(
                        "SELECT 1 FROM fact_revisions WHERE game_uid=? AND revision=?",
                        (uid, revision),
                    ).fetchone()
                    is None,
                    "Historical revision replay cannot roll back current facts",
                )
                status = "replaced"
            elif previous == revision:
                status = "noop"
            old = self.connection.execute(
                "SELECT payload FROM fact_revisions WHERE game_uid=? AND revision=?",
                (uid, revision),
            ).fetchone()
            require(
                old is None or old[0] == canonical(observation["semantic"]),
                "Revision content differs",
            )
            self.connection.execute(
                "INSERT OR IGNORE INTO fact_revisions VALUES(?,?,?)",
                (uid, revision, canonical(observation["semantic"])),
            )
            existing = self.connection.execute(
                "SELECT payload FROM observations WHERE observation_id=?", (oid,)
            ).fetchone()
            require(
                existing is None or existing[0] == canonical(observation),
                "Observation content differs",
            )
            self.connection.execute(
                "INSERT OR IGNORE INTO observations VALUES(?,?,?,?)",
                (oid, uid, revision, canonical(observation)),
            )
            if previous != revision:
                self.connection.execute(
                    f"INSERT INTO {table} VALUES(?,?,?) ON CONFLICT(game_uid) DO UPDATE SET revision=excluded.revision,observation_id=excluded.observation_id",
                    (uid, revision, oid),
                )
            # Replaying the same observation is a no-op, including its audit trail.
            if existing is None:
                self.connection.execute(
                    "INSERT INTO observation_events(game_uid,previous_revision,revision,observation_id,status) VALUES(?,?,?,?,?)",
                    (uid, previous, revision, oid, status),
                )
            return {
                "game_uid": uid,
                "revision": revision,
                "observation_id": oid,
                "status": status,
                "membership_level": observation["membership"]["level"],
            }

    def read_observation(self, oid):
        row = self.connection.execute(
            "SELECT payload,game_uid,revision FROM observations WHERE observation_id=?",
            (oid,),
        ).fetchone()
        require(row is not None, "Unknown observation")
        result = json.loads(row["payload"])
        stored_id = result.pop("observation_id")
        require(stored_id == oid == digest(result), "Observation integrity failed")
        result["observation_id"] = stored_id
        require(
            digest(result["semantic"]) == result["fact_revision"],
            "Fact integrity failed",
        )
        require(
            result["semantic"]["game_uid"] == row["game_uid"]
            and result["fact_revision"] == row["revision"],
            "Observation identity binding differs",
        )
        return result

    def quarantine_bytes(self, body, receipt, reason):
        """Retain rejected input without promoting identities or partial facts."""
        receipt = evidence(receipt)
        require(
            isinstance(body, bytes)
            and 0 < len(body) <= 1024 * 1024
            and hashlib.sha256(body).hexdigest() == receipt["sha256"],
            "Quarantine input hash/size differs",
        )
        value = parse_json(body)
        require(
            value.get("provider") in ("nba_api", "espn")
            and isinstance(reason, str)
            and 0 < len(reason) <= 512,
            "Invalid quarantine",
        )
        gid = value["game"].get("game_id", value["game"].get("source_game_id"))
        key = digest({"receipt": receipt, "reason": reason})
        self.connection.execute(
            "INSERT OR IGNORE INTO quarantined_inputs VALUES(?,?,?,?,?,?)",
            (
                key,
                value["provider"],
                gid,
                body.decode("utf-8"),
                canonical(receipt),
                reason,
            ),
        )
        return key

    def current_semantics(self, provider=None):
        require(provider in (None, "nba_api", "espn"), "Unknown current provider")
        result = []
        tables = (
            ["current_games"]
            if provider == "nba_api"
            else ["current_espn_games"]
            if provider == "espn"
            else ["current_games", "current_espn_games"]
        )
        rows = [
            row
            for table in tables
            for row in self.connection.execute(
                f"SELECT c.game_uid uid,f.payload,f.revision,c.observation_id, '{'nba_api' if table == 'current_games' else 'espn'}' expected_provider FROM {table} c LEFT JOIN fact_revisions f ON c.game_uid=f.game_uid AND c.revision=f.revision ORDER BY c.game_uid"
            )
        ]
        require(
            len({row["uid"] for row in rows}) == len(rows),
            "Provider selection required; overlapping appearances cannot be pooled",
        )
        for row in rows:
            require(row["payload"] is not None, "Current game points to missing facts")
            value = json.loads(row["payload"])
            require(
                value["game_uid"] == row["uid"]
                and value["provider"] == row["expected_provider"],
                "Current game identity binding differs",
            )
            require(digest(value) == row["revision"], "Current fact integrity failed")
            require(
                (value["scope"], value["scope_version"]) == self.game_scope(row["uid"]),
                "Current facts held after scope correction; reprocess complete game",
            )
            for binding in value.get("identity_bindings", []):
                require(
                    self.resolve_person(value["provider"], binding["source_player_id"])
                    == (binding["player_id"], binding["version"]),
                    "Current facts held after identity correction; reprocess complete game",
                )
            observation = self.read_observation(row["observation_id"])
            require(
                observation["semantic"] == value,
                "Current observation differs from fact",
            )
            result.append(value)
        return result

    def resolve_date_quarantine(self, input_id, observation_id):
        """Append a resolution without deleting the rejected original or reason."""
        with self.atomic():
            original = self.connection.execute(
                "SELECT * FROM quarantined_inputs WHERE input_id=?", (input_id,)
            ).fetchone()
            require(
                original is not None and original["provider"] == "nba_api",
                "Unknown NBA quarantine",
            )
            observation = self.read_observation(observation_id)
            review = observation.get("date_review")
            require(
                review is not None
                and observation["source_game_id"] == original["source_game_id"]
                and review["original_receipt"] == json.loads(original["receipt"])
                and review["original_receipt"]["sha256"]
                == hashlib.sha256(original["payload"].encode()).hexdigest(),
                "Resolution original quarantine binding differs",
            )
            uid = observation["semantic"]["game_uid"]
            current = self.connection.execute(
                "SELECT observation_id FROM current_games WHERE game_uid=?", (uid,)
            ).fetchone()
            require(
                current is not None and current[0] == observation_id,
                "Resolution must bind the current reviewed observation",
            )
            prior = self.connection.execute(
                "SELECT observation_id,review_sha256 FROM quarantine_resolutions WHERE input_id=?",
                (input_id,),
            ).fetchone()
            expected = (observation_id, review["review_sha256"])
            require(
                prior is None or tuple(prior) == expected,
                "Conflicting quarantine resolution",
            )
            self.connection.execute(
                "INSERT OR IGNORE INTO quarantine_resolutions VALUES(?,?,?)",
                (input_id, *expected),
            )

    def unresolved_quarantines(self):
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT q.input_id,q.provider,q.source_game_id,q.reason FROM quarantined_inputs q LEFT JOIN quarantine_resolutions r USING(input_id) WHERE r.input_id IS NULL ORDER BY q.input_id"
            )
        ]

    def player_log(self, player_id, *, season=None, provider=None):
        # Offline inspection only: never used by existing Ask/Player routes.
        result = []
        for game in self.current_semantics(provider):
            if season is not None and game["scope"]["season"] != season:
                continue
            for player in game["appearances"]:
                if player["player_id"] == player_id:
                    result.append(
                        {
                            "game_uid": game["game_uid"],
                            "date": game["scope"]["date"],
                            "season": game["scope"]["season"],
                            "phase": game["scope"]["phase"],
                            "provider": game["provider"],
                            "revision": digest(game),
                            **player,
                        }
                    )
        return sorted(result, key=lambda r: (r["date"], r["game_uid"]))

    def player_summary(self, player_id, *, season=None, provider=None):
        rows = self.player_log(player_id, season=season, provider=provider)
        totals: dict[str, int] = {}
        valid: dict[str, int] = {}
        for row in rows:
            for key, value in row["stats"].items():
                if value is not None:
                    totals[key] = totals.get(key, 0) + value
                    valid[key] = valid.get(key, 0) + 1
        return {
            "player_id": player_id,
            "seasons": sorted({row["season"] for row in rows}),
            "providers": sorted({row["provider"] for row in rows}),
            "games": len(rows),
            "totals": totals,
            "valid_appearances": valid,
            "coverage": "observed_sample",
            "runtime_eligible": False,
        }
