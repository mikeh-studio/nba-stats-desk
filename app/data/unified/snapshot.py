"""Read-only retained SQLite snapshots; never initialize or migrate source files."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from .identity import canonical, digest, require
from .ledger import CONTRACTS, record
from .selection import validate_inventory
from .store import ObservationStore, parse_json


class _Reader(ObservationStore):
    def __init__(self, path):
        # Deliberately omit the writer's initializer, DDL, migrations and chmod.
        self.connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA query_only=ON")
        self.connection.execute("BEGIN")


def snapshot(path):
    path = Path(path).resolve()
    require(
        path.is_file() and path.suffix == ".sqlite" and path.stat().st_size <= 1024**3,
        "Invalid retained SQLite snapshot",
    )
    require(
        not Path(str(path) + "-wal").exists(),
        "Checkpoint and retain the source before offline selection",
    )
    with path.open("rb") as stream:
        original_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    reader = _Reader(path)
    try:
        require(
            [
                r[0]
                for r in reader.connection.execute(
                    "SELECT version FROM identity_metadata"
                )
            ]
            == ["unified_identity/1"],
            "Unknown observation registry version",
        )
        observation_validator = Draft202012Validator(
            parse_json((CONTRACTS / "observation.schema.json").read_bytes()),
            format_checker=FormatChecker(),
        )
        catalog = reader.catalog()
        observations = []
        for provider, table in (
            ("nba_api", "current_games"),
            ("espn", "current_espn_games"),
        ):
            count = reader.connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
            require(count <= 1500, "Source observation bound exceeded")
            games = reader.current_semantics(provider)
            for row in reader.connection.execute(
                f"SELECT observation_id FROM {table} ORDER BY game_uid"
            ):
                obs = reader.read_observation(row[0])
                observation_validator.validate(obs)
                sem = obs["semantic"]
                require(
                    reader.lookup(provider, obs["source_game_id"]) == sem["game_uid"],
                    "Source observation alias binding differs",
                )
                clocks = [obs["provenance"]["captured_at"]]
                if "source_chain" in obs:
                    clocks.append(obs["source_chain"]["source"]["captured_at"])
                if "date_review" in obs:
                    clocks.append(
                        obs["date_review"]["review"]["authority"]["receipt"][
                            "captured_at"
                        ]
                    )
                manifest = obs["membership"]["participant_manifest"]
                if manifest:
                    clocks.append(manifest["evidence"]["captured_at"])
                observations.append(
                    {
                        "game_uid": sem["game_uid"],
                        "source_game_id": obs["source_game_id"],
                        "provider": provider,
                        "scope": sem["scope"],
                        "fact_revision": obs["fact_revision"],
                        "observation_id": obs["observation_id"],
                        "membership": sem["membership_level"],
                        "appearances": len(sem["appearances"]),
                        "capture_times": sorted({t for t in clocks if t is not None}),
                    }
                )
            require(len(games) == count, "Source current inventory count differs")
        resolved = reader.connection.execute(
            "SELECT q.payload,q.receipt,q.source_game_id,r.observation_id,r.review_sha256 FROM quarantined_inputs q JOIN quarantine_resolutions r USING(input_id)"
        ).fetchall()
        require(len(resolved) <= 3000, "Quarantine resolution bound exceeded")
        for row in resolved:
            observation = reader.read_observation(row["observation_id"])
            review = observation.get("date_review")
            require(
                review is not None
                and observation["source_game_id"] == row["source_game_id"]
                and review["original_receipt"] == parse_json(row["receipt"])
                and review["original_receipt"]["sha256"]
                == hashlib.sha256(row["payload"].encode()).hexdigest()
                and review["review_sha256"]
                == row["review_sha256"]
                == digest(review["review"]),
                "Quarantine resolution binding differs",
            )
        holds = []
        rows = reader.connection.execute(
            "SELECT q.* FROM quarantined_inputs q LEFT JOIN quarantine_resolutions r USING(input_id) WHERE r.input_id IS NULL ORDER BY q.input_id"
        ).fetchall()
        require(len(rows) <= 3000, "Unresolved input bound exceeded")
        for row in rows:
            payload = parse_json(row["payload"])
            require(
                hashlib.sha256(row["payload"].encode()).hexdigest()
                == parse_json(row["receipt"])["sha256"],
                "Quarantine integrity failed",
            )
            game = payload.get("game", {})
            holds.append(
                {
                    "input_id": row["input_id"],
                    "provider": row["provider"],
                    "source_game_id": row["source_game_id"],
                    "season": payload.get("season"),
                    "phase": payload.get("phase"),
                    "date": game.get("game_date", game.get("date")),
                    "reason": row["reason"],
                }
            )
        value = record(
            "inventory",
            {
                "version": "observed_source_inventory/1",
                "source_snapshot": {
                    "sha256": original_hash,
                    "reference": str(path),
                    "captured_at": None,
                    "kind": "archive_snapshot",
                },
                "identity_catalog_version": catalog["catalog_version"],
                "observations": sorted(
                    observations,
                    key=lambda r: (r["scope"]["season"], r["provider"], r["game_uid"]),
                ),
                "unresolved_inputs": holds,
                "expected_universe": "not_established",
                "runtime_eligible": False,
            },
        )
        validate_inventory(value)
        return value
    finally:
        reader.close()
        with path.open("rb") as stream:
            require(
                hashlib.file_digest(stream, "sha256").hexdigest() == original_hash,
                "Source changed during offline snapshot",
            )
        require(
            not Path(str(path) + "-wal").exists(), "Source WAL appeared during snapshot"
        )


def evaluation_snapshot(
    path, inventory, requested_scope, disputes=(), revalidations=()
):
    """Retain scope observations under the exact already-pinned source hash."""
    from .evaluation import validate_inputs
    from .selection import request_scope

    requested_scope = request_scope(requested_scope)
    path = Path(path).resolve()
    expected_hash = inventory["payload"]["source_snapshot"]["sha256"]
    with path.open("rb") as stream:
        require(
            hashlib.file_digest(stream, "sha256").hexdigest() == expected_hash,
            "Evaluation source differs from pinned inventory",
        )
    require(not Path(str(path) + "-wal").exists(), "Live source WAL is unsupported")
    reader = _Reader(path)
    try:
        refs = [
            r
            for r in inventory["payload"]["observations"]
            if r["scope"]["season"] == requested_scope["season"]
            and r["scope"]["phase"] == requested_scope["phase"]
            and requested_scope["first"]
            <= r["scope"]["date"]
            <= requested_scope["last"]
        ]
        observations = [
            reader.read_observation(r["observation_id"])
            for r in sorted(refs, key=lambda r: r["observation_id"])
        ]
        value = record(
            "evaluation_inputs",
            {
                "version": "scope_observation_inputs/1",
                "inventory_id": inventory["record_id"],
                "scope": requested_scope,
                "observations": observations,
                "disputes": sorted(disputes, key=canonical),
                "revalidations": sorted(revalidations, key=canonical),
            },
            (inventory["record_id"],),
        )
        validate_inputs(value, inventory)
        return value
    finally:
        reader.close()
        with path.open("rb") as stream:
            require(
                hashlib.file_digest(stream, "sha256").hexdigest() == expected_hash,
                "Evaluation source changed during read",
            )
        require(
            not Path(str(path) + "-wal").exists(),
            "Source WAL appeared during evaluation read",
        )


def mapping_snapshot(path, inventory):
    from .pdf_checks import validate_catalog

    path = Path(path).resolve()
    expected = inventory["payload"]["source_snapshot"]["sha256"]
    with path.open("rb") as stream:
        require(
            hashlib.file_digest(stream, "sha256").hexdigest() == expected,
            "Mapping source differs from pinned inventory",
        )
    require(
        not Path(str(path) + "-wal").exists(), "Live mapping source WAL unsupported"
    )
    reader = _Reader(path)
    try:
        value = record(
            "mapping_catalog",
            {
                "version": "pdf_identity_catalog/1",
                "inventory_id": inventory["record_id"],
                "catalog": reader.catalog(),
            },
            [inventory["record_id"]],
        )
        validate_catalog(value, inventory)
        return value
    finally:
        reader.close()
        with path.open("rb") as stream:
            require(
                hashlib.file_digest(stream, "sha256").hexdigest() == expected,
                "Mapping source changed during read",
            )
        require(
            not Path(str(path) + "-wal").exists(),
            "Source WAL appeared during mapping read",
        )
