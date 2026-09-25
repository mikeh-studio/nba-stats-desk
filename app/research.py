"""Shared, bounded research queries for pages and Ask; no model-authored SQL."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agent.semantic_serving import source_players, warehouse_for_repository
from app.agent.semantic_source import load_snapshot
from app.agent.semantics import Evidence, Query, SemanticError, load_contract, run_query
from app.seasons import validate_season

CORE_METRICS = ("pts", "reb", "ast", "stl", "blk", "tov", "fg3m", "min")
SHOOTING_METRICS = (
    "fga",
    "fg3a",
    "fta",
    "fg_pct",
    "fg3_pct",
    "ft_pct",
    "ts_pct",
    "efg_pct",
)
RESEARCH_METRICS = (*CORE_METRICS, *SHOOTING_METRICS, "pts_per36", "ast_per36")


class ResearchQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    season: str = "2025-26"
    phase: Literal["Regular Season", "Playoffs", "Both"] = "Regular Season"
    player_ids: list[int] = Field(min_length=1, max_length=2)
    metrics: list[str] = Field(
        default_factory=lambda: list(CORE_METRICS), min_length=1, max_length=18
    )
    aggregation: Literal["average", "total"] = "average"
    start: date | None = None
    end: date | None = None
    opponent: str | None = Field(default=None, pattern=r"^[A-Z]{2,3}$")
    home_away: Literal["home", "away"] | None = None
    rest: Literal["back_to_back", "one_day", "two_plus"] | None = None
    teammate_id: int | None = Field(default=None, gt=0)
    teammate_status: (
        Literal["participated", "reported_out_no_appearance", "unknown", "conflicting"]
        | None
    ) = None

    @model_validator(mode="after")
    def valid_scope(self):
        validate_season(self.season)
        if any(p <= 0 for p in self.player_ids) or len(set(self.player_ids)) != len(
            self.player_ids
        ):
            raise ValueError("Specify one or two distinct positive player IDs")
        if len(set(self.metrics)) != len(self.metrics) or not set(self.metrics) <= set(
            RESEARCH_METRICS
        ):
            raise ValueError("Unsupported or duplicate research metric")
        if self.start and self.end and self.start > self.end:
            raise ValueError("Start must precede end")
        if (self.teammate_id is None) != (self.teammate_status is None):
            raise ValueError("Teammate ID and status must be supplied together")
        if self.teammate_id in self.player_ids:
            raise ValueError("Focal and teammate must differ")
        return self


def load_research_evidence(settings: Any, repo: Any, season: str) -> Evidence:
    if settings.research_snapshot_path:
        return load_snapshot(Path(settings.research_snapshot_path))[1]
    return warehouse_for_repository(repo).load([season])[1]


def load_context(path: str | None) -> list[dict[str, Any]]:
    if not path:
        return []
    from app.research_snapshots import read_snapshot

    return read_snapshot(Path(path))["rows"]


def breakdown(
    evidence: Evidence,
    query: ResearchQuery,
    context: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    evidence.validate()
    players = {p["player_id"]: p for p in source_players(evidence)}
    if any(p not in players for p in query.player_ids):
        raise SemanticError(
            "unknown_player", "Player is not present in the selected evidence"
        )
    context_index = {}
    game_context: dict[tuple, dict] = {}
    for r in context or []:
        context_key = (r["season"], r["game_id"], r["player_id"], r.get("teammate_id"))
        if context_key in context_index:
            raise SemanticError("duplicate_grain", "Duplicate research context")
        context_index[context_key] = r
        shared = game_context.setdefault(context_key[:3], {})
        for field in ("home_away", "rest_days"):
            value = r.get(field)
            if value is not None:
                if field in shared and shared[field] != value:
                    raise SemanticError(
                        "conflicting_context", "Conflicting game context"
                    )
                shared[field] = value
    # Derive rest from all observed team games BEFORE player/date/opponent filters.
    team_dates: dict[tuple[str, str, str], set[str]] = {}
    for fact in evidence.rows:
        team_dates.setdefault(
            (fact["season"], fact["season_type"], fact["team_abbr"]), set()
        ).add(str(fact["game_date"]))
    rest_by_game = {}
    for team_key, values in team_dates.items():
        dates = sorted(values)
        for prior, current in zip(dates, dates[1:]):
            rest_by_game[(*team_key, current)] = (
                date.fromisoformat(current) - date.fromisoformat(prior)
            ).days - 1
    selected, missing = [], {str(p): 0 for p in query.player_ids}
    phases = ("Regular Season", "Playoffs") if query.phase == "Both" else (query.phase,)
    for row in evidence.rows:
        if (
            row["season"] != query.season
            or row["season_type"] not in phases
            or row["player_id"] not in query.player_ids
        ):
            continue
        day = date.fromisoformat(str(row["game_date"]))
        if (
            (query.start and day < query.start)
            or (query.end and day > query.end)
            or (query.opponent and row.get("opponent_abbr") != query.opponent)
        ):
            continue
        ctx = context_index.get(
            (query.season, row["game_id"], row["player_id"], query.teammate_id), {}
        )
        ctx = {
            **game_context.get((query.season, row["game_id"], row["player_id"]), {}),
            **ctx,
        }
        home = str(row.get("home_away") or ctx.get("home_away") or "").lower()
        rest = ctx.get(
            "rest_days",
            rest_by_game.get(
                (
                    row["season"],
                    row["season_type"],
                    row["team_abbr"],
                    str(row["game_date"]),
                )
            ),
        )
        status = ctx.get("teammate_status")
        if (
            (query.home_away and home not in ("home", "away"))
            or (query.rest and rest is None)
            or (query.teammate_status and status is None)
        ):
            missing[str(row["player_id"])] += 1
            continue
        if query.home_away and home != query.home_away:
            continue
        if (
            query.rest
            and not {
                "back_to_back": rest == 0,
                "one_day": rest == 1,
                "two_plus": rest is not None and rest >= 2,
            }[query.rest]
        ):
            continue
        if query.teammate_status and status != query.teammate_status:
            continue
        selected.append(dict(row))
    if not selected and any(missing.values()):
        raise SemanticError(
            "unsupported_coverage",
            "Requested filters lack context coverage; no substitute scope was used",
        )
    contract = load_contract()
    anchor = query.end or max(
        (
            date.fromisoformat(v)
            for (s, p), v in evidence.data_through.items()
            if s == query.season and p in phases
        ),
        default=None,
    )
    if anchor is None:
        raise SemanticError("unsupported_coverage", "Season or phase is unavailable")
    filtered = replace(evidence, rows=selected)
    output = []
    for pid in query.player_ids:
        metric_rows = []
        for key in query.metrics:
            metric = contract.metric(key)
            result = run_query(
                filtered,
                Query(
                    metric=key,
                    season=query.season,
                    season_type=query.phase,
                    aggregation="ratio" if metric.denominator else query.aggregation,
                    player_id=pid,
                    as_of=anchor.isoformat(),
                    window="date_range" if query.start else "season_to_date",
                    start_date=query.start.isoformat() if query.start else None,
                ),
            )
            item = next((r for r in result["rows"] if r["player_id"] == pid), None)
            metric_rows.append(
                {
                    "metric": key,
                    "label": metric.label,
                    "unit": "percent" if metric.unit == "ratio" else metric.unit,
                    "aggregation": "ratio" if metric.denominator else query.aggregation,
                    "result": item,
                }
            )
        output.append(
            {
                "player_id": pid,
                "player_name": players[pid]["player_name"],
                "metrics": metric_rows,
                "filter_missing_games": missing[str(pid)],
                "games": [r for r in selected if r["player_id"] == pid],
            }
        )
    scope = query.model_dump(mode="json")
    return {
        "version": 1,
        "scope": scope,
        "players": output,
        "contract_version": contract.version,
        "snapshot_id": evidence.snapshot_id,
        "query_id": hashlib.sha256(
            json.dumps(
                [evidence.snapshot_id, scope, context or []], sort_keys=True
            ).encode()
        ).hexdigest(),
        "source": evidence.source,
        "data_through": {f"{s} {p}": v for (s, p), v in evidence.data_through.items()},
        "limitations": [
            "Retrospective descriptive statistics, not causal effects or a historical knowledge replay.",
            "Rest is derived from observed team game dates unless reviewed schedule context is provided.",
            "Missing appearances and filter context are not zero production.",
        ],
    }


def answer_payload(result: dict[str, Any]) -> dict[str, Any]:
    rows = []
    for p in result["players"]:
        for m in p["metrics"]:
            r = m["result"] or {}
            rows.append(
                [
                    p["player_name"],
                    m["label"],
                    r.get("display_value"),
                    m["unit"],
                    m["aggregation"],
                    r.get("valid_games", 0),
                    r.get("observed_games", 0),
                    p["filter_missing_games"],
                ]
            )
    scope = result["scope"]
    return {
        "answer": f"Research breakdown for {', '.join(p['player_name'] for p in result['players'])}: {scope['season']} {scope['phase']}, {scope['start'] or 'season start'} through {scope['end'] or 'latest source date'}. The table uses the selected filters and shows each metric's valid sample.",
        "tables": [
            {
                "title": "Research breakdown",
                "columns": [
                    {"key": k, "label": label}
                    for k, label in zip(
                        (
                            "player",
                            "metric",
                            "value",
                            "unit",
                            "aggregation",
                            "valid",
                            "observed",
                            "missing",
                        ),
                        (
                            "Player",
                            "Metric",
                            "Value",
                            "Unit",
                            "Aggregation",
                            "Valid games",
                            "Observed games",
                            "Missing filter context",
                        ),
                    )
                ],
                "rows": rows,
            }
        ],
        "charts": [],
        "followups": [],
        "tool_calls": [],
        "clarification_options": [],
        "assumptions": result["limitations"],
        "research": result,
        "research_scope": scope,
    }
