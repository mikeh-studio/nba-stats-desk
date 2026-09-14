"""Versioned, deterministic analytics over complete player-game evidence.

This contract runner is independent of the legacy Ask tools. It does not
fetch data or infer natural-language intent. Callers must supply explicit
coverage and provenance; absence of rows alone never proves coverage.
"""

from __future__ import annotations

import calendar
import math
from dataclasses import asdict, dataclass
from dataclasses import field as dataclass_field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from app.agent.formulas import evaluate_formula, extract_formula_variables
from app.seasons import validate_season

CATALOG_PATH = Path(__file__).with_name("semantic_contract.yml")
COMPONENTS = frozenset(
    "pts reb ast stl blk tov fg3m min fgm fga ftm fta fg3a plus_minus".split()
)
PHASES = ("Regular Season", "Playoffs")


class SemanticError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Metric:
    key: str
    label: str
    numerator: str
    denominator: str | None
    attempt_field: str | None
    unit: str
    direction: str

    @property
    def components(self) -> set[str]:
        return extract_formula_variables(self.numerator) | (
            extract_formula_variables(self.denominator) if self.denominator else set()
        )

    @property
    def aggregations(self) -> tuple[str, ...]:
        return ("ratio",) if self.denominator else ("total", "average")

    def public(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "components": sorted(self.components),
            "aggregations": list(self.aggregations),
            "operations": ["summary", "rank", "percentile", "game_log"],
        }


@dataclass(frozen=True)
class Contract:
    version: str
    metrics: Mapping[str, Metric]
    default_phase: str
    min_games: int
    timezone: str
    default_fantasy_metric: str

    def metric(self, key: str) -> Metric:
        if "_".join(key.casefold().split()) in {
            "fantasy",
            "fantasy_points",
            "fantasy_score",
            "fantasy_scoring",
        }:
            key = self.default_fantasy_metric
        if key not in self.metrics:
            raise SemanticError("unsupported_metric", f"Unsupported metric: {key}")
        return self.metrics[key]


def load_contract(path: Path = CATALOG_PATH) -> Contract:
    raw = yaml.safe_load(path.read_text())
    metrics = {}
    for key, config in raw["metrics"].items():
        metric = Metric(
            key=key,
            label=config["label"],
            numerator=config["numerator"],
            denominator=config.get("denominator"),
            attempt_field=config.get("attempt_field"),
            unit=config["unit"],
            direction=config["direction"],
        )
        if not metric.components <= COMPONENTS:
            raise SemanticError("invalid_contract", f"Unknown components for {key}")
        if metric.direction not in ("higher", "lower"):
            raise SemanticError("invalid_contract", f"Invalid direction for {key}")
        if metric.denominator and metric.attempt_field not in metric.components:
            raise SemanticError("invalid_contract", f"Missing attempt field for {key}")
        metrics[key] = metric
    defaults = raw["defaults"]
    if defaults["fantasy_metric"] not in metrics:
        raise SemanticError("invalid_contract", "Unknown default fantasy metric")
    return Contract(
        raw["version"],
        metrics,
        defaults["season_type"],
        defaults["min_games"],
        defaults["timezone"],
        defaults["fantasy_metric"],
    )


def _date(value: Any) -> date:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise SemanticError("invalid_scope", f"Invalid date: {value}") from exc


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise SemanticError("invalid_data", "Non-numeric metric component") from exc
    if not math.isfinite(parsed):
        raise SemanticError("invalid_data", "Non-finite metric component")
    return parsed


@dataclass(frozen=True)
class Evidence:
    rows: Sequence[Mapping[str, Any]]
    covered_scopes: frozenset[tuple[str, str]]
    source: str
    snapshot_id: str
    # Per-scope dates describe the actual source snapshot, not rebuild time.
    data_through: Mapping[tuple[str, str], str]
    model_built_at: str | None = None
    complete: bool = False

    def validate(self) -> None:
        if not self.complete:
            raise SemanticError(
                "incomplete_evidence", "Complete scope evidence is required"
            )
        if not self.source or not self.snapshot_id:
            raise SemanticError(
                "invalid_evidence", "Source and snapshot ID are required"
            )
        seen = set()
        for row in self.rows:
            key = (row.get("season"), row.get("game_id"), row.get("player_id"))
            if any(value is None or value == "" for value in key):
                raise SemanticError("invalid_data", "Missing player-game key")
            if type(key[2]) is not int or key[2] < 1:
                raise SemanticError(
                    "invalid_data", "Player IDs must be positive integers"
                )
            if not isinstance(key[1], str):
                raise SemanticError("invalid_data", "Game IDs must be strings")
            if key in seen:
                raise SemanticError(
                    "duplicate_grain", f"Duplicate player-game key: {key}"
                )
            seen.add(key)
            scope = (row["season"], row.get("season_type"))
            if scope not in self.covered_scopes or scope not in self.data_through:
                raise SemanticError("invalid_evidence", "Row outside declared coverage")
            if _date(row.get("game_date")) > _date(self.data_through[scope]):
                raise SemanticError(
                    "invalid_evidence", "Row newer than source coverage"
                )
            for field in COMPONENTS:
                value = _number(row.get(field))
                if value is not None and value < 0 and field != "plus_minus":
                    raise SemanticError("invalid_data", f"Negative {field}")
            for made, attempted in (("fgm", "fga"), ("fg3m", "fg3a"), ("ftm", "fta")):
                m, a = _number(row.get(made)), _number(row.get(attempted))
                if m is not None and a is not None and m > a:
                    raise SemanticError("invalid_data", f"{made} exceeds {attempted}")


