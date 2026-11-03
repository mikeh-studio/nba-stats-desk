"""Synthetic semantic factories; source and policy profiles remain explicit."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from app.agent.semantics import Evidence
from app.config import Settings
from tests.semantic_intent_fixtures import migrate_legacy_plan


def settings(season="2024-25"):
    return Settings(
        project_id="test-project",
        gold_dataset="nba_gold",
        metadata_dataset="nba_metadata",
        freshness_threshold_hours=36,
        max_search_results=12,
        season=season,
        openai_api_key="fixture-key",
        agent_rate_limit_per_minute=0,
        agent_rate_limit_daily=0,
    )


class Warehouse:
    def __init__(self, error=None):
        self.error = error
        self.calls = []
        fixture = json.loads(
            (
                Path(__file__).resolve().parents[1] / "fixtures/semantics/cases.json"
            ).read_text()
        )
        self.rows = [
            dict(
                r,
                player_name={1: "Regular Player", 2: "Small Sample", 3: "Third Player"}[
                    r["player_id"]
                ],
            )
            for r in fixture["rows"]
        ]
        self.coverage = fixture["coverage"]

    def load(self, seasons):
        self.calls.append(seasons)
        if self.error:
            raise self.error
        scopes = frozenset((s, p) for s, p, _ in self.coverage if s in seasons)
        evidence = Evidence(
            [r for r in self.rows if r["season"] in seasons],
            scopes,
            "fixture/games",
            "frozen-fixture",
            {(s, p): d for s, p, d in self.coverage if s in seasons},
            complete=True,
        )
        return {
            "capture": {
                "query_id": "fixture-job",
                "bytes_processed": 123,
                "query_count": 1,
            }
        }, evidence


class Model:
    def __init__(self, metric="pts", **query):
        self.metric = metric
        self.query = query
        self.calls = []
        self.responses = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        content = json.loads(kwargs["input"][0]["content"])
        self.content = content
        queries = [
            {
                "metric": self.metric,
                "season": content["selected_season"],
                "aggregation": "ratio" if self.metric == "fg_pct" else "average",
                "player_name": "resolved_player_2"
                if self.metric == "fg_pct"
                else "resolved_player_1",
                **self.query,
            }
        ]
        return SimpleNamespace(
            output_text=json.dumps(
                {
                    "plan": migrate_legacy_plan(
                        {"status": "query", "queries": queries, "message": ""}, kwargs
                    )
                }
            )
        )
