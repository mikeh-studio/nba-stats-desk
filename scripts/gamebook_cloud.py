#!/usr/bin/env python3
"""Publish gamebook candidates to isolated GCS/BigQuery dev storage only."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv
from google.api_core.exceptions import NotFound, PreconditionFailed
from google.cloud import bigquery, storage

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.gamebook_store import (  # noqa: E402
    SCHEMA,
    TABLES,
    GamebookStore,
    canonical,
    now,
    require,
    sha,
)

CONTRACT = ROOT / "contracts/gamebook_bigquery.json"
FACTS = (
    "gamebook_documents",
    "gamebook_revisions",
    "gamebook_events",
    "event_relationships",
)


class ConfirmedQueryFailure(RuntimeError):
    pass


def target(project, dataset):
    require(bool(re.fullmatch(r"[a-z][a-z0-9-]{4,62}", project)), "Invalid project")
    require(
        bool(re.fullmatch(r"nba_gamebook_dev(?:_[a-z0-9]+)*", dataset)),
        "Only isolated gamebook dev datasets are allowed",
    )
    return f"{project}.{dataset}"


def publication_sql(destination, staging):
    """All permanent data mutations, pointer, history and success audit are atomic."""
    require(
        all(re.fullmatch(r"candidate_[a-f0-9]+_" + t, staging[t]) for t in FACTS),
        "Invalid candidate table",
    )
    ref = lambda t: f"`{destination}.{t}`"  # noqa: E731
    stage = lambda t: ref(staging[t])  # noqa: E731
    inserts = "\n".join(
        f"INSERT INTO {ref(t)} SELECT * FROM {stage(t)};"
        for t in FACTS
        if t != "gamebook_documents"
    )
    return f"""BEGIN TRANSACTION;
UPDATE {ref("gamebook_store_control")} SET generation = generation + 1 WHERE store_id = 'main';
ASSERT @@row_count = 1 AS 'store_control_missing';
ASSERT (SELECT COUNT(*) = 1 FROM {ref("gamebook_coverage")} WHERE game_id = @game_id) AS 'expected_game_missing';
ASSERT (SELECT current_revision IS NOT DISTINCT FROM @expected FROM {ref("gamebook_coverage")} WHERE game_id = @game_id) AS 'stale_publisher';
ASSERT NOT EXISTS(SELECT 1 FROM {ref("gamebook_revisions")} WHERE game_id = @game_id AND revision = @revision AND payload_sha256 != @payload_hash) AS 'revision_output_changed';
IF (SELECT current_revision IS DISTINCT FROM @revision FROM {ref("gamebook_coverage")} WHERE game_id = @game_id) THEN
  ASSERT NOT EXISTS(SELECT 1 FROM {ref("gamebook_revisions")} WHERE game_id = @game_id AND revision = @revision) AS 'historical_revision_republish';
  ASSERT (SELECT COUNT(*) = 1 FROM {stage("gamebook_revisions")}) AS 'candidate_revision_count';
  ASSERT (SELECT COUNT(*) = 1 FROM {stage("gamebook_revisions")} WHERE game_id = @game_id AND revision = @revision AND payload_sha256 = @payload_hash AND event_count = @event_count AND relationship_count = @relationship_count) AS 'candidate_revision_missing';
  ASSERT (SELECT COUNT(*) = 1 FROM {stage("gamebook_documents")}) AS 'candidate_document_count';
  ASSERT (SELECT COUNT(*) = 1 FROM {stage("gamebook_documents")} d JOIN {stage("gamebook_revisions")} r USING(document_sha256)) AS 'candidate_document_missing';
  ASSERT (SELECT COUNT(*) FROM {stage("gamebook_events")}) = @event_count AS 'candidate_event_count';
  ASSERT (SELECT COUNT(*) FROM {stage("event_relationships")}) = @relationship_count AS 'candidate_relationship_count';
  ASSERT NOT EXISTS(SELECT 1 FROM {stage("gamebook_events")} WHERE game_id != @game_id OR revision != @revision) AS 'candidate_scope';
  ASSERT NOT EXISTS(SELECT event_id FROM {stage("gamebook_events")} GROUP BY event_id HAVING COUNT(*) != 1) AS 'duplicate_event';
  ASSERT NOT EXISTS(SELECT event_id FROM {stage("event_relationships")} GROUP BY event_id HAVING COUNT(*) != 1) AS 'duplicate_relationship';
  ASSERT NOT EXISTS(SELECT 1 FROM {stage("event_relationships")} r LEFT JOIN {stage("gamebook_events")} e USING(game_id, revision, event_id) WHERE e.event_id IS NULL OR e.kind != 'field_goal' OR r.to_player_id != e.scorer_id OR r.from_player_id = r.to_player_id OR r.relationship_type != 'assist') AS 'invalid_relationship';
  INSERT INTO {ref("gamebook_documents")} SELECT s.* FROM {stage("gamebook_documents")} s WHERE NOT EXISTS(SELECT 1 FROM {ref("gamebook_documents")} d WHERE d.document_sha256 = s.document_sha256);
  {inserts}
  INSERT INTO {ref("gamebook_publication_history")} VALUES(@attempt, @game_id, @expected, @revision, CURRENT_TIMESTAMP(), @attempt, @reason);
  UPDATE {ref("gamebook_coverage")} SET current_revision = @revision, published_at = CURRENT_TIMESTAMP(), last_attempt_id = @attempt, health = 'healthy' WHERE game_id = @game_id;
  INSERT INTO {ref("gamebook_extraction_runs")} VALUES(@attempt, @game_id, @revision, @expected, @started, CURRENT_TIMESTAMP(), 'published', @input, '[]', @reason);
