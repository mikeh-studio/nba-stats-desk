#!/usr/bin/env python3
"""Convert frozen gamebook extractions into a revisioned, local-only store."""

from __future__ import annotations

import argparse
import io
import json
import math
import os
import re
import sqlite3
import sys
import tempfile
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from pypdf import PdfReader
from pypdf.errors import PyPdfError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.fetch_gamebooks import load_manifest  # noqa: E402
from scripts.parse_gamebooks import METRICS, canonical, parse_action, sha  # noqa: E402

SCHEMA = ROOT / "contracts/gamebook_storage.sql"
VERSION = 1
AUTO = object()
TABLES = (
    "gamebook_documents",
    "gamebook_revisions",
    "gamebook_extraction_runs",
    "gamebook_events",
    "event_relationships",
    "gamebook_coverage",
    "gamebook_publication_history",
)


def now():
    return datetime.now(timezone.utc).isoformat()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def clock_seconds(clock):
    require(
        isinstance(clock, str) and re.fullmatch(r"\d{0,2}:\d{2}(?:\.\d+)?", clock),
        "Invalid clock",
    )
    minutes, seconds = clock.split(":")
    value = int(minutes or "0") * 60 + float(seconds)
    require(
        float(seconds) < 60 and 0 <= value <= 720, "Clock outside regulation period"
    )
    return value


def payload(result):
    # A local file path is transport metadata, not extraction identity.
    return {k: v for k, v in result.items() if k != "pdf"}