@dataclass(frozen=True)
class Query:
    metric: str
    season: str
    aggregation: str
    season_type: str = dataclass_field(
        default_factory=lambda: load_contract().default_phase
    )
    operation: str = "summary"
    as_of: str | None = None
    window: str = "season_to_date"
    n: int | None = None
    start_date: str | None = None
    player_id: int | None = None
    team_abbr: str | None = None
    opponent_abbr: str | None = None
    min_games: int | None = None
    min_attempts: int | None = None
    direction: str | None = None
    limit: int = 10
    seasons: list[str] | None = None
    excluded_player_ids: list[int] | None = None

    def validate(self, metric: Metric) -> None:
        if self.seasons is not None:
            if (
                not isinstance(self.seasons, list)
                or not self.seasons
                or self.window != "date_range"
            ):
                raise SemanticError(
                    "invalid_scope", "Multiple seasons require an explicit date range"
                )
            for season in self.seasons:
                try:
                    validate_season(season)
                except ValueError as exc:
                    raise SemanticError("unsupported_coverage", str(exc)) from exc
        if self.excluded_player_ids is not None and (
            self.operation != "rank"
            or not isinstance(self.excluded_player_ids, list)
            or any(
                type(player) is not int or player < 1
                for player in self.excluded_player_ids
            )
        ):
            raise SemanticError(
                "invalid_scope",
                "Player exclusions require a ranking and valid identities",
            )
        try:
            validate_season(self.season)
        except ValueError as exc:
            raise SemanticError("unsupported_coverage", str(exc)) from exc
        if self.season_type not in (*PHASES, "Both"):
            raise SemanticError("unsupported_coverage", "Unsupported season phase")
        if self.operation not in ("summary", "rank", "percentile", "game_log"):
            raise SemanticError("unsupported_operation", self.operation)
        if self.operation == "game_log":
            if self.player_id is None:
                raise SemanticError("clarification_required", "Specify the player")
            if any(
                v is not None
                for v in (self.min_games, self.min_attempts, self.direction)
            ):
                raise SemanticError(
                    "invalid_scope",
                    "Game logs do not apply ranking thresholds or direction",
                )
        if self.aggregation not in metric.aggregations:
            raise SemanticError("unsupported_operation", "Invalid metric aggregation")
        if self.window not in (
            "season_to_date",
            "last_n_games",
            "prior_n_games",
            "last_n_days",
            "last_n_months",
            "last_week",
            "last_month",
            "date_range",
        ):
            raise SemanticError("invalid_scope", "Unsupported window")
        if self.window in (
            "last_n_games",
            "prior_n_games",
            "last_n_days",
            "last_n_months",
        ):
            if type(self.n) is not int or self.n < 1:
                raise SemanticError(
                    "invalid_scope", "Window requires positive integer n"
                )
        elif self.n is not None:
            raise SemanticError("invalid_scope", "n only applies to trailing windows")
        if (self.start_date is not None) != (self.window == "date_range"):
            raise SemanticError(
                "invalid_scope", "start_date requires date_range window"
            )
        for name, value, minimum in (
            ("limit", self.limit, 1),
            ("min_games", self.min_games, 1),
            ("min_attempts", self.min_attempts, 0),
            ("player_id", self.player_id, 1),
        ):
            if value is not None and (type(value) is not int or value < minimum):
                raise SemanticError("invalid_scope", f"Invalid {name}")
        if self.limit > 100:
            raise SemanticError("invalid_scope", "limit must be at most 100")
        if self.direction not in (None, "higher", "lower"):
            raise SemanticError("invalid_scope", "Invalid direction")
        if (
            self.operation in ("rank", "percentile")
            and metric.denominator
            and self.min_attempts is None
        ):
            raise SemanticError(
                "clarification_required", "Specify an attempt threshold"
            )
        if self.min_attempts is not None and not metric.denominator:
            raise SemanticError("invalid_scope", "Attempt thresholds apply to ratios")


