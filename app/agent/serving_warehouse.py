"""Feature-gated serving selection with publication-scoped single-flight caches."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from threading import Lock
from time import monotonic

from app.agent.execution import (
    CURRENT,
    ExecutionFailure,
    acquire_timed_lock,
    optional_shadow_budget,
    record_data_read,
    record_serving_shadow,
    stage,
)
from app.agent.semantic_serving import players_from_rows
from app.agent.semantics import PHASES, SemanticError
from app.agent.serving_runtime import ServingShadow, scoped_source


class CapabilityMiss(Exception):
    pass


class PublicationCache:
    def __init__(self, reader, clock):
        self.reader, self.clock = reader, clock
        self._locks = {False: Lock(), True: Lock()}
        self._values = {}

    def get(self, *, detail=False):
        lock = self._locks[detail]
        acquire_timed_lock(lock, "serving_cache_wait", 120)
        hit = False
        try:
            pub = self._values.get(detail)
            hit = pub is not None
            if pub is None:
                pub = self.reader.load(now=self.clock(), include_detail=detail)
                self._values[detail] = pub
            # Expiry/corruption is a visible failure, not an implicit refresh.
            instant = self.clock()
            if (
                instant.tzinfo is None
                or not pub.approval.built_at <= instant < pub.approval.expires_at
            ):
                raise SemanticError("invalid_evidence", "Serving publication expired")
            record_data_read(
                kind="evidence_cache",
                status="ok",
                rows=len(pub.summaries),
                cache_hit=hit,
                source_version=pub.approval.source_snapshot_id,
            )
            return pub
        except Exception:
            record_data_read(kind="evidence_cache", status="error", cache_hit=hit)
            raise
        finally:
            lock.release()


@dataclass
class PreparedAttempt:
    plan: dict
    result: dict | None
    players: list
    appearances: list
    source_query: dict = field(default_factory=dict)
    required_seasons: tuple[str, ...] = ()


class ServingWarehouse:
    def __init__(
        self,
        facts,
        reader,
        clock,
        *,
        mode="shadow",
        reuse_cohort=False,
        families=("points",),
    ):
        self.reuse_cohort = reuse_cohort
        self.families = frozenset(families)
        if not self.families <= {"points", "metrics", "rankings", "detail", "rich"}:
            raise ValueError("Unknown serving family")
        if mode not in ("shadow", "serve"):
            raise ValueError("Unsupported serving mode")
        self.facts, self.mode, self.clock = facts, mode, clock
        self.cache = PublicationCache(reader, clock)
        self._last_shadow: ServingShadow | FailedShadow | None = None

    @property
    def serving_shadow(self):
        policy = CURRENT.get()
        if policy is not None:
            return policy.serving_state.get(self, {}).get("shadow")
        return self._last_shadow

    @serving_shadow.setter
    def serving_shadow(self, value):
        policy = CURRENT.get()
        if policy is not None:
            policy.serving_state.setdefault(self, {})["shadow"] = value
        else:
            self._last_shadow = value

    def candidate_shadow(self, evidence=None):
        pub = self.cache.get()
        metadata, _ = pub.expanded_metadata(self.clock())
        return ServingShadow(
            pub,
            self.clock,
            expanded=metadata.get("capability_version") == "ask_serving_expansion/1",
            budget_seconds=15.0,
            reference_evidence=evidence,
            query_runner=lambda query: self._shadow_candidate(query, pub, metadata),
        )

    def _shadow_candidate(self, query, pub, metadata):
        try:
            return self._run_candidate(query, pub, metadata)
        except CapabilityMiss:
            return None

    def prepare(self, planner, *, seasons=None, normalizer=None, question=None):
        if self.mode != "serve" or not self.families & {
            "points",
            "metrics",
            "rankings",
            "detail",
        }:
            return None
        if not question_may_use_serving(question, self.families):
            return None
        pub = self.cache.get()
        metadata, _ = pub.expanded_metadata(self.clock())
        # The fact resolver sees all observed phases in every requested season.
        # Do not clarify identity against an unrelated or partial catalog.
        if (
            seasons is not None
            and not {(s, p) for s in seasons for p in PHASES} <= pub.approval.scopes
        ):
            return None
        appearances = [
            r
            for r in metadata["appearances"]
            if seasons is None or r["season"] in seasons
        ]
        players = players_from_rows(appearances)
        teams = sorted(
            {
                r[k]
                for r in appearances
                for k in ("team_abbr", "opponent_abbr")
                if r.get(k)
            }
        )
        provenance = dict(
            serving=True,
            publication_id=pub.approval.publication_id,
            relation_version=pub.approval.relation_version,
            source_snapshot_id=pub.approval.source_snapshot_id,
            publication_built_at=pub.approval.built_at.isoformat(),
            publication_expires_at=pub.approval.expires_at.isoformat(),
        )
        from app.agent.name_matching import PlayerNameError

        try:
            plan = planner(players, teams)
        except PlayerNameError as exc:
            exc.source_query = provenance
            raise
        raw_plan = deepcopy(plan)
        if plan.get("status") == "compare":
            return PreparedAttempt(plan, None, players, appearances, provenance)
        if normalizer is not None:
            plan = normalizer(plan, appearances)
        required_seasons = sorted(
            set(seasons or []).union(
                season
                for q in plan.get("queries", [])
                if q.get("window") == "date_range"
                for season in q.get("seasons", [])
            ),
            reverse=True,
        )
        if required_seasons:
            if (
                not {(s, p) for s in required_seasons for p in PHASES}
                <= pub.approval.scopes
            ):
                return PreparedAttempt(raw_plan, None, players, appearances, provenance)
            appearances = [
                r for r in metadata["appearances"] if r["season"] in required_seasons
            ]
            players = players_from_rows(appearances)
        from app.agent.semantic_planner import execute_plan

        def run(query):
            return self._run_candidate(query, pub, metadata)

        try:
            result = execute_plan(
                plan, None, players, query_runner=run, appearances=appearances
            )
        except CapabilityMiss:
            result = None
        return PreparedAttempt(
            raw_plan if result is None else plan,
            result,
            players,
            appearances,
            provenance,
            tuple(required_seasons),
        )

    def _run_candidate(self, query, pub, metadata):
        detail = (
            query.operation == "game_log"
            or query.window != "season_to_date"
            or any(
                getattr(query, k) is not None
                for k in (
                    "as_of",
                    "start_date",
                    "n",
                    "home_away",
                    "team_abbr",
                    "opponent_abbr",
                    "seasons",
                )
            )
        )
        family = (
            "detail"
            if detail
            else "rankings"
            if query.operation in ("rank", "percentile")
            else "points"
            if query.metric == "pts"
            else "metrics"
        )
        now = self.clock()
        contract = pub.approval.validate_contract_binding(
            pub.summaries, relation_version=pub.approval.relation_version, now=now
        )
        if family == "points" and not pub.approval._supports(
            query, now=now, contract=contract
        ):
            raise CapabilityMiss()
        if family not in self.families or (detail and pub.detail_digest is None):
            raise CapabilityMiss()
        if query.operation == "game_log" and hasattr(self.cache.reader, "run_game_log"):
            value = self.cache.reader.run_game_log(query, now=self.clock())
            if value is None:
                raise CapabilityMiss()
            return value
        selected = self.cache.get(detail=True) if detail else pub
        if metadata.get("capability_version") == "ask_serving_points/1":
            value = selected.run(
                query, now=self.clock(), reuse_cohort=self.reuse_cohort
            )
        else:
            value = selected.run_expanded(
                query, now=self.clock(), reuse_cohort=self.reuse_cohort
            )
        if value is None:
            raise CapabilityMiss()
        return value

    def load(self, seasons):
        self.serving_shadow = None
        from app.seasons import season_bounds

        if any(season_bounds(s)[1] >= self.clock().date() for s in seasons):
            return self.facts.load(seasons)
        if self.mode == "shadow":
            result = self.facts.load(seasons)
            self.serving_shadow = LazyShadow(self, result[1])
            return result
        if "rich" not in self.families:
            return self.facts.load(seasons)
        core = self.cache.get()
        if (
            core.detail_digest is None
            or not {(s, p) for s in seasons for p in PHASES} <= core.approval.scopes
        ):
            return self.facts.load(seasons)
        pub = self.cache.get(detail=True)
        if not any(s in seasons for s, p in pub.approval.scopes):
            return self.facts.load(seasons)
        evidence = pub.detail_evidence(now=self.clock())
        if evidence is None:
            return self.facts.load(seasons)
        if not all(
            any(s == season for s, p in evidence.covered_scopes) for season in seasons
        ):
            return self.facts.load(seasons)
        # Whole-request fact fallback occurs only for an undeclared capability.
        # Never merge partial populations or publications.
        if {s for s, p in evidence.covered_scopes} != set(seasons):
            from dataclasses import replace

            scopes = frozenset(
                (s, p) for s, p in evidence.covered_scopes if s in seasons
            )
            evidence = replace(
                evidence,
                rows=[r for r in evidence.rows if r["season"] in seasons],
                covered_scopes=scopes,
                data_through={
                    k: v for k, v in evidence.data_through.items() if k in scopes
                },
                source=scoped_source(
                    __import__("json").loads(pub.metadata_json), seasons
                ),
            )
        return {
            "capture": {
                "source_version": pub.approval.source_snapshot_id,
                "serving_publication": pub.approval.publication_id,
                "relation_version": pub.approval.relation_version,
                "publication_built_at": pub.approval.built_at.isoformat(),
                "publication_expires_at": pub.approval.expires_at.isoformat(),
            }
        }, evidence

    def load_for_request(self, seasons, *, rich=False):
        if self.mode == "serve" and not rich:
            return self.facts.load(seasons)
        return self.load(seasons)

    def players(self, evidence):
        return self.facts.players(evidence)


class FailedShadow:
    """Keep an optional candidate failure visible without replacing its reference."""

    def __init__(self, code):
        self.code = code
        self.outcomes = []

    def compare(self, query, reference):
        outcome = {"status": "error", "code": self.code}
        record_serving_shadow(outcome)
        if len(self.outcomes) < 64:
            self.outcomes.append(outcome)


class RefreshingServingWarehouse:
    """Pin one immutable version for the complete governed request.

    Read a bounded private reference on selection, so atomic operator promotion
    is visible without restarting. Invalid selected references fail visibly;
    previous versions are retained only as bounded caches, never error fallback.
    """

    def __init__(
        self,
        facts,
        client,
        reference_path,
        clock,
        *,
        project,
        mode,
        reuse_cohort=False,
        families=("points",),
    ) -> None:
        from collections import OrderedDict
        from pathlib import Path

        self.facts, self.client, self.path, self.clock = (
            facts,
            client,
            Path(reference_path),
            clock,
        )
        self.project, self.mode, self.reuse_cohort, self.families = (
            project,
            mode,
            reuse_cohort,
            families,
        )
        self._lock = Lock()
        self._versions: OrderedDict[object, ServingWarehouse] = OrderedDict()
        self._last: ServingWarehouse | None = None
        self._last_shadow: ServingShadow | FailedShadow | None = None

    def _reference(self):
        import json
        import stat

        from app.agent.serving_bigquery import ServingReference

        policy = CURRENT.get()
        state = policy.serving_state.setdefault(self, {}) if policy else None
        if state is not None and "reference" in state:
            return state["reference"]
        with stage("serving_reference"):
            try:
                if not stat.S_ISREG(self.path.stat().st_mode):
                    raise ValueError("Regular reference file required")
                with self.path.open("rb") as stream:
                    content = stream.read(32769)
                if len(content) > 32768:
                    raise ValueError("Reference size bound exceeded")
                reference = ServingReference(**json.loads(content))
                reference.relation()
                if reference.project != self.project:
                    raise ValueError("Reference project mismatch")
            except (OSError, ValueError, TypeError) as exc:
                raise SemanticError(
                    "invalid_evidence", "Serving reference unavailable or invalid"
                ) from exc
        if state is not None:
            state["reference"] = reference
        return reference

    def _select(self) -> ServingWarehouse:
        from app.agent.serving_bigquery import BigQueryServingReader

        policy = CURRENT.get()
        state = policy.serving_state.setdefault(self, {}) if policy else None
        if state is not None and "target" in state:
            return state["target"]
        reference = self._reference()
        acquire_timed_lock(self._lock, "serving_version_wait", 5)
        try:
            target = self._versions.get(reference)
            if target is None:
                target = ServingWarehouse(
                    self.facts,
                    BigQueryServingReader(self.client, reference),
                    self.clock,
                    mode=self.mode,
                    reuse_cohort=self.reuse_cohort,
                    families=self.families,
                )
                self._versions[reference] = target
                while len(self._versions) > 2:
                    self._versions.popitem(last=False)
            self._versions.move_to_end(reference)
            self._last = target
            if state is not None:
                state["target"] = target
            return target
        finally:
            self._lock.release()

    def _completed(self, seasons):
        from app.seasons import season_bounds

        return all(season_bounds(s)[1] < self.clock().date() for s in seasons)

    def prepare(self, planner, *, seasons=None, normalizer=None, question=None):
        if self.mode == "shadow":
            return None
        if not set(self.families) & {"points", "metrics", "rankings", "detail"}:
            return None
        if not question_may_use_serving(question, self.families):
            return None
        if seasons is not None and (
            not self._completed(seasons) or not self._reference().covers(seasons)
        ):
            return None
        return self._select().prepare(
            planner, seasons=seasons, normalizer=normalizer, question=question
        )

    def load(self, seasons):
        if not self._completed(seasons):
            return self.facts.load(seasons)
        if self.mode != "shadow":
            if "rich" not in self.families:
                return self.facts.load(seasons)
            if not self._reference().covers(seasons):
                return self.facts.load(seasons)
            return self._select().load(seasons)
        result = self.facts.load(seasons)
        shadow = LazyShadow(self, result[1])
        policy = CURRENT.get()
        if policy is not None:
            policy.serving_state.setdefault(self, {})["shadow"] = shadow
        else:
            self._last_shadow = shadow
        return result

    def load_for_request(self, seasons, *, rich=False):
        if self.mode == "serve" and not rich:
            return self.facts.load(seasons)
        return self.load(seasons)

    def players(self, evidence):
        return self.facts.players(evidence)

    @property
    def serving_shadow(self):
        policy = CURRENT.get()
        if policy is not None:
            return policy.serving_state.get(self, {}).get("shadow")
        return self._last_shadow


class LazyShadow:
    """Only scalar execution compares; other routes incur no candidate reads."""

    def __init__(self, warehouse, evidence):
        self.warehouse, self.evidence = warehouse, evidence
        self.outcomes = []

    def compare(self, query, reference):
        warehouse = self.warehouse
        # Check the resolved query's family before selecting a reference/core.
        detail = (
            query.operation == "game_log"
            or query.window != "season_to_date"
            or any(
                getattr(query, k) is not None
                for k in (
                    "as_of",
                    "start_date",
                    "n",
                    "home_away",
                    "team_abbr",
                    "opponent_abbr",
                    "seasons",
                )
            )
        )
        family = (
            "detail"
            if detail
            else "rankings"
            if query.operation in ("rank", "percentile")
            else "points"
            if query.metric == "pts"
            else "metrics"
        )
        if family not in warehouse.families:
            record_serving_shadow({"status": "ineligible"})
            self.outcomes.append({"status": "ineligible"})
            return
        try:
            if getattr(warehouse, "_shadow_failed_until", 0) > monotonic():
                raise SemanticError(
                    "shadow_retry_deferred", "Recent optional shadow failure"
                )
            with optional_shadow_budget(15.0):
                target = (
                    warehouse._select() if hasattr(warehouse, "_select") else warehouse
                )
                shadow = target.candidate_shadow(self.evidence)
                shadow.compare(query, reference)
                self.outcomes.extend(shadow.outcomes)
                if shadow.outcomes and shadow.outcomes[-1]["status"] == "error":
                    warehouse._shadow_failed_until = monotonic() + 2.0
        except Exception as exc:
            warehouse._shadow_failed_until = monotonic() + 2.0
            failure = FailedShadow(
                exc.code
                if isinstance(exc, (SemanticError, ExecutionFailure))
                else "shadow_failure"
            )
            failure.compare(query, reference)
            self.outcomes.extend(failure.outcomes)


def question_may_use_serving(question, families):
    """Decline known disabled requirements before reading a candidate catalog."""
    if question is None:
        return True
    import re

    from app.agent.planner import explicit_metric_mentions
    from app.agent.semantic_planner import explicit_scope

    text = question.casefold()
    scope = explicit_scope(question)
    detailed = bool(set(scope) - {"season_type"}) or bool(
        re.search(
            r"\b(?:home|away|against|versus|vs|game logs?|since|before|after|between)\b",
            text,
        )
    )
    ranked = bool(
        re.search(
            r"\b(?:top|rank|ranking|ranked|percentile|leaderboard|leaders)\b", text
        )
    )
    metrics = explicit_metric_mentions(question)
    if detailed:
        return "detail" in families
    if ranked:
        return "rankings" in families
    if metrics and (metrics - {"pts"}):
        return "metrics" in families
    return "points" in families if metrics == {"pts"} else True
