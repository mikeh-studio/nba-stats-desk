"""Trusted, explicit warehouse read limits, independent of Ask request tracing.

Only the two generated query shapes below are supported. This is not a SQL parser;
all ordinary repository SQL is denied while a source policy is configured.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from typing import Iterator, NoReturn

from app.agent.semantics import COMPONENTS, PHASES, SemanticError
from app.seasons import validate_season


def denied(message: str) -> NoReturn:
    raise SemanticError("source_policy_denied", message)


def capture_sql(sources, phases=None):
    columns = ", ".join(
        [
            "season",
            "season_type",
            "game_id",
            "player_id",
            "player_name",
            "game_date",
            "team_abbr",
            "opponent_abbr",
            "home_away",
            *sorted(COMPONENTS),
        ]
    )
    selects = [
        f"SELECT {columns} FROM `{source}` FOR SYSTEM_TIME AS OF @snapshot_at "
        f"WHERE season = @season_{i}"
        + (" AND season_type IN UNNEST(@phases)" if phases is not None else "")
        for i, source in enumerate(sources)
    ]
    return (
        "WITH source AS (" + " UNION ALL ".join(selects) + ") "
        "SELECT *, COUNT(*) OVER() AS source_row_count FROM source "
        "ORDER BY season, game_date, game_id, player_id LIMIT @row_limit"
    )


def identity_sql(relation):
    return (
        "SELECT DISTINCT season, season_type, player_id, player_name, team_abbr "
        f"FROM `{relation}` FOR SYSTEM_TIME AS OF @snapshot_at "
        "WHERE season = @season AND season_type IN UNNEST(@phases) "
        "AND player_id IN UNNEST(@player_ids) "
        "ORDER BY player_id, player_name, team_abbr LIMIT @row_limit"
    )


@dataclass(frozen=True)
class SourcePolicy:
    """Server-authored allowlist; never construct from a request payload.

    ``relations`` binds each season to exactly one reviewed relation. Snapshot
    timestamps, caps, and source mode are immutable for a configured session.
    Identity IDs limit name resolution, not the statistical comparison cohort.
    """

    relations: tuple[tuple[str, str], ...]
    phases: tuple[str, ...]
    snapshot_at: str
    allowed_player_ids: frozenset[int]
    source_mode: str = "warehouse"
    max_rows: int = 100_000
    maximum_bytes_billed: int = 100_000_000
    submission_timeout: float = 30
    result_timeout: float = 120
    identity_max_rows: int = 1000

    def __post_init__(self):
        if not self.relations or len(dict(self.relations)) != len(self.relations):
            denied("Supply distinct season relations")
        for season, relation in self.relations:
            validate_season(season)
            if not re.fullmatch(
                r"[a-z][a-z0-9-]{4,62}\.[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*",
                relation,
            ):
                denied("Invalid approved relation")
        if (
            not self.phases
            or len(set(self.phases)) != len(self.phases)
            or any(p not in PHASES for p in self.phases)
        ):
            denied("Supply distinct canonical phases")
        try:
            stamp = datetime.fromisoformat(self.snapshot_at)
            if stamp.utcoffset() is None:
                raise ValueError("timezone required")
        except (ValueError, TypeError) as exc:
            raise SemanticError(
                "source_policy_denied", "Invalid snapshot timestamp"
            ) from exc
        if self.source_mode not in {"warehouse", "retained_local_review"}:
            denied("Unknown source mode")
        for value, ceiling in (
            (self.max_rows, 100_000),
            (self.identity_max_rows, 1000),
            (self.maximum_bytes_billed, 100_000_000),
        ):
            if type(value) is not int or not 1 <= value <= ceiling:
                denied("Invalid source read cap")
        if any(
            type(v) not in (int, float) or not 0 < v <= cap
            for v, cap in ((self.submission_timeout, 30), (self.result_timeout, 120))
        ):
            denied("Invalid source timeout")
        if not self.allowed_player_ids or any(
            type(p) is not int or p < 1 for p in self.allowed_player_ids
        ):
            denied("Supply explicit positive identity IDs")
        # Copy mutable caller collections into immutable storage.
        object.__setattr__(self, "relations", tuple(tuple(x) for x in self.relations))
        object.__setattr__(self, "phases", tuple(self.phases))
        object.__setattr__(
            self, "allowed_player_ids", frozenset(self.allowed_player_ids)
        )

    def relation(self, season):
        relation = dict(self.relations).get(season)
        if relation is None:
            denied("Season is outside the approved source scope")
        return relation

    def check_scope(self, seasons, phases):
        if not seasons or len(set(seasons)) != len(seasons):
            denied("Supply distinct explicit seasons")
        for season in seasons:
            self.relation(season)
        if (
            not phases
            or len(set(phases)) != len(phases)
            or not set(phases) <= set(self.phases)
        ):
            denied("Phase is outside the approved source scope")

    def authorize(self, *, sql, config, seasons, phases, identity_ids=None):
        """Compare the complete generated statement and typed parameter payload."""
        self.check_scope(seasons, phases)
        from google.cloud import bigquery

        params = [
            bigquery.ScalarQueryParameter("snapshot_at", "TIMESTAMP", self.snapshot_at)
        ]
        if identity_ids is None:
            expected_sql = capture_sql([self.relation(s) for s in seasons], phases)
            params += [
                bigquery.ScalarQueryParameter("row_limit", "INT64", self.max_rows + 1)
            ]
            params += [
                bigquery.ScalarQueryParameter(f"season_{i}", "STRING", s)
                for i, s in enumerate(seasons)
            ]
        else:
            if (
                len(seasons) != 1
                or not identity_ids
                or any(type(pid) is not int for pid in identity_ids)
                or not set(identity_ids) <= self.allowed_player_ids
            ):
                denied("Identity is outside the approved source scope")
            expected_sql = identity_sql(self.relation(seasons[0]))
            params += [bigquery.ScalarQueryParameter("season", "STRING", seasons[0])]
            params += [
                bigquery.ArrayQueryParameter(
                    "player_ids", "INT64", sorted(identity_ids)
                )
            ]
            params += [
                bigquery.ScalarQueryParameter(
                    "row_limit", "INT64", self.identity_max_rows + 1
                )
            ]
        params += [bigquery.ArrayQueryParameter("phases", "STRING", list(phases))]
        expected = [p.to_api_repr() for p in params]
        actual = config.to_api_repr().get("query", {}).get("queryParameters", [])
        if sql != expected_sql or actual != expected:
            denied("Unknown query shape or typed source parameters")
        job_api = config.to_api_repr()
        if set(job_api) - {"query", "labels"}:
            denied("Unsupported warehouse job configuration")
        api = job_api.get("query", {})
        if set(api) - {
            "queryParameters",
            "parameterMode",
            "maximumBytesBilled",
            "useLegacySql",
            "useQueryCache",
        }:
            denied("Unsupported warehouse job configuration")
        if (
            config.use_legacy_sql is not False
            or config.use_query_cache is not False
            or type(config.maximum_bytes_billed) is not int
            or not 0 < config.maximum_bytes_billed <= self.maximum_bytes_billed
        ):
            denied("Warehouse caps or SQL mode differ from source policy")


CURRENT_SOURCE_POLICY: ContextVar[SourcePolicy | None] = ContextVar(
    "source_policy", default=None
)
_CONFIGURED_SOURCE_POLICY: SourcePolicy | None = None


def configure_source_policy(policy: SourcePolicy | None) -> None:
    """Install before application lifespan; trusted composition only."""
    global _CONFIGURED_SOURCE_POLICY
    _CONFIGURED_SOURCE_POLICY = policy


def get_source_policy(explicit: SourcePolicy | None = None) -> SourcePolicy | None:
    candidates = [
        p
        for p in (_CONFIGURED_SOURCE_POLICY, CURRENT_SOURCE_POLICY.get(), explicit)
        if p is not None
    ]
    if candidates and any(p != candidates[0] for p in candidates[1:]):
        denied("Conflicting trusted source policies")
    return candidates[0] if candidates else None


@contextmanager
def source_policy_scope(policy: SourcePolicy) -> Iterator[None]:
    get_source_policy(policy)
    token = CURRENT_SOURCE_POLICY.set(policy)
    try:
        yield
    finally:
        CURRENT_SOURCE_POLICY.reset(token)
