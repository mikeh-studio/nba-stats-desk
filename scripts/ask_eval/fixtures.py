"""Frozen sources and controlled planner. Never used by the public app."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace


class FrozenWarehouse:
    def __init__(self, source, variant):
        self.source = source
        self.variant = variant

    def load(self, seasons):
        from app.agent.semantics import Evidence, SemanticError

        if "2024-25" not in seasons:
            raise SemanticError(
                "unsupported_coverage", "Frozen fixture covers only 2024-25."
            )
        rows = copy.deepcopy(self.source["rows"])
        if self.variant == "missing_points":
            rows[0]["pts"] = None
        if self.variant == "duplicate":
            rows.append(dict(rows[0]))
        if self.variant == "ambiguous":
            for row in rows:
                if row["player_id"] == 202:
                    row["player_name"] = "Avery Example"
        coverage = {
            (s, p): max(
                r["game_date"]
                for r in rows
                if r["season"] == s and r["season_type"] == p
            )
            for s in ["2024-25"]
            for p in ("Regular Season", "Playoffs")
        }
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

    class Repository(BigQueryWarehouseRepository):
        def __init__(self):
            super().__init__(settings, client=SimpleNamespace())
            self._governed_warehouse = FrozenWarehouse(source, variant)

        def get_health(self):
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
                for r in source["rows"]
            }
            return [
                p for p in players.values() if query.lower() in p["player_name"].lower()
            ][:limit]

        def get_player_detail(self, player_id):
            rows = [r for r in source["rows"] if r["player_id"] == player_id]
            if not rows:
                return None
            neighbors = copy.deepcopy(source["neighbors"].get(str(player_id), []))
            if variant == "invalid_similarity" and neighbors:
                neighbors[0]["similarity_score"] = 2
            if variant == "missing_similarity":
                neighbors = []
            return {
                "player": {
                    k: rows[0][k]
                    for k in ("player_id", "player_name", "season", "team_abbr")
                },
                "similar_players": neighbors,
                "panel_states": {"similarity": "fresh" if neighbors else "unavailable"},
                "game_log": {"games": rows},
                "chart_baselines": {"pts": {"value": 35 / 3}},
                "sample": {"games_sampled": 6},
            }

        def get_player_game_log(self, player_id, limit=10, **kwargs):
            return {
                "games": [r for r in source["rows"] if r["player_id"] == player_id][
                    -limit:
                ]
            }

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
        if name == "semantic_plan":
            plan = self.turn.get("planner_response")
            if plan is None:
                raise ValueError("No controlled plan was declared for this turn")
            value = {"plan": plan}
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


def validate_cases(document, schema):
    import jsonschema

    jsonschema.validate(document, schema)
    ids = [c["id"] for c in document["cases"]]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate evaluation case IDs")
    for case in document["cases"]:
        if (
            case["review"]["status"] != "pending"
            and not str(case["review"]["reviewer"] or "").strip()
        ):
            raise ValueError("Review decisions require a named reviewer")
        for turn in case["turns"]:
            paths = [c["path"] for c in turn["checks"]]
            if len(paths) != len(set(paths)):
                raise ValueError("Duplicate assertion paths")
    if document["human_reviewed"] and not all(
        c["review"]["status"] == "approved" for c in document["cases"]
    ):
        raise ValueError("A human-reviewed set requires approval of every case")


def load_inputs(root: Path, input_dir: Path | None = None):
    folder = input_dir or root / "tests/fixtures/ask"
    document = json.loads((folder / "cases.json").read_text())
    validate_cases(
        document, json.loads((root / "tests/fixtures/ask/schema.json").read_text())
    )
    source = json.loads((folder / "source.json").read_text())
    if source.get("source_kind") == "production_snapshot":
        from scripts.ask_eval.production import validate_production_source

        validate_production_source(source)
    return source, document
