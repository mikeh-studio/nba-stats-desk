"""Read-only isolated warehouse release protocol. No default app activation.

The rehearsal receipt is deliberately distinct from production source authority.
Only trusted server composition supplies the project and control namespace.
"""

from __future__ import annotations

import copy
import json
import math
import re
from datetime import date, datetime

from app.serving.core_dbt import isolated_dataset
from app.serving.publication_contract import PROFILE, digest, timestamp

KEYS = {
    "accepted_game_results": ["game_id"],
    "dim_game": ["game_id"],
    "accepted_player_appearances": ["game_id", "player_id"],
    "participant_manifest": ["game_id", "player_id"],
    "fct_player_game_stats": ["game_id", "player_id"],
    "fct_team_game_results": ["game_id", "team_abbr"],
    "dim_player": ["player_id"],
    "dim_team": ["team_id"],
    "core_player_summary": ["player_id"],
    "core_player_windows": ["player_id", "window_key"],
    "core_player_trends": ["player_id", "metric", "aggregation"],
    "core_player_search": ["player_id"],
    "core_performance": ["game_id", "player_id"],
}


def canonical(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str) and re.fullmatch(
        r"\d{4}-\d\d-\d\dT.*(?:Z|\+00:00)", value
    ):
        return timestamp(value).isoformat()
    if isinstance(value, dict):
        return {k: canonical(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [canonical(v) for v in value]
    return value


def ordered(name, rows):
    rows = [canonical(dict(r)) for r in rows]
    keys = [tuple(r[k] for k in KEYS[name]) for r in rows]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate model primary key")
    return sorted(rows, key=lambda r: tuple(r[k] for k in KEYS[name]))


def equal_values(actual, expected):
    if isinstance(actual, dict) and isinstance(expected, dict):
        return set(actual) == set(expected) and all(
            equal_values(actual[k], expected[k]) for k in actual
        )
    if isinstance(actual, list) and isinstance(expected, list):
        return len(actual) == len(expected) and all(
            equal_values(a, e) for a, e in zip(actual, expected)
        )
    if type(actual) in (int, float) and type(expected) in (int, float):
        return math.isclose(actual, expected, rel_tol=1e-12, abs_tol=1e-12)
    return actual == expected


def validate_receipt(receipt, *, project, namespace, now):
    isolated_dataset(namespace)
    pid = receipt["publication_id"]
    if not re.fullmatch(r"pub_[0-9a-f]{16}", pid):
        raise ValueError("invalid publication ID")
    if (
        receipt["version"] != "core-warehouse-rehearsal/1"
        or receipt["purpose"] != "isolated_warehouse_rehearsal"
        or receipt["runtime_eligible"] is not False
        or receipt["profile_digest"] != digest(PROFILE)
        or receipt["scope"] != PROFILE["scope"]
        or receipt["semantics"] != PROFILE["semantics_version"]
        or set(receipt["assets"]) != set(PROFILE["assets"])
    ):
        raise ValueError("incompatible rehearsal receipt")
    if (
        not timestamp(receipt["validation_as_of"])
        <= timestamp(now)
        < timestamp(receipt["expires_at"])
    ):
        raise ValueError("release expired or clock invalid")
    if timestamp(receipt["expires_at"]) > timestamp(receipt["source_valid_until"]):
        raise ValueError("release extends source freshness")
    for name, asset in receipt["assets"].items():
        if asset["relation"] != f"{project}.{namespace}_release_{pid[4:]}.{name}":
            raise ValueError("release relation outside trusted namespace")
        if (
            not re.fullmatch(r"[0-9a-f]{64}", asset["digest"])
            or not 0 < asset["rows"] <= 100000
        ):
            raise ValueError("invalid asset attestation")


class Resolver:
    """All reads use a server-issued immutable ID, including retained flow tokens."""

    def __init__(self, client, *, project, namespace, clock):
        if not re.fullmatch(r"[a-z][a-z0-9-]{4,61}[a-z0-9]", project):
            raise ValueError("invalid project")
        self.client, self.project, self.namespace, self.clock = (
            client,
            project,
            isolated_dataset(namespace),
            clock,
        )
        self.cache = {}

    def query(self, sql, parameters=()):
        from google.cloud import bigquery

        config = bigquery.QueryJobConfig(
            maximum_bytes_billed=268435456,
            query_parameters=list(parameters),
            use_query_cache=False,
        )
        try:
            return list(
                self.client.query(sql, job_config=config, location="US").result(
                    timeout=120
                )
            )
        except Exception as exc:
            raise ValueError("warehouse release registry unavailable") from exc

    def resolve(self, token=None):
        if token is None:
            rows = self.query(
                f"SELECT active_id FROM `{self.project}.{self.namespace}_control.pointer` WHERE slot='main'"
            )
            if len(rows) != 1 or not rows[0]["active_id"]:
                raise ValueError("active publication unavailable")
            token = rows[0]["active_id"]
        receipt = self.check(token)
        return PinnedRelease(self, copy.deepcopy(receipt))

    def check(self, token):
        from google.cloud import bigquery

        if not isinstance(token, str) or not re.fullmatch(r"pub_[0-9a-f]{16}", token):
            raise ValueError("unknown publication token")
        rows = self.query(
            f"SELECT * FROM `{self.project}.{self.namespace}_control.releases` WHERE publication_id=@id",
            [bigquery.ScalarQueryParameter("id", "STRING", token)],
        )
        if len(rows) != 1 or not rows[0]["eligible"] or rows[0]["revoked"]:
            raise ValueError("publication unavailable or revoked")
        r = rows[0]
        receipt = json.loads(r["manifest"])
        if (
            receipt["publication_id"] != token
            or digest(receipt) != r["manifest_digest"]
        ):
            raise ValueError("manifest digest differs")
        validate_receipt(
            receipt, project=self.project, namespace=self.namespace, now=self.clock()
        )
        return receipt


class PinnedRelease:
    def __init__(self, resolver, receipt):
        self.resolver, self.receipt = resolver, receipt
        self.publication_id = receipt["publication_id"]
        self.cache_key = (
            self.publication_id,
            digest(receipt),
            receipt["semantics"],
            digest(receipt["scope"]),
            receipt["input_digest"],
        )

    def check(self):
        if self.resolver.check(self.publication_id) != self.receipt:
            raise ValueError("pinned release changed")

    def verify_assets(self):
        self.check()
        for spec in self.receipt["assets"].values():
            self.verify_asset(spec)

    def verify_asset(self, spec):
        try:
            table = self.resolver.client.get_table(spec["relation"], timeout=30)
        except Exception as exc:
            raise ValueError("immutable asset unavailable") from exc
        if (
            table.table_type != "SNAPSHOT"
            or table.etag != spec["etag"]
            or table.num_rows != spec["rows"]
            or table.expires is None
            or table.expires < timestamp(self.receipt["expires_at"])
        ):
            raise ValueError("immutable asset unavailable or changed")
        return table

    def asset(self, name):
        if name not in PROFILE["assets"]:
            raise ValueError("asset not allowlisted")
        self.check()
        spec = self.receipt["assets"][name]
        table = self.verify_asset(spec)
        key = self.cache_key + (name,)
        if key not in self.resolver.cache:
            try:
                rows = ordered(
                    name,
                    self.resolver.client.list_rows(table, max_results=spec["rows"] + 1),
                )
            except Exception as exc:
                raise ValueError("immutable asset read failed") from exc
            if len(rows) != spec["rows"] or digest(rows) != spec["digest"]:
                raise ValueError("asset contents differ")
            self.resolver.cache[key] = rows
        self.check()
        return copy.deepcopy(self.resolver.cache[key])
