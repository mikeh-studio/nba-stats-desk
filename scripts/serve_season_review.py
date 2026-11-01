#!/usr/bin/env python3
"""Local read-only API review: historical NBA archive and explicit ESPN source.

Uses the standalone reference planner with no model credentials or dotenv.
Does not replace a deployed service or an existing local/private runtime.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--port", type=int, default=8013)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z][a-z0-9-]{4,62}", args.project):
        parser.error("Invalid project identifier")
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import uvicorn
    from app import main as api
    from app.config import Settings

    settings = Settings(
        project_id=args.project,
        gold_dataset="nba_gold",
        metadata_dataset="nba_metadata",
        freshness_threshold_hours=240,
        max_search_results=12,
        ask_local_espn_dev=True,
        openai_agent_enabled=True,
        agent_planner_enabled=False,
        performance_cache_prewarm_enabled=False,
        agent_rate_limit_per_minute=0,
        agent_rate_limit_daily=0,
        agent_request_deadline_seconds=180,
    )
    dependency = api.get_settings
    api.app.dependency_overrides[dependency] = lambda: settings
    api.get_settings = lambda: settings

    from app.agent.espn_dev_source import ESPNDevSource
    from app.agent.semantic_source import BigQuerySemanticSource, snapshot_evidence
    from app.agent.semantics import SemanticError
    from app.seasons import DEFAULT_SEASON, validate_season
    from fastapi import HTTPException, Query
    from google.api_core.exceptions import GoogleAPICallError
    from google.cloud import bigquery

    @api.app.get("/api/season-review")
    def season_review(
        season: str = Query(default=DEFAULT_SEASON),
        player_id: int = Query(default=2544, ge=1),
        limit: int = Query(default=10, ge=1, le=82),
    ):
        """Bounded player-game read with explicit source and coverage metadata."""
        try:
            validate_season(season)
            base = BigQuerySemanticSource(
                bigquery.Client(project=args.project), project=args.project
            )
            source = ESPNDevSource(base) if season == DEFAULT_SEASON else base
            value = source.capture([season])
            evidence = snapshot_evidence(value)
        except (ValueError, SemanticError) as exc:
            raise HTTPException(
                422, "Requested season evidence is unavailable or invalid"
            ) from exc
        except (GoogleAPICallError, TimeoutError) as exc:
            raise HTTPException(
                503, "Season source is temporarily unavailable"
            ) from exc
        rows = [r for r in evidence.rows if r["player_id"] == player_id]
        rows.sort(key=lambda r: (r["game_date"], r["game_id"]), reverse=True)
        if not rows:
            raise HTTPException(
                404, "No mapped player appearances in the accepted evidence"
            )
        return {
            "season": season,
            "provider": "espn" if season == DEFAULT_SEASON else "nba_warehouse_archive",
            "sources": value["sources"],
            "coverage": value["coverage"],
            "snapshot_id": value["sha256"],
            "cohort_complete": evidence.cohort_complete,
            "coverage_notes": value.get("coverage_notes", []),
            "review_metadata": {
                k: v
                for k, v in value.get("review_metadata", {}).items()
                if k != "revisions"
            },
            "player_id": player_id,
            "appearances": len(rows),
            "items": rows[:limit],
        }

    uvicorn.run(api.app, host="127.0.0.1", port=args.port, proxy_headers=False)


if __name__ == "__main__":
    main()