def normalize(result, pdf, identity):
    """Validate conversion, recount box metrics, and separate scoring facts/edges.

    This checks a frozen parser result, not independent PDF re-extraction or labels.
    Re-derive actions from retained PBP rows to catch missing/extra scoring facts.
    """
    require(
        result["status"] == "reconciled" and result["errors"] == [],
        "Extraction rejected",
    )
    game, extraction = result["game"], result["extraction"]
    revision = extraction["revision"]
    require(
        extraction["schema_version"] == "assist_events/1",
        "Unsupported extraction schema",
    )
    require(
        revision
        == sha(
            canonical({k: v for k, v in extraction.items() if k != "revision"}).encode()
        ),
        "Extraction revision hash mismatch",
    )
    require(
        pdf is not None and sha(pdf) == extraction["document_sha256"],
        "PDF missing or hash mismatch",
    )
    require(
        identity is not None and sha(identity) == extraction["identity_sha256"],
        "Identity missing or hash mismatch",
    )
    pages = PdfReader(io.BytesIO(pdf)).pages
    players = result["player_validation"]
    require(bool(players), "Missing player reconciliation")
    by_id = {p["player_id"]: p for p in players}
    require(
        len(by_id) == len(players)
        and all(isinstance(i, str) and i.isdigit() for i in by_id),
        "Duplicate or invalid player identity",
    )
    require(
        all(p["played"] and p["team"] in (game["away"], game["home"]) for p in players),
        "Invalid player team/participation",
    )
    rows = result["rows"]
    require(
        {r["period"] for r in rows} == {1, 2, 3, 4},
        "Only complete regulation games are supported",
    )
    for period in range(1, 5):
        for marker in ("Start of Period", "End of Period"):
            require(
                sum(marker in r["raw_text"] for r in rows if r["period"] == period)
                == 1,
                "Missing/duplicate period boundary",
            )
    events = result["scoring_events"]
    keyed = {(e["page"], e["row"], e["team"]): e for e in events}
    require(len(keyed) == len(events), "Duplicate scoring action")
    counters, scores = defaultdict(Counter), {game["away"]: 0, game["home"]: 0}
    found = set()
    for row in rows:
        for action in row["actions"]:
            parsed = parse_action(action, players)
            if parsed is None:
                continue
            locator = (row["page"], row["row"], action["team"])
            require(
                locator in keyed and locator not in found,
                "Missing/duplicate scoring event in retained rows",
            )
            found.add(locator)
            e = keyed[locator]
            require(
                all(e[k] == v for k, v in parsed.items()),
                "Scoring event differs from retained action",
            )
            require(
                e["period"] == row["period"] and e["clock"] == row["clock"],
                "Event period/clock mismatch",
            )
            scores[e["team"]] += e["points"]
            require(row["score"] is not None, "Scoring action without running score")
        if row["score"]:
            expected = dict(zip(row["score_teams"], map(int, row["score"].split("-"))))
            require(scores == expected, "Running score mismatch")
    require(len(found) == len(events), "Extra scoring events")
    require(
        result["assists"] == [e for e in events if e["assister_id"]],
        "Assist subset mismatch",
    )
    normalized_events, relationships = [], []
    for e in events:
        expected_scope = {
            "game_id": game["game_id"],
            "game_date": game["date"],
            "season": game["season"],
            "phase": "Regular Season",
            "revision": revision,
            "document_sha256": extraction["document_sha256"],
            "source_url": result["source_url"],
        }
        require(
            all(e[k] == v for k, v in expected_scope.items()), "Event scope mismatch"
        )
        require(
            e["event_id"]
            == sha(f"{revision}:{e['page']}:{e['row']}:{e['team']}".encode()),
            "Event identity mismatch",
        )
        bbox = e["bbox"]
        require(
            type(e["page"]) is int and 1 <= e["page"] <= len(pages),
            "Evidence page outside source PDF",
        )
        page = pages[e["page"] - 1]
        require(
            len(bbox) == 4
            and all(isinstance(v, (int, float)) and math.isfinite(v) for v in bbox)
            and 0 <= bbox[0] < bbox[2]
            and 0 <= bbox[1] < bbox[3]
            and bbox[2] <= float(page.mediabox.width)
            and bbox[3] <= float(page.mediabox.height),
            "Invalid evidence bounding box",
        )
        require(
            type(e["points"]) is int
            and e["points"] in ((1,) if e["kind"] == "free_throw" else (2, 3)),
            "Invalid scoring points",
        )
        stats = counters[e["scorer_id"]]
        stats["PTS"] += e["points"]
        stats["FT" if e["kind"] == "free_throw" else "FG"] += 1
        stats["3P"] += int(e["points"] == 3)
        normalized_events.append(
            {
                "game_id": e["game_id"],
                "revision": revision,
                "event_id": e["event_id"],
                "game_date": e["game_date"],
                "season": e["season"],
                "phase": e["phase"],
                "team_abbreviation": e["team"],
                "period": e["period"],
                "clock": e["clock"],
                "clock_seconds_remaining": clock_seconds(e["clock"]),
                "kind": e["kind"],
                "scorer_id": e["scorer_id"],
                "scorer_name": e["scorer"],
                "points": e["points"],
                "page": e["page"],
                "source_row": e["row"],
                "bbox_json": canonical(bbox),
                "raw_text": e["raw_text"],
            }
        )
        if e["assister_id"]:
            require(
                e["kind"] == "field_goal" and e["assister_id"] != e["scorer_id"],
                "Invalid assist",
            )
            counters[e["assister_id"]]["A"] += 1
            relationships.append(
                {
                    "game_id": e["game_id"],
                    "revision": revision,
                    "event_id": e["event_id"],
                    "relationship_type": "assist",
                    "from_player_id": e["assister_id"],
                    "from_player_name": e["assister"],
                    "to_player_id": e["scorer_id"],
                }
            )
    for p in players:
        actual = {m: counters[p["player_id"]][m] for m in METRICS}
        require(
            actual == p["stats"] == p["extracted"] and p["matches"],
            "Player box-score mismatch",
        )
    require(set(result["box_totals"]) == set(scores), "Missing team box totals")
    for team, totals in result["box_totals"].items():
        require(
            totals
            == {
                m: sum(p["stats"][m] for p in players if p["team"] == team)
                for m in METRICS
            },
            "Team box-score mismatch",
        )
    return normalized_events, relationships


class PublicationConflict(ValueError):
    pass


class GamebookStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "store.sqlite3", isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        require(version in (0, VERSION), "Unsupported storage schema version")
        self.db.executescript(SCHEMA.read_text())

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self, *, write=True):
        self.db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def rows(self, sql, parameters=()):
        return [dict(r) for r in self.db.execute(sql, parameters)]

    def insert(self, table, row):
        require(table in TABLES, "Unknown table")
        columns = list(row)
        self.db.execute(
            f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
            list(row.values()),
        )

    def artifact(self, category, data, suffix):
        relative = f"{category}/{sha(data)}.{suffix}"
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            # Stage complete bytes before making the content-addressed name visible.
            # Hard-link creation is atomic and refuses to replace an existing file.
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                require(
                    path.read_bytes() == data,
                    "Immutable artifact differs from expected bytes",
                )
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return relative

    def register_games(self, games):
        """Expected games are registered even when no extraction exists."""
        require(
            len({g["game_id"] for g in games}) == len(games), "Duplicate expected game"
        )
        with self.transaction():
            for g in games:
                row = {
                    "game_id": g["game_id"],
                    "source": "nba_gamebook_pdf",
                    "domain": "assists",
                    "game_date": g["date"],
                    "season": g["season"],
                    "phase": "Regular Season",
                    "away": g["away"],
                    "home": g["home"],
                }
                previous = self.coverage(g["game_id"])
                if previous:
                    require(
                        all(previous[k] == v for k, v in row.items()),
                        "Expected game scope changed",
                    )
                else:
                    self.insert("gamebook_coverage", {**row, "health": "pending"})

    def coverage(self, game_id):
        rows = self.rows(
            "SELECT * FROM gamebook_coverage WHERE game_id = ?", (game_id,)
        )
        return rows[0] if rows else None

    def ingest(
        self,
        result,
        *,
        pdf=None,
        identity=None,
        expected_previous=AUTO,
        reason="local snapshot conversion",
    ):
        game_id = result["game"]["game_id"]
        state = self.coverage(game_id)
        require(state is not None, "Game is outside the registered expected scope")
        expected = (
            state["current_revision"]
            if expected_previous is AUTO
            else expected_previous
        )
        started, attempt_id = now(), uuid4().hex
        input_path = self.artifact(
            "inputs", canonical(payload(result)).encode(), "json"
        )
        revision = result.get("extraction", {}).get("revision")
        attempt = {
            "attempt_id": attempt_id,
            "game_id": game_id,
            "candidate_revision": revision,
            "expected_previous_revision": expected,
            "started_at": started,
            "input_path": input_path,
            "reason": reason,
        }
        try:
            g = result["game"]
            require(
                all(
                    state[k] == g[v]
                    for k, v in (
                        ("game_date", "date"),
                        ("season", "season"),
                        ("away", "away"),
                        ("home", "home"),
                    )
                ),
                "Game scope mismatch",
            )
            document_path = (
                self.artifact("documents", pdf, "pdf") if pdf is not None else None
            )
            identity_path = (
                self.artifact("identities", identity, "json")
                if identity is not None
                else None
            )
            events, relationships = normalize(result, pdf, identity)
            with self.transaction():
                current = self.coverage(game_id)["current_revision"]
                if current != expected:
                    raise PublicationConflict(
                        "Current revision changed since candidate preparation"
                    )
                existing = self.rows(
                    "SELECT * FROM gamebook_revisions WHERE game_id = ? AND revision = ?",
                    (game_id, revision),
                )
                fingerprint = sha(canonical(payload(result)).encode())
                if existing:
                    require(
                        existing[0]["payload_sha256"] == fingerprint,
                        "Same revision has different output",
                    )
                if current == revision:
                    self.insert(
                        "gamebook_extraction_runs",
                        {
                            **attempt,
                            "status": "noop",
                            "finished_at": now(),
                            "errors_json": "[]",
                        },
                    )
                    # Do not erase a newer failure by replaying old accepted data.
                    self.db.execute(
                        "UPDATE gamebook_coverage SET last_attempt_id = ? WHERE game_id = ?",
                        (attempt_id, game_id),
                    )
                    return {
                        "attempt_id": attempt_id,
                        "status": "noop",
                        "game_id": game_id,
                    }
                require(
                    not existing,
                    "Historical revision cannot silently replace a newer revision",
                )
                extraction = result["extraction"]
                if not self.rows(
                    "SELECT 1 FROM gamebook_documents WHERE document_sha256 = ?",
                    (extraction["document_sha256"],),
                ):
                    self.insert(
                        "gamebook_documents",
                        {
                            "document_sha256": extraction["document_sha256"],
                            "artifact_path": document_path,
                            "byte_count": len(pdf),
                        },
                    )
                self.insert(
                    "gamebook_revisions",
                    {
                        "game_id": game_id,
                        "revision": revision,
                        "document_sha256": extraction["document_sha256"],
                        "payload_sha256": fingerprint,
                        "payload_path": input_path,
                        "identity_sha256": extraction["identity_sha256"],
                        "identity_path": identity_path,
                        "source_url": result["source_url"],
                        "parser_version": extraction["parser_version"],
                        "parser_sha256": extraction["parser_sha256"],
                        "pypdf_version": extraction["pypdf_version"],
                        "extraction_schema_version": extraction["schema_version"],
                        "storage_schema_version": VERSION,
                        "event_count": len(events),
                        "relationship_count": len(relationships),
                        "raw_row_count": len(result["rows"]),
                        "validation_scope": "same_document_reconciled",
                        "independent_review_status": "not_run",
                    },
                )
                for table, records in (
                    ("gamebook_events", events),
                    ("event_relationships", relationships),
                ):
                    for row in records:
                        self.insert(table, row)
                self._publish(attempt, revision)
            return {"attempt_id": attempt_id, "game_id": game_id, "status": "published"}
        except (
            ValueError,
            KeyError,
            TypeError,
            OSError,
            sqlite3.Error,
            PyPdfError,
        ) as exc:
            status = (
                "conflict"
                if isinstance(exc, PublicationConflict)
                else (
                    "failed"
                    if isinstance(exc, (OSError, sqlite3.Error))
                    else "rejected"
                )
            )
            errors = [{"type": type(exc).__name__, "message": str(exc)}]
            with self.transaction():
                self.insert(
                    "gamebook_extraction_runs",
                    {
                        **attempt,
                        "status": status,
                        "finished_at": now(),
                        "errors_json": canonical(errors),
                    },
                )
                self.db.execute(
                    "UPDATE gamebook_coverage SET last_attempt_id = ?, last_failure_attempt_id = ?, health = 'failed' WHERE game_id = ?",
                    (attempt_id, attempt_id, game_id),
                )
            return {
                "attempt_id": attempt_id,
                "game_id": game_id,
                "status": status,
                "errors": errors,
            }

    def _publish(self, attempt, revision):
        """Called inside the candidate transaction: all facts, history and pointer commit together."""
        published = now()
        self.insert(
            "gamebook_extraction_runs",
            {
                **attempt,
                "status": "published",
                "finished_at": published,
                "errors_json": "[]",
            },
        )
        self.insert(
            "gamebook_publication_history",
            {
                "publication_id": uuid4().hex,
                "game_id": attempt["game_id"],
                "previous_revision": attempt["expected_previous_revision"],
                "new_revision": revision,
                "published_at": published,
                "attempt_id": attempt["attempt_id"],
                "reason": attempt["reason"],
            },
        )
        self.db.execute(
            "UPDATE gamebook_coverage SET current_revision = ?, published_at = ?, last_attempt_id = ?, health = 'healthy' WHERE game_id = ?",
            (revision, published, attempt["attempt_id"], attempt["game_id"]),
        )

    def export(self, output):
        """One read transaction pins coverage, events, pairs, and report membership."""
        output = Path(output)
        output.mkdir(parents=True, exist_ok=False)
        with self.transaction(write=False):
            counts, files, schemas = {}, {}, {}
            for table in (*TABLES, "assist_pair_game", "current_assist_pair_game"):
                records = self.rows(f"SELECT * FROM {table}")
                body = "".join(
                    canonical(r) + "\n" for r in sorted(records, key=canonical)
                ).encode()
                (output / f"{table}.jsonl").write_bytes(body)
                counts[table] = len(records)
                files[f"{table}.jsonl"] = {"sha256": sha(body), "rows": len(records)}
                schemas[table] = self.rows(f"PRAGMA table_info({table})")
            coverage = self.rows("SELECT * FROM gamebook_coverage ORDER BY game_id")
            pairs = self.rows(
                "SELECT * FROM current_assist_pair_game ORDER BY game_id, team_abbreviation, from_player_id, to_player_id"
            )
            report = {
                "metric_version": "assist_pair_game/1",
                "source": "nba_gamebook_pdf",
                "domain": "assists",
                "coverage": coverage,
                "pairs": pairs,
            }
            report["snapshot_id"] = sha(canonical(report).encode())
            (output / "pair_report.json").write_text(canonical(report) + "\n")
            (output / "schemas.json").write_text(json.dumps(schemas, indent=2) + "\n")
            (output / "schema.sql").write_bytes(SCHEMA.read_bytes())
            artifacts = {
                name: {"sha256": sha((output / name).read_bytes())}
                for name in ("pair_report.json", "schemas.json", "schema.sql")
            }
            manifest = {
                "storage_schema_version": VERSION,
                "exported_at": now(),
                "store_root": str(self.root.resolve()),
                "schema_sha256": sha(SCHEMA.read_bytes()),
                "tables": files,
                "artifacts": artifacts,
                "counts": counts,
                "report_snapshot_id": report["snapshot_id"],
            }
            # Completion marker written last. Incomplete directories are not exports.
            (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--expected-games", required=True, type=Path)
    parser.add_argument("--identity-dir", required=True, type=Path)
    parser.add_argument("--store", required=True, type=Path)
    args = parser.parse_args()
    if not args.store.resolve().is_relative_to((ROOT / "reports").resolve()):
        parser.error("store must be inside ignored reports/")
    snapshot = json.loads(args.snapshot.read_text())
    require(
        snapshot["schema_version"] == "assist_pair_report/1",
        "Unsupported snapshot schema",
    )
    games = load_manifest(args.expected_games)
    results = snapshot["games"]
    expected = {g["game_id"] for g in games}
    ids = [r["game"]["game_id"] for r in results]
    require(
        len(ids) == len(set(ids)) and set(ids) <= expected,
        "Duplicate or unexpected snapshot game",
    )
    store = GamebookStore(args.store)
    try:
        store.artifact("manifests", args.expected_games.read_bytes(), "json")
        store.artifact("snapshots", args.snapshot.read_bytes(), "json")
        store.register_games(games)
        outcomes = []
        for result in results:
            game_id = result["game"]["game_id"]
            digest = result.get("extraction", {}).get("document_sha256", "")
            require(
                not digest or re.fullmatch(r"[0-9a-f]{64}", digest),
                "Invalid document hash",
            )
            pdf_path = args.snapshot.parent / "documents" / f"{digest}.pdf"
            identity_path = args.identity_dir / f"{game_id}-stats.json"
            outcome = store.ingest(
                result,
                pdf=pdf_path.read_bytes() if pdf_path.exists() else None,
                identity=identity_path.read_bytes() if identity_path.exists() else None,
            )
            outcomes.append(outcome)
            print(json.dumps(outcome), flush=True)
        output = (
            args.store
            / "exports"
            / (
                datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                + "-"
                + uuid4().hex[:8]
            )
        )
        store.export(output)
        print("Export:", output)
        return int(
            any(o["status"] not in ("published", "noop") for o in outcomes)
            or any(
                c["health"] != "healthy"
                for c in store.rows("SELECT * FROM gamebook_coverage")
            )
        )
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