ELSE
  INSERT INTO {ref("gamebook_extraction_runs")} VALUES(@attempt, @game_id, @revision, @expected, @started, CURRENT_TIMESTAMP(), 'noop', @input, '[]', @reason);
  UPDATE {ref("gamebook_coverage")} SET last_attempt_id = @attempt WHERE game_id = @game_id;
END IF;
ASSERT NOT @inject_failure AS 'injected_failure_after_publication_writes';
COMMIT TRANSACTION;
SELECT status FROM {ref("gamebook_extraction_runs")} WHERE attempt_id = @attempt;
"""


class CloudStore:
    def __init__(
        self, project, dataset, bucket, output, location="US", bq=None, gcs=None
    ):
        self.destination = target(project, dataset)
        self.project, self.dataset, self.location = project, dataset, location
        self.output = Path(output)
        self.output.mkdir(parents=True, exist_ok=False)
        self.bq = bq or bigquery.Client(project=project)
        self.gcs = gcs or storage.Client(project=project)
        self.bucket = self.gcs.get_bucket(bucket, timeout=30)
        require(
            self.bucket.location.upper() == location.upper(),
            "Bucket and dataset locations differ",
        )
        policy = self.bucket.get_iam_policy(requested_policy_version=3, timeout=30)
        require(
            not any(
                m in ("allUsers", "allAuthenticatedUsers")
                for binding in policy.bindings
                for m in binding["members"]
            ),
            "Bucket has public IAM access",
        )
        self.prefix = f"gamebooks/dev/{dataset}"
        self.schemas = {
            t: [bigquery.SchemaField.from_api_repr(f) for f in fields]
            for t, fields in json.loads(CONTRACT.read_text()).items()
        }
        self.jobs = []
        self.uploads = {}
        # Optional durable automation checkpoint, set by the unattended worker.
        self.query_checkpoint = None

    def query(self, sql, params=()):
        job_id = "gamebook_" + uuid4().hex
        record = {
            "job_id": job_id,
            "sql_sha256": sha(sql.encode()),
            "submitted_at": now(),
        }
        self.jobs.append(record)
        self.save_jobs()
        config = bigquery.QueryJobConfig(
            query_parameters=list(params),
            maximum_bytes_billed=1_000_000_000,
            labels={"purpose": "gamebook-dev"},
        )
        if self.query_checkpoint:
            self.query_checkpoint(job_id)
        job = self.bq.query(
            sql,
            job_config=config,
            job_id=job_id,
            location=self.location,
            job_retry=None,
        )
        try:
            rows = [dict(r) for r in job.result(timeout=120)]
        except Exception as exc:
            # Never label an uncertain commit as failed. Keep its known job ID for recovery.
            job.reload(timeout=30)
            record.update(state=job.state, error=job.error_result)
            self.save_jobs()
            if job.state == "DONE" and job.error_result:
                raise ConfirmedQueryFailure(str(job.error_result)) from exc
            raise RuntimeError(
                f"Unknown query outcome; inspect job {job_id} before retrying"
            ) from exc
        record.update(
            state=job.state,
            bytes_processed=job.total_bytes_processed,
            bytes_billed=job.total_bytes_billed,
        )
        self.save_jobs()
        if self.query_checkpoint:
            self.query_checkpoint(None)
        return rows

    def save_jobs(self):
        (self.output / "jobs.json").write_text(
            json.dumps(self.jobs, indent=2, default=str) + "\n"
        )

    def setup(self):
        try:
            dataset = self.bq.get_dataset(self.destination, timeout=30)
            require(
                dataset.labels.get("purpose") == "gamebook-dev"
                and dataset.location.upper() == self.location.upper(),
                "Existing dataset is not this dev store",
            )
        except NotFound:
            dataset = bigquery.Dataset(self.destination)
            dataset.location = self.location
            dataset.labels = {"purpose": "gamebook-dev", "environment": "dev"}
            dataset.description = "Private gamebook development data. Synthetic tests use a separate dataset. No production consumers."
            self.bq.create_dataset(dataset, timeout=30)
        for name, schema in self.schemas.items():
            table = self.bq.create_table(
                bigquery.Table(f"{self.destination}.{name}", schema=schema),
                exists_ok=True,
                timeout=30,
            )
            require(
                [(f.name, f.field_type, f.mode) for f in table.schema]
                == [(f.name, f.field_type, f.mode) for f in schema],
                f"Schema drift: {name}",
            )
        self.query(
            f"CREATE TABLE IF NOT EXISTS `{self.destination}.gamebook_store_control` (store_id STRING, generation INT64); INSERT INTO `{self.destination}.gamebook_store_control` SELECT 'main', 0 FROM UNNEST([1]) WHERE NOT EXISTS (SELECT 1 FROM `{self.destination}.gamebook_store_control`);"
        )
        local = sqlite3.connect(":memory:")
        local.executescript(SCHEMA.read_text())
        for name, sql in local.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='view' ORDER BY rowid"
        ):
            query = re.split(r"\bAS\b", sql, maxsplit=1)[1].strip()
            for table_name in (*TABLES, "assist_pair_game"):
                query = re.sub(
                    rf"\b{table_name}\b", f"`{self.destination}.{table_name}`", query
                )
            table = bigquery.Table(f"{self.destination}.{name}")
            table.view_query = query
            existing = self.bq.create_table(table, exists_ok=True, timeout=30)
            require(existing.view_query.strip() == query.strip(), f"View drift: {name}")
        local.close()

    def upload(self, data, category, suffix):
        digest = sha(data)
        name = f"{self.prefix}/{category}/{digest}.{suffix}"
        uri = f"gs://{self.bucket.name}/{name}"
        if uri in self.uploads:
            return uri
        blob = self.bucket.blob(name)
        try:
            options = (
                {}
                if self.bucket.iam_configuration.uniform_bucket_level_access_enabled
                else {"predefined_acl": "private"}
            )
            blob.upload_from_string(data, if_generation_match=0, timeout=60, **options)
        except PreconditionFailed:
            blob.reload(timeout=30)
        verified = blob.download_as_bytes(
            if_generation_match=int(blob.generation), timeout=60
        )
        require(sha(verified) == digest, "Existing GCS object bytes differ")
        self.uploads[uri] = {
            "generation": str(blob.generation),
            "sha256": digest,
            "bytes": len(data),
        }
        (self.output / "uploads.json").write_text(
            json.dumps(self.uploads, indent=2) + "\n"
        )
        return uri

    def parameter(self, name, value, kind="STRING"):
        return bigquery.ScalarQueryParameter(name, kind, value)

    def register(self, games):
        for g in games:
            params = [
                self.parameter(k, g[v])
                for k, v in (
                    ("game_id", "game_id"),
                    ("season", "season"),
                    ("away", "away"),
                    ("home", "home"),
                )
            ]
            params.append(self.parameter("day", g["date"], "DATE"))
            self.query(
                f"""BEGIN TRANSACTION;
