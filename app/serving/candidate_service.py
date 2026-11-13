"""Trusted PTS-only application candidate; never a live serving authority.

Composition supplies an independently maintained, read-only binding checker.
Neither HTTP requests nor snapshot contents can configure that checker.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from app.agent.intelligence import plan_question
from app.agent.snapshot_source import CurrentPTSAskShadow, HistoricalPTSAskShadow
from app.config import Settings
from app.serving.comparison import (
    CurrentPTSComparisonShadow,
    HistoricalPTSComparisonShadow,
    PTSComparisonRequest,
)
from app.serving.composite import TwoSeasonPTS
from app.serving.context import PTSShadowRequest
from app.serving.contract import ServingError, canonical_json, content_digest
from app.serving.current import CurrentReadContext
from app.serving.player import CurrentPlayerPTSShadow, PlayerPTSShadow
from app.serving.shadow_host import create_current_shadow_app, create_shadow_app


@dataclass(frozen=True)
class CandidateBinding:
    """An external offline rehearsal pin, not an issued read lease."""

    journal_id: str
    generation: int
    record_id: str
    candidate_id: str
    code_id: str
    catalog_id: str
    status_id: str
    status_revision: int
    snapshot_ids: tuple[str, ...]
    context_ids: tuple[str, ...]
    selection_id: str | None
    expires_at: datetime

    @classmethod
    def from_mapping(cls, value):
        fields = {
            "journal_id",
            "generation",
            "record_id",
            "candidate_id",
            "code_id",
            "catalog_id",
            "status_id",
            "status_revision",
            "snapshot_ids",
            "context_ids",
            "selection_id",
            "expires_at",
        }
        if type(value) is not dict or set(value) != fields:
            raise ServingError("invalid_candidate_binding")
        if (
            type(value["snapshot_ids"]) is not list
            or type(value["context_ids"]) is not list
            or type(value["expires_at"]) is not str
        ):
            raise ServingError("invalid_candidate_binding")
        try:
            return cls(
                **{
                    k: v
                    for k, v in value.items()
                    if k not in {"snapshot_ids", "context_ids", "expires_at"}
                },
                snapshot_ids=tuple(value["snapshot_ids"]),
                context_ids=tuple(value["context_ids"]),
                expires_at=datetime.fromisoformat(value["expires_at"]),
            )
        except (TypeError, ValueError, OverflowError):
            raise ServingError("invalid_candidate_binding") from None

    def __post_init__(self):
        if type(self.snapshot_ids) is not tuple or type(self.context_ids) is not tuple:
            raise ServingError("invalid_candidate_binding")
        ids = (
            self.journal_id,
            self.record_id,
            self.candidate_id,
            self.code_id,
            self.catalog_id,
            self.status_id,
            *self.snapshot_ids,
            *self.context_ids,
        )
        if (
            type(self.generation) is not int
            or self.generation <= 0
            or type(self.status_revision) is not int
            or self.status_revision < 0
            or len(self.snapshot_ids) not in (1, 2)
            or len(self.context_ids) != len(self.snapshot_ids)
            or (len(self.snapshot_ids) == 1 and self.selection_id is not None)
            or (
                len(self.snapshot_ids) == 2
                and (
                    type(self.selection_id) is not str
                    or re.fullmatch(r"[0-9a-f]{64}", self.selection_id) is None
                )
            )
            or any(
                type(v) is not str or re.fullmatch(r"[0-9a-f]{64}", v) is None
                for v in ids
            )
            or not isinstance(self.expires_at, datetime)
            or self.expires_at.tzinfo is None
            or self.expires_at.utcoffset() is None
        ):
            raise ServingError("invalid_candidate_binding")


class CandidatePTSService:
    """One process-bound candidate. Caller owns trust in the binding checker."""

    def __init__(
        self,
        selection,
        *,
        binding: CandidateBinding,
        check_binding: Callable[[], CandidateBinding],
        players: tuple[tuple[int, str], ...],
        evidence_kind: str,
    ):
        if type(binding) is not CandidateBinding or not callable(check_binding):
            raise ServingError("explicit_selection_required")
        self._selection: CurrentReadContext | TwoSeasonPTS
        if type(selection) is CurrentReadContext:
            create_current_shadow_app(selection, evidence_kind=evidence_kind)
            self._selection = selection
            self._contexts = (selection,)
        elif type(selection) is TwoSeasonPTS:
            # Reopen through the same guarded constructor used by the shadow host.
            create_shadow_app(selection, evidence_kind=evidence_kind)
            historical, current = selection._contexts
            reopened = TwoSeasonPTS.open(
                historical._handle._root,
                historical._handle._policy,
                current._handle._root,
                current._handle._policy,
                historical_snapshot_id=historical.binding["snapshot_id"],
                current_snapshot_id=current.binding["snapshot_id"],
                authority=selection._authority,
            )
            if reopened.binding != selection.binding:
                raise ServingError("snapshot_binding_mismatch")
            self._selection = reopened
            self._contexts = reopened._contexts
        else:
            raise ServingError("explicit_selection_required")
        if (
            type(players) is not tuple
            or not 0 < len(players) <= 10000
            or any(
                type(p) is not tuple
                or len(p) != 2
                or type(p[0]) is not int
                or not 0 < p[0] <= 2147483647
                or type(p[1]) is not str
                or not p[1].strip()
                or len(p[1]) > 80
                for p in players
            )
            or len({p[0] for p in players}) != len(players)
            or len({p[1].casefold().strip() for p in players}) != len(players)
        ):
            raise ServingError("invalid_candidate_catalog")
        if (
            type(self._selection) is TwoSeasonPTS
            and not {p[0] for p in players}
            <= self._selection._authority.approved_player_ids
        ):
            raise ServingError("identity_compatibility_denied")
        if content_digest(players) != binding.catalog_id:
            raise ServingError("identity_compatibility_denied")
        if (
            tuple(c.binding["snapshot_id"] for c in self._contexts)
            != binding.snapshot_ids
            or tuple(c.binding["context_id"] for c in self._contexts)
            != binding.context_ids
            or (
                self._selection.binding["selection_id"]
                if type(self._selection) is TwoSeasonPTS
                else None
            )
            != binding.selection_id
        ):
            raise ServingError("snapshot_binding_mismatch")
        self._binding = binding
        self._check_binding = check_binding
        self._players = dict(players)
        self._context_bytes = tuple(canonical_json(c.binding) for c in self._contexts)
        self._evidence_kind = evidence_kind
        self.check()

    @property
    def evidence_kind(self):
        return self._evidence_kind

    @property
    def binding_expires_at(self):
        return self._binding.expires_at.isoformat()

    def check(self, seasons=()):
        try:
            actual = self._check_binding()
        except Exception as exc:
            raise ServingError("candidate_authority_changed") from exc
        if (
            self._binding.expires_at.astimezone(timezone.utc)
            <= datetime.now(timezone.utc)
            or type(actual) is not CandidateBinding
            or actual != self._binding
            or tuple(canonical_json(c.binding) for c in self._contexts)
            != self._context_bytes
            or (
                self._selection.binding["selection_id"]
                if type(self._selection) is TwoSeasonPTS
                else None
            )
            != self._binding.selection_id
        ):
            raise ServingError("candidate_authority_changed")
        for season in seasons:
            self._context(season).authorize()
        try:
            after = self._check_binding()
        except Exception as exc:
            raise ServingError("candidate_authority_changed") from exc
        if (
            type(after) is not CandidateBinding
            or after != self._binding
            or (
                self._selection.binding["selection_id"]
                if type(self._selection) is TwoSeasonPTS
                else None
            )
            != self._binding.selection_id
            or self._binding.expires_at.astimezone(timezone.utc)
            <= datetime.now(timezone.utc)
        ):
            raise ServingError("candidate_authority_changed")

    def check_payload(self, payload):
        """Revalidate both the trusted binding and the exact recorded source status."""
        members = payload.get("candidate_evidence")
        if members is None:
            members = [payload]
        if type(members) is not list or len(members) not in (1, 2):
            raise ServingError("invalid_candidate_evidence")
        seasons = tuple(member["binding"]["scope"]["season"] for member in members)
        self.check(seasons)
        for member, season in zip(members, seasons):
            self._context(season).check_emission(member)
        self.check(seasons)

    def _context(self, season):
        for context in self._contexts:
            if context.binding["scope"]["season"] == season:
                return context
        raise ServingError("season_not_available")

    def _single(self, season, player_ids, aggregation, consumer):
        self.check((season,))
        if any(p not in self._players for p in player_ids):
            raise ServingError("identity_not_approved")
        context = self._context(season)
        before = context.authorize()
        scope = context.binding["scope"]
        if len(player_ids) == 2:
            cls = (
                CurrentPTSComparisonShadow
                if type(context) is CurrentReadContext
                else HistoricalPTSComparisonShadow
            )
            payload = cls(context).answer(
                PTSComparisonRequest.create(player_ids, aggregation, scope=scope)
            )
        else:
            request = PTSShadowRequest.create(player_ids[0], aggregation, scope=scope)
            if consumer == "player":
                cls = (
                    CurrentPlayerPTSShadow
                    if type(context) is CurrentReadContext
                    else PlayerPTSShadow
                )
                payload = cls(context).get_pts(request)
            else:
                cls = (
                    CurrentPTSAskShadow
                    if type(context) is CurrentReadContext
                    else HistoricalPTSAskShadow
                )
                payload = cls(context).answer(request)
        TwoSeasonPTS._same_status([before], [payload["authority"]])
        context.check_emission(payload)
        self.check((season,))
        return payload

    def player(self, player_id, season, aggregation="average"):
        return self._single(season, (player_id,), aggregation, "player")

    def compare(self, player_ids, season, aggregation="average"):
        if len(player_ids) != 2 or player_ids[0] == player_ids[1]:
            raise ServingError("invalid_request")
        return self._single(season, tuple(player_ids), aggregation, "comparison")

    def ask(self, question: str, settings: Settings):
        self.check()
        if settings.ask_intelligence_provider != "reference":
            raise ServingError("unsupported_candidate_provider")
        players = [
            {"player_id": key, "player_name": value}
            for key, value in self._players.items()
        ]
        plan = plan_question(
            None,
            settings=settings,
            model="reference",
            question=question,
            selected_season="2026-27",
            players=players,
            teams=(),
            deterministic=True,
        )
        if (
            plan.get("status") not in {"query", "compare"}
            or plan.get("model_calls") != 0
        ):
            raise ServingError("unsupported_candidate_question")
        queries = plan.get("queries")
        if type(queries) is not list or len(queries) not in (1, 2):
            raise ServingError("unsupported_candidate_question")
        lookup = {
            name.casefold().strip(): player_id
            for player_id, name in self._players.items()
        }
        ids, seasons = [], []
        for q in queries:
            if (
                q.get("metric") != "pts"
                or q.get("operation") != "summary"
                or q.get("aggregation") not in ("total", "average")
                or q.get("season_type") != "Regular Season"
                or q.get("window") != "season_to_date"
                or any(
                    q.get(k) is not None
                    for k in (
                        "min_games",
                        "min_attempts",
                        "n",
                        "start_date",
                        "as_of",
                        "team_abbr",
                        "opponent_abbr",
                        "home_away",
                        "game_result",
                        "weekdays",
                        "stat_filters",
                    )
                )
                or type(q.get("player_name")) is not str
            ):
                raise ServingError("unsupported_candidate_question")
            name = q["player_name"].casefold().strip()
            if name not in lookup:
                raise ServingError("identity_not_approved")
            ids.append(lookup[name])
            seasons.append(q["season"])
        if len(set(ids)) != 1 or len({q["aggregation"] for q in queries}) != 1:
            raise ServingError("unsupported_candidate_question")
        if len(queries) == 2:
            if type(self._selection) is not TwoSeasonPTS or set(seasons) != {
                "2025-26",
                "2026-27",
            }:
                raise ServingError("season_not_available")
            self.check(tuple(seasons))
            result = self._selection.answer(
                (ids[0],), queries[0]["aggregation"], consumer="ask"
            )
            members = result["members"]
        else:
            members = [
                self._single(seasons[0], (ids[0],), queries[0]["aggregation"], "ask")
            ]
        self.check(tuple(seasons))
        return {
            "status": "answered",
            "answer": "Observed points are shown below for the stated regular-season scope. This is an offline candidate; rankings and league completeness are not established.",
            "tables": [
                copy.deepcopy(t) for member in members for t in member["tables"]
            ],
            "candidate_evidence": copy.deepcopy(members),
            "scope_assumptions": [
                f"{m['binding']['scope']['season']} regular season: {m['binding']['scope']['start_date']} through {m['binding']['scope']['end_date']}"
                for m in members
            ],
            "runtime_eligible": False,
            "offline_candidate": True,
            "evidence_kind": self.evidence_kind,
            "binding_expires_at": self.binding_expires_at,
        }
