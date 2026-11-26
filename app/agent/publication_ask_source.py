"""Request-scoped evidence from the authorized production publication."""

import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from app.agent.semantics import Evidence, SemanticError, load_contract

_STAT = r"total|per game|ratio|per 36"
_DATES = r"(?: from (\d{4}-\d{2}-\d{2}) to (\d{4}-\d{2}-\d{2}))?"
_COMPARE = re.compile(rf"compare (.+?) vs (.+?) ({_STAT}) (.+?){_DATES}", re.I)
_SINGLE = re.compile(rf"(.+?) ({_STAT}|game log) (.+?){_DATES}", re.I)
_ALIASES = {
    "points": "pts",
    "rebounds": "reb",
    "assists": "ast",
    "minutes": "min",
    "fg%": "fg_pct",
    "3p%": "fg3_pct",
    "ft%": "ft_pct",
}
_AGGREGATIONS = {
    "total": "total",
    "per game": "per_game",
    "ratio": "ratio",
    "per 36": "per36",
    "game log": "total",
}


@dataclass(frozen=True)
class PublicationStatRequest:
    names: tuple[str, ...]
    metric: str
    aggregation: str
    operation: str
    first: str | None
    last: str | None


def parse_publication_stat(question: str) -> PublicationStatRequest | None:
    """Share the exact, self-contained stat grammar across publication surfaces."""
    wording = question.strip()
    if match := re.fullmatch(r"how many points did (.+?) score\??", wording, re.I):
        wording = f"{match[1]} total points"
    elif match := re.fullmatch(
        r"(.+?) average (points|rebounds|assists)\??", wording, re.I
    ):
        wording = f"{match[1]} per game {match[2]}"

    names: tuple[str, ...]
    if match := _COMPARE.fullmatch(wording):
        a, b, mode, key, first, last = match.groups()
        names = (a, b)
    elif match := _SINGLE.fullmatch(wording):
        name, mode, key, first, last = match.groups()
        names = (name,)
    else:
        return None

    mode, key = mode.casefold(), key.casefold()
    metric = _ALIASES.get(key, key)
    if mode == "per 36" and metric in {"pts", "ast"}:
        metric += "_per36"
    return PublicationStatRequest(
        names,
        metric,
        _AGGREGATIONS[mode],
        "game_log" if mode == "game log" else "summary",
        first,
        last,
    )


@dataclass(frozen=True)
class PublicationAskSource:
    evidence: Evidence
    status: dict[str, Any]

    @property
    def season(self) -> str:
        return self.status["scope"]["season"]

    @property
    def phase(self) -> str:
        return self.status["scope"]["phase"]

    @property
    def first(self) -> date:
        return date.fromisoformat(self.status["scope"]["first"])

    @property
    def last(self) -> date:
        return date.fromisoformat(self.status["scope"]["last"])

    @property
    def publication_id(self) -> str:
        return self.status["publication_id"]

    @property
    def provenance(self) -> dict[str, Any]:
        return {
            "publication_id": self.publication_id,
            "season": self.season,
            "phase": self.phase,
            "first": str(self.first),
            "last": str(self.last),
            "freshness": self.status["freshness"],
            "cohort_complete": self.evidence.cohort_complete,
            **(
                {"cohort_proof": self.status["cohort_proof"]}
                if "cohort_proof" in self.status
                else {}
            ),
        }

    def load(self, seasons: list[str]):
        if set(seasons) != {self.season}:
            raise SemanticError(
                "unsupported_coverage",
                "This publication covers one season. Start a separate analysis for historical seasons.",
            )
        return {
            "capture": self.provenance,
            "review_metadata": self.provenance,
            "coverage_notes": [
                f"{self.season} {self.phase.lower()} evidence from {self.first} through {self.last} · Publication {self.publication_id}."
            ],
        }, self.evidence

    def stat_plan(self, request: PublicationStatRequest, selected_player=None):
        aggregation = {"per_game": "average", "per36": "ratio"}.get(
            request.aggregation, request.aggregation
        )
        metric = load_contract().metric(request.metric)
        if request.operation == "game_log" and metric.denominator:
            aggregation = "ratio"
        comparison = len(request.names) == 2
        names = (
            (selected_player,) if selected_player and not comparison else request.names
        )
        return {
            "status": "compare" if comparison else "query",
            "message": "Scoped publication statistics",
            "model_calls": 0,
            "queries": [
                {
                    "player_name": name,
                    "metric": metric.key,
                    "aggregation": aggregation,
                    "operation": request.operation,
                    "season": self.season,
                    "season_type": self.phase,
                    **(
                        {
                            "window": "date_range",
                            "start_date": request.first,
                            "as_of": request.last,
                        }
                        if request.first
                        else {}
                    ),
                }
                for name in names
            ],
        }

    def check_window(self, first: str | date | None, last: str | date | None) -> None:
        if (first and str(first) < str(self.first)) or (
            last and str(last) > str(self.last)
        ):
            raise SemanticError(
                "unsupported_coverage",
                f"The publication verifies games from {self.first} through {self.last}. Choose dates within that coverage.",
            )

    def check_phase(self, phases: list[str]) -> None:
        if set(phases) != {self.phase}:
            raise SemanticError(
                "unsupported_coverage",
                f"The current publication covers only the {self.phase.lower()}.",
            )

    def check_plan(self, plan: dict[str, Any]) -> None:
        for query in plan.get("queries", []):
            self.load(query.get("seasons") or [query["season"]])
            self.check_phase([query.get("season_type") or "Regular Season"])
            self.check_window(query.get("start_date"), query.get("as_of"))
            if (
                query.get("operation") in {"rank", "percentile"}
                and not self.evidence.cohort_complete
            ):
                raise SemanticError(
                    "unsupported_coverage",
                    "This publication can verify player box scores. The complete league cohort required for this ranking or percentile is unavailable.",
                )


class ProductionAskWarehouse:
    """Keep the ordinary historical warehouse alongside the publication reader."""

    def __init__(self, historical, read_publication):
        self.historical = historical
        self.read_publication = read_publication

    def __getattr__(self, key):
        return getattr(self.historical, key)

    def open_publication(self, publication_id=None):
        try:
            source = self.read_publication(publication_id)
        except (ValueError, OSError, KeyError) as exc:
            raise SemanticError(
                "publication_unavailable",
                "The authorized publication is unavailable. Try again when its verification is restored.",
            ) from exc
        if publication_id and (
            source is None or source.publication_id != publication_id
        ):
            raise SemanticError(
                "publication_unavailable",
                "This conversation's publication is unavailable. Start a new analysis for the active publication.",
            )
        return source

    def recheck_publication(self, source):
        current = self.open_publication(source.publication_id)
        if source.evidence.cohort_complete and not current.evidence.cohort_complete:
            raise SemanticError(
                "publication_unavailable",
                "The publication's league coverage could not be reverified. Try again.",
            )