UPDATE `{self.destination}.gamebook_store_control` SET generation=generation+1 WHERE store_id='main';
ASSERT NOT EXISTS(SELECT 1 FROM `{self.destination}.gamebook_coverage` WHERE game_id=@game_id AND (game_date!=@day OR season!=@season OR away!=@away OR home!=@home)) AS 'scope_changed';
INSERT INTO `{self.destination}.gamebook_coverage` (game_id,source,domain,game_date,season,phase,away,home,health)
SELECT @game_id,'nba_gamebook_pdf','assists',@day,@season,'Regular Season',@away,@home,'pending' FROM UNNEST([1])
WHERE NOT EXISTS(SELECT 1 FROM `{self.destination}.gamebook_coverage` WHERE game_id=@game_id);
COMMIT TRANSACTION;""",
                params,
            )

    def ingest(
        self,
        result,
        *,
        pdf=None,
        identity=None,
        inject_failure=False,
        expected_previous="auto",
        reason="dev gamebook publication",
    ):
        require(
            not inject_failure or self.dataset.startswith("nba_gamebook_dev_test_"),
            "Fault injection is restricted to synthetic test datasets",
        )
        attempt, started = uuid4().hex, now()
        game_id = result["game"]["game_id"]
        state = self.query(
            f"SELECT current_revision FROM `{self.destination}.gamebook_coverage` WHERE game_id=@game_id",
            [self.parameter("game_id", game_id)],
        )
        require(len(state) == 1, "Game not registered")
        expected = (
            state[0]["current_revision"]
            if expected_previous == "auto"
            else expected_previous
        )
        local = GamebookStore(self.output / attempt)
        local.register_games([result["game"]])
        outcome = local.ingest(result, pdf=pdf, identity=identity)
        local_attempt = local.rows("SELECT * FROM gamebook_extraction_runs")[0]
        frames = {t: local.rows(f"SELECT * FROM {t}") for t in FACTS}
        evidence = {}
        for category in ("inputs", "documents", "identities"):
            for path in sorted((local.root / category).glob("*")):
                evidence[str(path.relative_to(local.root))] = self.upload(
                    path.read_bytes(), category, path.suffix[1:]
                )
        self.upload(
            canonical({"attempt_id": attempt, "evidence": evidence}).encode(),
            "attempt_evidence",
            "json",
        )
        input_uri = self.upload(
            (local.root / local_attempt["input_path"]).read_bytes(), "inputs", "json"
        )
        revision = result.get("extraction", {}).get("revision")
        params = [
            self.parameter(k, v)
            for k, v in {
                "attempt": attempt,
                "game_id": game_id,
                "revision": revision,
                "expected": expected,
                "input": input_uri,
                "reason": reason,
            }.items()
        ]
        params.append(self.parameter("started", started, "TIMESTAMP"))
        try:
            if outcome["status"] != "published":
                return self.record_failure(
                    params, "rejected", local_attempt["errors_json"], attempt
                )
            if expected == revision and not inject_failure:
                # Validate the frozen output again, but avoid reload jobs for accepted facts.
                try:
                    self.query(
                        f"""BEGIN TRANSACTION;
