"""Request-scoped evidence from the authorized production publication."""

from dataclasses import dataclass
from datetime import date
from typing import Any

from app.agent.semantics import Evidence, SemanticError


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
