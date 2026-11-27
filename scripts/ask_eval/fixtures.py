"""Frozen sources and controlled planner. Never used by the public app."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import fields, replace
from datetime import datetime
from pathlib import Path
from threading import Lock
from types import SimpleNamespace


def source_dataset_for_variant(source, variant):
    if source.get("source_kind") == "production_snapshot":
        return "snapshot", source.get("snapshot", {}).get("rows", [])
    if variant == "rich_league":
        return "rich_league_rows", source.get("rich_league_rows", [])
    return "rows", source.get("rows", [])


class FrozenWarehouse:
    def __init__(self, source, variant):
        _, self.rows = source_dataset_for_variant(source, variant)
        self.variant = variant

    def load(self, seasons):
        from app.agent.semantics import Evidence, SemanticError

        requested = set(seasons)
        supported = {row["season"] for row in self.rows}
        if not requested or not requested.issubset(supported):
            raise SemanticError(
                "unsupported_coverage",
                f"Frozen fixture covers only {', '.join(sorted(supported)) or 'no seasons'}.",
            )
        rows = copy.deepcopy([row for row in self.rows if row["season"] in requested])
        if self.variant == "missing_points":
            rows[0]["pts"] = None
        if self.variant == "duplicate":
            rows.append(dict(rows[0]))
        if self.variant == "ambiguous":
            for row in rows:
                if row["player_id"] == 202:
                    row["player_name"] = "Avery Example"
        coverage = {}
        for row in rows:
            key = (row["season"], row["season_type"])
            coverage[key] = max(coverage.get(key, ""), row["game_date"])
        digest = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()
        return {
            "capture": {"query_id": "synthetic-" + digest, "query_count": 0}
        }, Evidence(
            rows,
            frozenset(coverage),
            "synthetic/ask-harness",
            digest,
            coverage,
            complete=True,
        )


def frozen_repository(settings, source, variant):
    from app.repository import BigQueryWarehouseRepository

    warehouse = FrozenWarehouse(source, variant)
    rich = variant == "rich_league"
    repository_rows = (
        sorted(warehouse.rows, key=lambda row: (row["game_date"], row["game_id"]))
        if rich
        else warehouse.rows
    )
    latest_season = max(row["season"] for row in repository_rows) if rich else "2024-25"

    class Repository(BigQueryWarehouseRepository):
        def __init__(self):
            super().__init__(settings, client=SimpleNamespace())
            self._governed_warehouse = warehouse

        def _query(self, *args, **kwargs):
            from scripts.ask_eval.source_guard import uncaptured_read

            uncaptured_read("synthetic")

        def get_health(self):
            if rich:
                rows = [
                    row for row in repository_rows if row["season"] == latest_season
                ]
                dates = [row["game_date"] for row in rows]
                return {
                    "season": latest_season,
                    "status": "fixture",
                    "is_fresh": False,
                    "checked_at_utc": max(dates) + "T00:00:00Z",
                    "season_coverage": {
                        "season": latest_season,
                        "first_game_date": min(dates),
                        "latest_game_date": max(dates),
                        "game_count": len({row["game_id"] for row in rows}),
                        "player_game_rows": len(rows),
                        "season_types": sorted({row["season_type"] for row in rows}),
                        "is_full_season": False,
                    },
                }
            return {
                "season": "2024-25",
                "status": "fixture",
                "is_fresh": False,
                "checked_at_utc": "2025-04-17T00:00:00Z",
                "season_coverage": {
                    "season": "2024-25",
                    "first_game_date": "2025-04-10",
                    "latest_game_date": "2025-04-17",
                    "game_count": 8,
                    "player_game_rows": 24,
                    "season_types": ["Regular Season", "Playoffs"],
                    "is_full_season": False,
                },
            }

        def search_players(self, query, limit=12, **kwargs):
            players = {
                r["player_id"]: {
                    "player_id": r["player_id"],
                    "player_name": r["player_name"],
                    "team_abbr": r["team_abbr"],
                    "is_ranked": True,
                }
                for r in repository_rows
            }
            return [
                p for p in players.values() if query.lower() in p["player_name"].lower()
            ][:limit]

        def get_player_detail(self, player_id):
            rows = [r for r in repository_rows if r["player_id"] == player_id]
            if not rows:
                return None
            neighbors = copy.deepcopy(source["neighbors"].get(str(player_id), []))
            if variant == "invalid_similarity" and neighbors:
                neighbors[0]["similarity_score"] = 2
            if variant == "missing_similarity":
                neighbors = []
            return {
                "player": {
                    k: rows[-1 if rich else 0][k]
                    for k in ("player_id", "player_name", "season", "team_abbr")
                },
                "similar_players": neighbors,
                "panel_states": {"similarity": "fresh" if neighbors else "unavailable"},
                "game_log": {"games": rows},
                "chart_baselines": {} if rich else {"pts": {"value": 35 / 3}},
                "sample": {"games_sampled": len(rows) if rich else 6},
            }

        def get_player_game_log(self, player_id, limit=10, **kwargs):
            rows = [r for r in repository_rows if r["player_id"] == player_id]
            if rich:
                start, end = kwargs.get("start_date"), kwargs.get("end_date")
                rows = [
                    row
                    for row in rows
                    if (not start or row["game_date"] >= start)
                    and (not end or row["game_date"] <= end)
                ]
            return {"games": rows[-limit:]}

    return Repository()


class ControlledModel:
    """Return predeclared plans, not computed expected results.

    Legacy narrative is a visibly controlled placeholder. A missing governed
    contract on baseline is a contract gap, not a measured model hallucination.
    """

    def __init__(self, turn):
        self.turn = turn
        self.responses = self

    def with_options(self, **kwargs):
        return self

    def create(self, **kwargs):
        name = kwargs.get("text", {}).get("format", {}).get("name", "")
        if name == "semantic_intent_v3":
            plan = self.turn.get("planner_response")
            if plan is None:
                raise ValueError("No controlled plan was declared for this turn")
            value = {"plan": plan}
        elif name == "research_intent_v3":
            value = {"request": self.turn["research_response"]}
        elif name == "teammate_study_answer":
            value = self.turn["study_response"]
        elif name == "visualization_choice":
            values = kwargs["text"]["format"]["schema"]["properties"]["chart_id"][
                "enum"
            ]
            value = {"chart_id": values[0]}
        else:
            value = {
                "answer": "Controlled legacy narrative; statistical prose is not scored as a real model run.",
                "tables": [],
                "charts": [],
                "assumptions": [],
                "metric_definitions": [],
                "followups": [],
            }
        return SimpleNamespace(
            output_text=json.dumps(value),
            output=[],
            usage=SimpleNamespace(input_tokens=0, output_tokens=0, total_tokens=0),
        )


def expectation_digest(case):
    """Bind review to the authored expectations, including recovery inputs."""
    expectation = {
        key: case.get(key)
        for key in ("id", "family", "variant_of", "source_variant", "attachments")
    }
    expectation["turns"] = [
        {
            key: turn.get(key)
            for key in (
                "question",
                "checks",
                "expected_failure",
                "reset_context",
                "client_context",
            )
        }
        for turn in case["turns"]
    ]
    canonical = json.dumps(
        expectation,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def approval_issue(case):
    review = case["review"]
    if not (review.get("reviewer") or "").strip():
        return "requires a non-empty reviewer"
    reviewed_at = review.get("reviewed_at")
    try:
        if not reviewed_at or "T" not in reviewed_at:
            raise ValueError
        datetime.fromisoformat(reviewed_at)
    except ValueError:
        return "requires an ISO 8601 reviewed_at timestamp"
    if review.get("expectation_sha256") != expectation_digest(case):
        return "expectation_sha256 does not match the case expectations"
    return None


def validate_cases(document, schema):
    import jsonschema

    jsonschema.validate(document, schema)
    ids = [c["id"] for c in document["cases"]]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate evaluation case IDs")
    by_id = {c["id"]: c for c in document["cases"]}
    for case in document["cases"]:
        parent = case.get("variant_of")
        if parent and (parent not in by_id or by_id[parent].get("variant_of")):
            raise ValueError("Paraphrase must reference a distinct scenario")
        for turn in case["turns"]:
            paths = [c["path"] for c in turn["checks"]]
            if len(paths) != len(set(paths)):
                raise ValueError("Duplicate assertion paths")
        if document["schema_version"] == 2 and case["review"]["status"] == "approved":
            if issue := approval_issue(case):
                raise ValueError(f"Approval for {case['id']} {issue}")
    if document["human_reviewed"] and not all(
        c["review"]["status"] == "approved" for c in document["cases"]
    ):
        raise ValueError("A human-reviewed set requires approval of every case")


def bundled_schema_path(root: Path, document) -> Path:
    filename = (
        "schema.v1.json" if document.get("schema_version") == 1 else "schema.json"
    )
    return root / "tests/fixtures/ask" / filename


def schema_path_for(root: Path, document, input_dir: Path | None = None) -> Path:
    if input_dir is not None:
        input_schema = input_dir / "schema.json"
        if input_schema.is_file():
            return input_schema
    return bundled_schema_path(root, document)


def load_inputs(root: Path, input_dir: Path | None = None):
    import jsonschema

    folder = input_dir or root / "tests/fixtures/ask"
    document = json.loads((folder / "cases.json").read_text())
    bundled_schema = bundled_schema_path(root, document)
    schema_path = schema_path_for(root, document, input_dir)
    validate_cases(document, json.loads(bundled_schema.read_text()))
    if schema_path != bundled_schema:
        jsonschema.validate(document, json.loads(schema_path.read_text()))
    source = json.loads((folder / "source.json").read_text())
    if source.get("source_kind") == "production_snapshot":
        from scripts.ask_eval.production import validate_production_source

        validate_production_source(source)
    return source, document


def fixture_artifacts(source, directory, attachments=None):
    """Materialize synthetic attachments in an evaluation-owned temporary directory.

    Hashes are calculated over frozen inputs, not application-computed answers.
    Production captures never use this adapter.
    """
    paths = {}
    for key in attachments if attachments is not None else ("availability", "study"):
        if key not in source:
            continue
        value = copy.deepcopy(source[key])
        if ATTACHMENT_SETTINGS.get(key) == "research_availability_path":
            # A study-only or attachment-free baseline need not provide these modules.
            from app.agent.semantic_source import snapshot_digest
            from app.research_snapshots import digest

            value["stats"]["sha256"] = snapshot_digest(value["stats"])
            value["sha256"] = digest(value)
        path = Path(directory) / (key + ".json")
        path.write_text(json.dumps(value))
        paths[key] = str(path)
    return paths


ATTACHMENT_SETTINGS = {
    "availability": "research_availability_path",
    "rich_availability": "research_availability_path",
    "study": "agent_teammate_study_path",
}


class FixtureCapabilityError(ValueError):
    """This application revision cannot consume a requested frozen attachment."""

    def __init__(self, attachment, reason):
        self.detail = {
            "code": "evaluation_fixture_capability_unavailable",
            "attachment": attachment,
            "reason": reason,
        }
        super().__init__(f"{attachment}: {reason}")


class FixtureAttachments:
    """Lazy, case-scoped attachment setup across baseline application versions."""

    def __init__(self, source, directory):
        self.source = source
        self.directory = directory
        self.paths = {}
        self.lock = Lock()

    def configure(self, base, attachments):
        if not attachments:
            return base
        supported = {field.name for field in fields(base)}
        # Check the whole request before preparing any files or applying settings.
        selected_settings = set()
        for key in attachments:
            setting = ATTACHMENT_SETTINGS.get(key)
            if setting is None or setting not in supported:
                raise FixtureCapabilityError(key, "application_setting_missing")
            if (
                self.source.get("source_kind") == "production_snapshot"
                or key not in self.source
            ):
                raise FixtureCapabilityError(key, "frozen_attachment_missing")
            if setting in selected_settings:
                raise FixtureCapabilityError(key, "conflicting_attachment_setting")
            selected_settings.add(setting)
        with self.lock:
            for key in attachments:
                if key not in self.paths:
                    try:
                        self.paths.update(
                            fixture_artifacts(self.source, self.directory, [key])
                        )
                    except ImportError as exc:
                        raise FixtureCapabilityError(
                            key, "application_attachment_module_missing"
                        ) from exc
            return replace(
                base,
                **{ATTACHMENT_SETTINGS[key]: self.paths[key] for key in attachments},
            )


def prepared_fixture_repository(repository, source, variant):
    """Extend frozen endpoint evaluation with certified serving; no cloud reads."""
    from dataclasses import replace
    from datetime import datetime

    from app.agent.semantic_serving import source_players
    from app.agent.semantic_source import snapshot_digest
    from app.agent.semantics import load_contract
    from app.agent.serving_runtime import PilotPublication
    from app.agent.serving_warehouse import ServingWarehouse

    if variant != "complete":
        return repository  # adversarial variants keep their original source failure
    facts = repository._governed_warehouse
    _, evidence = facts.load([source.get("season", "2024-25")])
    captured = "2026-10-22T00:00:00+00:00"
    now = datetime.fromisoformat(captured)
    snapshot = dict(
        rows=list(evidence.rows),
        sources=[evidence.source],
        source_timestamp=captured,
        coverage=[
            dict(
                season=s,
                phase=p,
                rows=sum(
                    r["season"] == s and r["season_type"] == p for r in evidence.rows
                ),
                data_through=d,
            )
            for (s, p), d in evidence.data_through.items()
        ],
        cohort_complete=True,
        position_context=evidence.position_context,
        capture={"query_count": 0},
    )
    snapshot["sha256"] = snapshot_digest(snapshot)
    pub = PilotPublication.build(
        snapshot,
        expected_snapshot_id=snapshot["sha256"],
        publication_id="frozen-evaluation",
        relation_version="frozen-evaluation/2",
        now=now,
        metrics=list(load_contract().metrics),
        include_detail=True,
    )

    class Reader:
        def load(self, *, now, include_detail=False):
            return pub if include_detail else replace(pub, detail_json=None)

    class Facts:
        def load(self, seasons):
            return facts.load(seasons)

        def players(self, evidence):
            return source_players(evidence)

    repository._governed_warehouse = ServingWarehouse(
        Facts(),
        Reader(),
        lambda: now,
        mode="serve",
        reuse_cohort=repository.settings.ask_cohort_cache_enabled,
        families=("points", "metrics", "rankings", "detail", "rich"),
    )
    return repository