UPDATE `{self.destination}.gamebook_store_control` SET generation=generation+1 WHERE store_id='main';
ASSERT (SELECT current_revision IS NOT DISTINCT FROM @expected FROM `{self.destination}.gamebook_coverage` WHERE game_id=@game_id) AS 'stale_publisher';
ASSERT (SELECT COUNT(*)=1 FROM `{self.destination}.gamebook_revisions` WHERE game_id=@game_id AND revision=@revision AND payload_sha256=@payload_hash) AS 'revision_output_changed';
INSERT INTO `{self.destination}.gamebook_extraction_runs` VALUES(@attempt,@game_id,@revision,@expected,@started,CURRENT_TIMESTAMP(),'noop',@input,'[]',@reason);
UPDATE `{self.destination}.gamebook_coverage` SET last_attempt_id=@attempt WHERE game_id=@game_id;
COMMIT TRANSACTION;""",
                        [
                            *params,
                            self.parameter(
                                "payload_hash",
                                frames["gamebook_revisions"][0]["payload_sha256"],
                            ),
                        ],
                    )
                    return {"attempt_id": attempt, "status": "noop", "game_id": game_id}
                except ConfirmedQueryFailure as exc:
                    return self.record_failure(
                        params, "failed", canonical([{"error": str(exc)}]), attempt
                    )
            for name in ("gamebook_documents", "gamebook_revisions"):
                for row in frames[name]:
                    for field in ("artifact_path", "payload_path", "identity_path"):
                        if field in row:
                            path = local.root / row[field]
                            row[field] = self.upload(
                                path.read_bytes(), path.parent.name, path.suffix[1:]
                            )
            staging = {}
            for name, rows in frames.items():
                table_id = f"candidate_{attempt}_{name}"
                staging[name] = table_id
                table = bigquery.Table(
                    f"{self.destination}.{table_id}", schema=self.schemas[name]
                )
                table.expires = datetime.now(timezone.utc) + timedelta(hours=24)
                self.bq.create_table(table, timeout=30)
                if rows:
                    body = "".join(canonical(row) + "\n" for row in rows).encode()
                    uri = self.upload(body, "candidates", "jsonl")
                    config = bigquery.LoadJobConfig(
                        schema=self.schemas[name],
                        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
                        write_disposition="WRITE_EMPTY",
                        max_bad_records=0,
                    )
                    job = self.bq.load_table_from_uri(
                        uri, table.reference, job_config=config, location=self.location
                    )
                    self.jobs.append(
                        {"job_id": job.job_id, "kind": "load", "table": table_id}
                    )
                    self.save_jobs()
                    try:
                        job.result(timeout=120)
                    except Exception as exc:
                        # Only isolated staging tables can have changed at this point.
                        return self.record_failure(
                            params,
                            "failed",
                            canonical(
                                [
                                    {
                                        "error": "candidate_load_failed",
                                        "job_id": job.job_id,
                                        "type": type(exc).__name__,
                                    }
                                ]
                            ),
                            attempt,
                        )
            rev = frames["gamebook_revisions"][0]
            publication_params = [
                *params,
                self.parameter("payload_hash", rev["payload_sha256"]),
                self.parameter("event_count", rev["event_count"], "INT64"),
                self.parameter(
                    "relationship_count", rev["relationship_count"], "INT64"
                ),
                self.parameter("inject_failure", inject_failure, "BOOL"),
            ]
            try:
                published = self.query(
                    publication_sql(self.destination, staging), publication_params
                )
                return {
                    "attempt_id": attempt,
                    "status": published[0]["status"],
                    "game_id": game_id,
                }
            except ConfirmedQueryFailure as exc:
                return self.record_failure(
                    params, "failed", canonical([{"error": str(exc)}]), attempt
                )
        finally:
            local.close()

    def record_failure(self, params, status, errors, attempt):
        self.query(
            f"""BEGIN TRANSACTION;