def _window(query: Query, anchor: date) -> tuple[date | None, date]:
    start = None
    end = anchor
    if query.window == "last_n_days":
        assert query.n is not None
        start = anchor - timedelta(days=query.n - 1)
    elif query.window == "last_n_months":
        assert query.n is not None
        month_index = anchor.year * 12 + anchor.month - 1 - query.n
        year, month = divmod(month_index, 12)
        if year < 1:
            raise SemanticError("invalid_scope", "Requested month window is too large")
        month += 1
        boundary = date(
            year, month, min(anchor.day, calendar.monthrange(year, month)[1])
        )
        # Trailing interval is (the same calendar date N months ago, anchor].
        start = boundary + timedelta(days=1)
    elif query.window == "last_week":
        end = anchor - timedelta(days=anchor.weekday() + 1)
        start = end - timedelta(days=6)
    elif query.window == "last_month":
        end = anchor.replace(day=1) - timedelta(days=1)
        start = end.replace(day=1)
    elif query.window == "date_range":
        start = _date(query.start_date)
        if start > end:
            raise SemanticError("invalid_scope", "start_date is after as_of")
    return start, end


def aggregate(
    rows: Sequence[Mapping[str, Any]], metric: Metric, aggregation: str
) -> dict[str, Any]:
    valid = [
        r for r in rows if all(_number(r.get(c)) is not None for c in metric.components)
    ]
    totals = {c: math.fsum(float(r[c]) for r in valid) for c in metric.components}
    numerator = evaluate_formula(metric.numerator, totals) if valid else None
    denominator = (
        evaluate_formula(metric.denominator, totals)
        if metric.denominator and valid
        else len(valid)
        if aggregation == "average" and valid
        else None
    )
    value = numerator
    if aggregation in ("average", "ratio"):
        value = (
            numerator / denominator if numerator is not None and denominator else None
        )
    return {
        "value": value,
        "display_value": round(value * (100 if metric.unit == "ratio" else 1), 1)
        if value is not None
        else None,
        "numerator": numerator,
        "denominator": denominator,
        "observed_games": len(rows),
        "valid_games": len(valid),
        "missing_component_games": len(rows) - len(valid),
        "components": totals,
        "game_ids": [r["game_id"] for r in rows],
    }


