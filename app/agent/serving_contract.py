"""Offline aggregate contract primitives; not connected to Ask routing.

Summaries are metric-specific complete-case sufficient statistics. They are
never disguised as game-grain Evidence. Publication membership validation is a
separate prerequisite for serving any cohort.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from app.agent.formulas import evaluate_formula
from app.agent.semantics import Metric, SemanticError, _number, load_contract


@dataclass(frozen=True)
class MetricSummary:
    publication_id: str
    season: str
    phase: str
    player_id: int
    metric: str
    game_ids: tuple[str, ...]
    valid_game_ids: tuple[str, ...]
    components: Mapping[str, float]

    def validate(self, metric: Metric) -> None:
        from app.agent.semantics import PHASES
        from app.seasons import validate_season

        validate_season(self.season)
        if (
            not self.publication_id
            or self.phase not in PHASES
            or type(self.player_id) is not int
            or self.player_id < 1
            or self.metric != metric.key
            or set(self.components) != metric.components
        ):
            raise SemanticError(
                "invalid_evidence", "Invalid summary identity or metric"
            )
        for ids in (self.game_ids, self.valid_game_ids):
            if len(set(ids)) != len(ids) or any(
                not isinstance(g, str) or not g for g in ids
            ):
                raise SemanticError("invalid_evidence", "Invalid appearance membership")
        if not set(self.valid_game_ids) <= set(self.game_ids):
            raise SemanticError("invalid_evidence", "Valid games outside observed set")
        for key, value in self.components.items():
            if type(value) not in (float, int) or not math.isfinite(value):
                raise SemanticError("invalid_evidence", "Nonfinite aggregate component")
            if value < 0 and key != "plus_minus":
                raise SemanticError("invalid_evidence", "Negative aggregate component")
            if not self.valid_game_ids and value != 0:
                raise SemanticError(
                    "invalid_evidence", "Components without valid games"
                )
        for made, attempted in (("fgm", "fga"), ("fg3m", "fg3a"), ("ftm", "fta")):
            if made in self.components and attempted in self.components:
                if self.components[made] > self.components[attempted]:
                    raise SemanticError("invalid_evidence", "Makes exceed attempts")


def summarize_rows(
    rows: Sequence[Mapping[str, Any]], metric: Metric, publication_id: str
) -> MetricSummary:
    """Build a candidate from already validated source rows, for offline parity.

    This function does not assert source completeness or certify publication.
    """
    if not rows:
        raise SemanticError("unsupported_coverage", "No appearance membership")
    identities = {(r["season"], r["season_type"], r["player_id"]) for r in rows}
    if len(identities) != 1:
        raise SemanticError("invalid_scope", "Summary requires one player/season/phase")
    season, phase, player = next(iter(identities))
    valid = [
        r for r in rows if all(_number(r.get(c)) is not None for c in metric.components)
    ]
    result = MetricSummary(
        publication_id,
        season,
        phase,
        player,
        metric.key,
        tuple(r["game_id"] for r in rows),
        tuple(r["game_id"] for r in valid),
        {c: math.fsum(float(r[c]) for r in valid) for c in metric.components},
    )
    result.validate(metric)
    return result


def aggregate_summaries(
    summaries: Sequence[MetricSummary], metric: Metric, aggregation: str
) -> dict[str, Any]:
    """Combine disjoint phases within one publication, player and season."""
    if aggregation not in metric.aggregations:
        raise SemanticError("unsupported_operation", "Unsupported aggregation")
    if not summaries:
        raise SemanticError("unsupported_coverage", "No summaries")
    for summary in summaries:
        summary.validate(metric)
    identities = {(s.publication_id, s.season, s.player_id) for s in summaries}
    phases = [s.phase for s in summaries]
    games = [g for s in summaries for g in s.game_ids]
    if (
        len(identities) != 1
        or len(set(phases)) != len(phases)
        or len(set(games)) != len(games)
    ):
        raise SemanticError(
            "invalid_evidence", "Mixed publication or overlapping scope"
        )
    valid = sum(len(s.valid_game_ids) for s in summaries)
    totals = {
        c: math.fsum(s.components[c] for s in summaries) for c in metric.components
    }
    numerator = (
        (valid if metric.key == "gp" else evaluate_formula(metric.numerator, totals))
        if valid
        else None
    )
    denominator = (
        evaluate_formula(metric.denominator, totals)
        if metric.denominator and valid
        else valid
        if aggregation == "average" and valid
        else None
    )
    value = numerator
    if aggregation in ("average", "ratio"):
        value = (
            numerator / denominator if numerator is not None and denominator else None
        )
    return dict(
        value=value,
        display_value=round(value * (100 if metric.unit == "ratio" else 1), 1)
        if value is not None
        else None,
        numerator=numerator,
        denominator=denominator,
        observed_games=len(games),
        valid_games=valid,
        missing_component_games=len(games) - valid,
        components=totals,
        game_ids=games,
    )


def metric_matrix() -> list[dict[str, Any]]:
    """Complete catalog inventory; only points is eligible for the future pilot."""
    contract = load_contract()
    return [
        dict(
            **metric.public(),
            contract_version=contract.version,
            valid_set="required components present on the same appearance; nonfinite/non-numeric data rejected",
            null_policy="unavailable without valid games or with zero ratio denominator",
            phase_composition="disjoint membership; same publication/player/season",
            qualification_min_games=contract.min_games,
            pilot_eligible=metric.key == "pts",
            routing_enabled=False,
        )
        for metric in contract.metrics.values()
    ]