UPDATE `{self.destination}.gamebook_store_control` SET generation=generation+1 WHERE store_id='main';
INSERT INTO `{self.destination}.gamebook_extraction_runs` VALUES(@attempt,@game_id,@revision,@expected,@started,CURRENT_TIMESTAMP(),@status,@input,@errors,@reason);
UPDATE `{self.destination}.gamebook_coverage` SET last_attempt_id=@attempt,last_failure_attempt_id=@attempt,health='failed' WHERE game_id=@game_id;
COMMIT TRANSACTION;""",
            [
                *params,
                self.parameter("status", status),
                self.parameter("errors", errors),
            ],
        )
        return {"attempt_id": attempt, "status": status, "errors": json.loads(errors)}

    def snapshot(self, name):
        # Single statement pins all view results and membership to one snapshot.
        tables = (*TABLES, "current_assist_pair_game", "current_event_relationships")
        result = self.query(
            "SELECT "
            + ",".join(
                f"ARRAY(SELECT AS STRUCT * FROM `{self.destination}.{t}`) AS {t}"
                for t in tables
            )
        )[0]
        body = (json.dumps(result, indent=2, default=str) + "\n").encode()
        (self.output / f"{name}.json").write_bytes(body)
        self.upload(body, "report_snapshots", "json")
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--expected-games", type=Path, required=True)
    parser.add_argument("--identity-dir", type=Path, required=True)
    parser.add_argument("--capture", type=Path)
    parser.add_argument("--supporting-evidence", type=Path, action="append", default=[])
    parser.add_argument("--dataset", default="nba_gamebook_dev")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    require(
        args.output.resolve().is_relative_to((ROOT / "reports").resolve()),
        "Output must be private reports/",
    )
    load_dotenv(ROOT / ".env")
    from scripts.fetch_gamebooks import load_manifest

    games = load_manifest(args.expected_games)
    snapshot = json.loads(args.snapshot.read_text())
    require(
        snapshot["schema_version"] == "assist_pair_report/1", "Unsupported snapshot"
    )
    expected = {g["game_id"]: g for g in games}
    results = snapshot["games"]
    require(
        len({r["game"]["game_id"] for r in results}) == len(results)
        and all(r["game"] == expected.get(r["game"]["game_id"]) for r in results),
        "Unexpected or duplicate game",
    )
    store = CloudStore(
        os.environ["GCP_PROJECT_ID"],
        args.dataset,
        os.environ["GCS_BUCKET_NAME"],
        args.output,
        os.getenv("BQ_LOCATION", "US"),
    )
    store.setup()
    store.upload(args.expected_games.read_bytes(), "manifests", "json")
    store.upload(args.snapshot.read_bytes(), "snapshots", "json")
    provenance = {}
    evidence_files = list(args.supporting_evidence)
    if args.capture:
        capture_report = json.loads((args.capture / "report.json").read_text())
        require(
            sha((args.capture / "manifest.json").read_bytes())
            == capture_report["manifest_sha256"],
            "Capture manifest hash mismatch",
        )
        require(
            json.loads((args.capture / "manifest.json").read_text()) == games,
            "Capture scope differs",
        )
        evidence_files += [args.capture / "report.json", args.capture / "manifest.json"]
        evidence_files += [args.capture / g["game_id"] / "attempt.json" for g in games]
    for g in games:
        for suffix in ("-meta.json", "-stats.meta.json"):
            path = args.identity_dir / (g["game_id"] + suffix)
            if path.exists():
                evidence_files.append(path)
    for path in evidence_files:
        require(
            path.resolve().is_relative_to((ROOT / "reports").resolve()),
            "Supporting evidence must be inside private reports/",
        )
        provenance[str(path)] = store.upload(
            path.read_bytes(), "source_metadata", path.suffix[1:]
        )
    if provenance:
        store.upload(canonical(provenance).encode(), "source_bundles", "json")
    store.register(games)
    outcomes = []
    for r in results:
        digest = r.get("extraction", {}).get("document_sha256", "")
        require(
            not digest or re.fullmatch("[a-f0-9]{64}", digest), "Invalid document hash"
        )
        pdf = args.snapshot.parent / "documents" / f"{digest}.pdf"
        identity = args.identity_dir / f"{r['game']['game_id']}-stats.json"
        outcome = store.ingest(
            r,
            pdf=pdf.read_bytes() if pdf.exists() else None,
            identity=identity.read_bytes() if identity.exists() else None,
        )
        outcomes.append(outcome)
        print(json.dumps(outcome), flush=True)
    snapshot = store.snapshot("published")
    (args.output / "outcomes.json").write_text(json.dumps(outcomes, indent=2) + "\n")
    return int(
        any(o["status"] not in ("published", "noop") for o in outcomes)
        or any(c["health"] != "healthy" for c in snapshot["gamebook_coverage"])
    )


if __name__ == "__main__":
    sys.exit(main())
