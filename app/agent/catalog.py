from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app.agent.formulas import extract_formula_variables

CATALOG_PATH = Path(__file__).with_name("semantic_catalog.yml")

# Metrics are tiered in the catalog (tier: 1..4). A vague "stats" question
# defaults to everything through this tier — tier 1 (pts/reb/ast headline) plus
# tier 2 (stl/blk/tov two-way) — i.e. the traditional box score. Tiers 3-4
# (shooting/availability context, derived composites) are only pulled in when a
# metric is named explicitly. DEFAULT_METRIC_KEYS mirrors tiers 1-2 in catalog
# order; SemanticCatalog.default_metric_keys() is the data-driven source of
# truth, and a test guards that the two stay in sync.
DEFAULT_TIER = 2
DEFAULT_METRIC_KEYS = ("pts", "reb", "ast", "stl", "blk", "tov")

# Generic catch-all wording that maps to "give me the standard box-score set"
# rather than a single named metric. A clarification reply like "look at all
# individual stats" lands here, so the user gets the default cohort instead of
# an "Unsupported metric" error for a word that was never a metric to begin with.
_GENERIC_METRIC_TOKENS = frozenset(
    {
        "all",
        "all_stats",
        "all_individual_stats",
        "individual_stats",
        "all_metrics",
        "all_of_them",
        "everything",
        "every_stat",
        "stat",
        "stats",
        "splits",
        "box_score",
        "boxscore",
        "full",
        "complete",
        "overall",
    }
)


def _normalize_key(value: str) -> str:
    return value.strip().lower().replace("-", "_").replace(" ", "_")


@dataclass(frozen=True)
class MetricDefinition:
    key: str
    label: str
    description: str
    aliases: tuple[str, ...]
    game_log_key: str
    trend_stat: str
    detail_average_key: str | None
    baseline_key: str | None
    percentile_key: str | None
    leaderboard_column: str
    direction: str
    formula: str | None
    tier: int

    @property
    def higher_is_better(self) -> bool:
        return self.direction != "lower"

    @property
    def is_derived(self) -> bool:
        return self.formula is not None

    @property
    def formula_variables(self) -> tuple[str, ...]:
        if self.formula is None:
            return ()
        return tuple(sorted(extract_formula_variables(self.formula)))

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "description": self.description,
            "aliases": list(self.aliases),
            "higher_is_better": self.higher_is_better,
            "direction": self.direction,
            "formula": self.formula,
            "tier": self.tier,
        }


class SemanticCatalog:
    def __init__(self, metrics: dict[str, MetricDefinition]) -> None:
        self.metrics = metrics
        self._aliases: dict[str, str] = {}
        for key, metric in metrics.items():
            self._aliases[_normalize_key(key)] = key
            self._aliases[_normalize_key(metric.label)] = key
            for alias in metric.aliases:
                self._aliases[_normalize_key(alias)] = key

    def list_metrics(self) -> list[dict[str, Any]]:
        return [metric.to_public_dict() for metric in self.metrics.values()]

    def tier_keys(self, tier: int) -> tuple[str, ...]:
        """Metric keys belonging to exactly ``tier``, in catalog order."""
        return tuple(key for key, metric in self.metrics.items() if metric.tier == tier)

    def default_metric_keys(self, max_tier: int = DEFAULT_TIER) -> tuple[str, ...]:
        """Keys for the default cohort: every metric through ``max_tier``.

        This is the data-driven definition of "the stats people mean by
        default" — headline (tier 1) plus two-way (tier 2) — used whenever a
        question names no concrete metric.
        """
        return tuple(
            key for key, metric in self.metrics.items() if metric.tier <= max_tier
        )

    def resolve_metric(self, value: str) -> MetricDefinition | None:
        key = self._aliases.get(_normalize_key(value))
        if key is None:
            return None
        return self.metrics[key]

    def resolve_metrics(
        self,
        values: list[str] | None,
        *,
        default_keys: tuple[str, ...] = DEFAULT_METRIC_KEYS,
    ) -> tuple[list[MetricDefinition], list[str]]:
        """Resolve a list of requested metrics, degrading gracefully.

        Unlike :meth:`resolve_metric`, this never returns an empty selection:
        unknown tokens are reported in ``invalid`` but dropped, generic wording
        ("all stats", "individual stats") expands to ``default_keys``, and a
        request where nothing resolved falls back to ``default_keys`` too. This
        keeps multi-metric tools (game log, trends, percentiles) answerable
        instead of hard-failing when a planner emits a vague metric word.
        """
        if not values:
            return [self.metrics[key] for key in default_keys], []

        resolved: list[MetricDefinition] = []
        invalid: list[str] = []
        for value in values:
            normalized = _normalize_key(str(value))
            if not normalized or normalized in _GENERIC_METRIC_TOKENS:
                # Generic catch-all wording is satisfied by the default cohort.
                continue
            metric = self.resolve_metric(str(value))
            if metric is None:
                invalid.append(str(value))
                continue
            if metric.key not in {item.key for item in resolved}:
                resolved.append(metric)
        if not resolved:
            resolved = [self.metrics[key] for key in default_keys]
        return resolved, invalid


@lru_cache(maxsize=1)
def load_semantic_catalog(path: str | Path = CATALOG_PATH) -> SemanticCatalog:
    with Path(path).open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    metrics: dict[str, MetricDefinition] = {}
    for key, config in (raw.get("metrics") or {}).items():
        formula = config.get("formula") or None
        if formula is not None:
            extract_formula_variables(str(formula))
        metrics[str(key)] = MetricDefinition(
            key=str(key),
            label=str(config["label"]),
            description=str(config["description"]),
            aliases=tuple(str(item) for item in config.get("aliases", [])),
            game_log_key=str(config.get("game_log_key") or key),
            trend_stat=str(config["trend_stat"]),
            detail_average_key=config.get("detail_average_key") or None,
            baseline_key=config.get("baseline_key") or None,
            percentile_key=config.get("percentile_key") or None,
            leaderboard_column=str(config.get("leaderboard_column") or ""),
            direction=str(config["direction"]),
            formula=str(formula) if formula is not None else None,
            # Untiered metrics fall to the lowest priority so they never leak
            # into the default cohort by accident.
            tier=int(config.get("tier") or 99),
        )
    return SemanticCatalog(metrics)