def run_query(
    evidence: Evidence, query: Query, contract: Contract | None = None
) -> dict[str, Any]:
    contract = contract or load_contract()
    metric = contract.metric(query.metric)
    query.validate(metric)
    evidence.validate()
    phases = PHASES if query.season_type == "Both" else (query.season_type,)
    seasons = query.seasons or [query.season]
    scopes = [(season, phase) for season in seasons for phase in phases]
    if any(
        s not in evidence.covered_scopes or s not in evidence.data_through
        for s in scopes
    ):
        raise SemanticError("unsupported_coverage", "Selected scope is not covered")
    scoped = [
        r
        for r in evidence.rows
        if r["season"] in seasons and r["season_type"] in phases
    ]
    anchor = (
        _date(query.as_of)
        if query.as_of
        else max((_date(r["game_date"]) for r in scoped), default=None)
    )
    if anchor is None:
        raise SemanticError("unavailable", "No game date can anchor this scope")
    start, end = _window(query, anchor)
    selected = [
        r
        for r in scoped
        if _date(r["game_date"]) <= end
        and (start is None or _date(r["game_date"]) >= start)
        and (query.team_abbr is None or r.get("team_abbr") == query.team_abbr)
        and (
            query.opponent_abbr is None or r.get("opponent_abbr") == query.opponent_abbr
        )
    ]
    if query.operation == "game_log":
        rows = sorted(
            (r for r in selected if r["player_id"] == query.player_id),
            key=lambda r: (str(r["game_date"]), r["game_id"]),
            reverse=True,
        )
        if query.window in ("last_n_games", "prior_n_games"):
            assert query.n is not None
            offset = query.n if query.window == "prior_n_games" else 0
            rows = rows[offset : offset + query.n]
        observed = len(rows)
        warnings = []
        if (
            query.window in ("last_n_games", "prior_n_games")
            and query.n is not None
            and observed < query.n
        ):
            warnings.append("partial_window")
        if observed > query.limit:
            warnings.append("display_limit_applied_latest_games")
        through = {
            (
                phase if len(seasons) == 1 else f"{season} {phase}"
            ): evidence.data_through[(season, phase)]
            for season, phase in scopes
        }
        if any(anchor > _date(d) for d in through.values()):
            warnings.append("requested_as_of_exceeds_source_coverage")
        games = []
        for row in reversed(rows[: query.limit]):
            value = aggregate([row], metric, query.aggregation)
            games.append(
                {
                    **{
                        k: row.get(k)
                        for k in (
                            "player_id",
                            "game_id",
                            "game_date",
                            "season_type",
                            "team_abbr",
                            "opponent_abbr",
                        )
                    },
                    **value,
                    "status": "ok" if value["value"] is not None else "unavailable",
                }
            )
        return {
            "status": "ok" if games else "no_observations",
            "contract_version": contract.version,
            "metric": metric.public(),
            "scope": {
                **asdict(query),
                "as_of": anchor.isoformat(),
                "window_start": start.isoformat() if start else None,
                "window_end": end.isoformat(),
            },
            "rows": games,
            "observed_games": observed,
            "displayed_games": len(games),
            "warnings": warnings,
            "provenance": {
                "source": evidence.source,
                "snapshot_id": evidence.snapshot_id,
                "data_through": through,
                "model_built_at": evidence.model_built_at,
                "temporal_semantics": "retrospective_source_snapshot",
            },
        }
    grouped: dict[int, list[Mapping[str, Any]]] = {}
    for row in selected:
        grouped.setdefault(int(row["player_id"]), []).append(row)
    summaries = []
    minimum = query.min_games if query.min_games is not None else contract.min_games
    for player_id, rows in grouped.items():
        rows.sort(key=lambda r: (str(r["game_date"]), r["game_id"]), reverse=True)
        if query.window in ("last_n_games", "prior_n_games"):
            assert query.n is not None
            offset = query.n if query.window == "prior_n_games" else 0
            rows = rows[offset : offset + query.n]
        summary = aggregate(rows, metric, query.aggregation)
        reasons = []
        if summary["valid_games"] < minimum:
            reasons.append("insufficient_games")
        if summary["missing_component_games"]:
            reasons.append("incomplete_components")
        if summary["value"] is None:
            reasons.append("unavailable_metric")
        if metric.attempt_field and query.min_attempts is not None:
            if summary["components"].get(metric.attempt_field, 0) < query.min_attempts:
                reasons.append("insufficient_attempts")
        summaries.append(
            {
                "player_id": player_id,
                **summary,
                "rank": None,
                "percentile": None,
                "eligible": not reasons,
                "status": "unavailable"
                if summary["value"] is None
                else "partial"
                if summary["missing_component_games"]
                else "ok",
                "exclusion_reasons": reasons,
                "sample_warning": "partial_window"
                if query.n and query.window.endswith("games") and len(rows) < query.n
                else None,
            }
        )
    cohort = [s for s in summaries if s["eligible"]]
    direction = query.direction or metric.direction
    for summary in cohort:
        value = summary["value"]
        better = sum(
            (s["value"] > value if direction == "higher" else s["value"] < value)
            for s in cohort
        )
        worse = sum(
            (s["value"] < value if direction == "higher" else s["value"] > value)
            for s in cohort
        )
        summary["rank"] = better + 1
        summary["percentile"] = (
            100 * worse / (len(cohort) - 1) if len(cohort) > 1 else None
        )
    output = summaries if query.operation == "summary" else cohort
    if query.operation == "percentile" and query.player_id is not None:
        output = summaries  # Explain why a specific player falls outside the cohort.
    if query.player_id is not None:
        output = [s for s in output if s["player_id"] == query.player_id]
    output.sort(
        key=lambda s: (s["rank"] if s["rank"] is not None else math.inf, s["player_id"])
    )
    output = [
        row
        for row in output
        if row["player_id"] not in (query.excluded_player_ids or [])
    ]
    warnings = []
    through = {
        (phase if len(seasons) == 1 else f"{season} {phase}"): evidence.data_through[
            (season, phase)
        ]
        for season, phase in scopes
    }
    if any(anchor > _date(d) for d in through.values()):
        warnings.append("requested_as_of_exceeds_source_coverage")
    return {
        "status": "ok"
        if output
        else "no_observations"
        if query.operation == "summary"
        else "empty_cohort",
        "contract_version": contract.version,
        "metric": metric.public(),
        "scope": {
            **asdict(query),
            "as_of": anchor.isoformat(),
            "window_start": start.isoformat() if start else None,
            "window_end": end.isoformat(),
        },
        "cohort": {
            "size": len(cohort),
            "min_games": minimum,
            "min_attempts": query.min_attempts,
            "attempt_field": metric.attempt_field,
            "direction": direction,
            "excluded_players": len(summaries) - len(cohort),
        },
        "rows": output[: query.limit],
        "warnings": warnings,
        "provenance": {
            "source": evidence.source,
            "snapshot_id": evidence.snapshot_id,
            "data_through": through,
            "model_built_at": evidence.model_built_at,
            "temporal_semantics": "retrospective_source_snapshot",
        },
    }


