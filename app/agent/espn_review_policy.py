"""Trusted, immutable ESPN capture for bounded real-provider local review.

The operator captures the bounded source before installing this policy. Request
handlers cannot select a bundle, widen its scope or execute warehouse queries.
"""

import hashlib
import json
from dataclasses import dataclass

from app.agent.espn_dev_source import DEV_DATASET, snapshot
from app.agent.semantic_source import (
    BigQuerySemanticSource,
    snapshot_digest,
    snapshot_evidence,
)
from app.agent.source_policy import SourcePolicy, denied, get_source_policy
from app.seasons import DEFAULT_SEASON


@dataclass(frozen=True)
class ESPNReviewPolicy(SourcePolicy):
    source_mode: str = "espn_pinned_review"
    bundle_json: str = ""
    bundle_sha256: str = ""

    def __post_init__(self):
        super().__post_init__()
        if (
            self.source_mode != "espn_pinned_review"
            or len(self.relations) != 1
            or self.relations[0][0] != DEFAULT_SEASON
            or self.phases != ("Regular Season",)
            or self.relations[0][1].split(".")[1:]
            != [DEV_DATASET, "espn_layer_player_box_scores"]
            or not isinstance(self.bundle_json, str)
            or not 0 < len(self.bundle_json.encode()) <= 16_000_000
            or hashlib.sha256(self.bundle_json.encode()).hexdigest()
            != self.bundle_sha256
        ):
            denied("Invalid pinned ESPN source scope or checksum")
        self.capture([DEFAULT_SEASON], ["Regular Season"])

    def capture(self, seasons, phases):
        self.check_scope(seasons, phases)
        value = snapshot(
            json.loads(self.bundle_json), DEFAULT_SEASON, self.relations[0][1]
        )
        if len(value["rows"]) > self.max_rows:
            denied("Pinned ESPN row cap exceeded")
        value["source_timestamp"] = self.snapshot_at
        value["capture"] = {
            "query_count": 0,
            "pinned_bundle_sha256": self.bundle_sha256,
        }
        value["sha256"] = snapshot_digest(value)
        snapshot_evidence(value)
        return value


class PinnedESPNSource(BigQuerySemanticSource):
    def capture_scoped(self, seasons, phases):
        policy = get_source_policy(self.source_policy)
        if type(policy) is not ESPNReviewPolicy:
            denied("Pinned ESPN source requires its explicit policy")
        return policy.capture(seasons, phases)

    def capture(self, seasons):
        return self.capture_scoped(seasons, ["Regular Season"])