def resolve_entity(name: str, players: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Exact normalized aliases resolve identity independently of qualification."""

    def normalize(value: Any) -> str:
        return " ".join(str(value).casefold().split())

    key = normalize(name)
    if not key:
        raise SemanticError("invalid_scope", "Player name is required")
    matches = {
        p["player_id"]: p
        for p in players
        if key
        in {normalize(p["player_name"]), *(normalize(a) for a in p.get("aliases", []))}
    }
    return {
        "status": "ok"
        if len(matches) == 1
        else "ambiguous"
        if matches
        else "not_found",
        "matches": [matches[k] for k in sorted(matches)],
    }


def pregame_evidence(
    reports: Sequence[Mapping[str, Any]],
    *,
    season: str,
    player_id: int,
    game_date: str,
    matchup: str,
    tipoff: str | None,
) -> dict[str, Any]:
    validate_season(season)
    _date(game_date)
    if type(player_id) is not int or player_id < 1 or not matchup:
        raise SemanticError(
            "invalid_scope", "A resolved player and matchup are required"
        )

    def timestamp(value: Any) -> datetime:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise SemanticError(
                "invalid_evidence", "Invalid report/tipoff timestamp"
            ) from exc
        if parsed.tzinfo is None:
            raise SemanticError("invalid_evidence", "Timestamp requires timezone")
        return parsed

    if tipoff is None:
        return {"status": "unverified", "reason": "tipoff_unavailable", "report": None}
    cutoff = timestamp(tipoff)
    candidates = [
        r
        for r in reports
        if r.get("season") == season
        and r.get("player_id") == player_id
        and r.get("game_date") == game_date
        and r.get("matchup") == matchup
        and timestamp(r.get("report_timestamp_utc")) < cutoff
    ]
    if not candidates:
        return {
            "status": "unknown",
            "reason": "no_matching_pregame_report",
            "report": None,
        }
    latest = max(timestamp(r["report_timestamp_utc"]) for r in candidates)
    tied = [r for r in candidates if timestamp(r["report_timestamp_utc"]) == latest]
    if len({(r.get("injury_status"), r.get("reason")) for r in tied}) > 1:
        raise SemanticError(
            "ambiguous_evidence", "Conflicting reports at the same time"
        )
    if not tied[0].get("source_url"):
        raise SemanticError("invalid_evidence", "Report source URL is required")
    return {
        "status": "ok",
        "report": dict(tied[0]),
        "coverage_warning": "sampled_reports_do_not_capture_every_intraday_update",
    }


def compare_queries(
    evidence: Evidence, current: Query, baseline: Query
) -> dict[str, Any]:
    """Compare explicit individual summaries and disclose both sample memberships."""
    if (
        current.operation != "summary"
        or baseline.operation != "summary"
        or current.player_id is None
        or baseline.player_id is None
        or current.metric != baseline.metric
        or current.aggregation != baseline.aggregation
    ):
        raise SemanticError(
            "invalid_scope", "Comparison requires compatible individual summaries"
        )
    current_result = run_query(evidence, current)
    baseline_result = run_query(evidence, baseline)
    current_row: dict[str, Any] = next(iter(current_result["rows"]), {})
    baseline_row: dict[str, Any] = next(iter(baseline_result["rows"]), {})
    a, b = current_row.get("value"), baseline_row.get("value")
    ratio = current_result["metric"]["unit"] == "ratio"
    difference = (
        (a - b) * (100 if ratio else 1) if a is not None and b is not None else None
    )
    return {
        "status": "ok" if difference is not None else "unavailable",
        "current": current_result,
        "baseline": baseline_result,
        "difference": difference,
        "difference_unit": "percentage_points"
        if ratio
        else current_result["metric"]["unit"],
        "relative_change_pct": (a - b) / b * 100
        if a is not None and b not in (None, 0)
        else None,
        "overlapping_game_ids": sorted(
            set(current_row.get("game_ids", [])) & set(baseline_row.get("game_ids", []))
        )
        if current.season == baseline.season and current.player_id == baseline.player_id
        else [],
    }
