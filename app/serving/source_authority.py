"""Independent, operator-pinned production authority; never issued by a builder."""

import json
import re
from pathlib import Path

from app.serving.control_cache import ControlCache
from app.serving.publication_contract import (
    PROFILE,
    digest,
    timestamp,
    validate_binding,
)
from app.serving.weekly_profile import weekly_scope


class ProviderAuthorities:
    """Route each published release to its own independently pinned policy."""

    def __init__(self, authorities, *, clock):
        if not authorities or not set(authorities) <= {"nba_api", "espn"}:
            raise ValueError("invalid source authorities")
        self.authorities, self.clock = dict(authorities), clock

    def policy(self, use):
        for provider in ("nba_api", "espn"):
            if provider in self.authorities:
                try:
                    policy, registry = self.authorities[provider].policy(use)
                    if policy["provider"] != provider:
                        raise ValueError("source policy provider differs")
                    return policy, registry
                except ValueError:
                    continue
        raise ValueError("production source authority unavailable")

    def check(self, manifest, binding):
        provider = manifest["scope"]["provider"]
        authority = self.authorities.get(provider)
        if authority is None:
            raise ValueError("production source authority unavailable")
        return authority.check(manifest, binding)

    def authorize_scope(self, use, scope):
        scope = weekly_scope(scope)
        authority = self.authorities.get(scope["provider"])
        if authority is None:
            raise ValueError("production source authority unavailable")
        policy, registry = authority.authorize_scope(use, scope)
        if policy["provider"] != scope["provider"]:
            raise ValueError("source policy provider differs")
        return policy, registry


class SourceAuthority:
    def __init__(
        self,
        *,
        policy_path,
        policy_digest,
        registry_path,
        clock,
        storage_client=None,
        cache_ttl=0,
    ):
        self.policy_path = (
            str(policy_path)
            if str(policy_path).startswith("gs://")
            else Path(policy_path)
        )
        self.registry_path = (
            str(registry_path)
            if str(registry_path).startswith("gs://")
            else Path(registry_path)
        )
        self.policy_digest, self.clock = policy_digest, clock
        self.storage_client = storage_client
        self.documents = ControlCache(cache_ttl)

    def read_document(self, location):
        if isinstance(location, Path):
            return json.loads(location.read_text())
        return self.documents.get(location, lambda: self._read_cloud_document(location))

    def _read_cloud_document(self, location):
        match = re.fullmatch(r"gs://([a-z0-9][a-z0-9._-]{1,220})/([^?#]+)", location)
        if not match or any(part in {"", ".", ".."} for part in match[2].split("/")):
            raise ValueError("invalid production registry URI")
        try:
            if self.storage_client is None:
                from google.cloud import storage

                self.storage_client = storage.Client()
            blob = self.storage_client.bucket(match[1]).blob(match[2])
            blob.reload(timeout=30)
            if (
                blob.size is None
                or not 0 < blob.size <= 2 * 1024 * 1024
                or blob.generation is None
            ):
                raise ValueError("production registry size or generation unavailable")
            data = blob.download_as_bytes(
                if_generation_match=blob.generation, timeout=30
            )
            if len(data) != blob.size:
                raise ValueError("production registry changed")
            return json.loads(data)
        except Exception as exc:
            raise ValueError("production registry unavailable") from exc

    def policy(self, use):
        try:
            return self._policy(use)
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise ValueError("production source authority unavailable") from exc

    def authorize_scope(self, use, scope):
        policy, registry = self.policy(use)
        scope, allowed = weekly_scope(scope), policy["scope"]
        if (
            any(scope[k] != allowed[k] for k in ("season", "phase", "provider"))
            or not allowed["first"]
            <= scope["first"]
            <= scope["last"]
            <= allowed["last"]
        ):
            raise ValueError("production scope outside operator policy")
        return policy, registry

    def _policy(self, use):
        policy = self.read_document(self.policy_path)
        registry = self.read_document(self.registry_path)
        now = timestamp(self.clock())
        if (
            digest(policy) != self.policy_digest
            or policy["version"] != "source-use-policy/1"
            or registry["version"] != "source-revocations/1"
            or registry["policy_digest"] != self.policy_digest
            or registry["policy_revoked"] is not False
            or use not in policy["permitted_uses"]
            or (
                policy["provider"],
                policy["source_method"],
                policy["source_selection"],
                policy["freshness"],
            )
            not in {
                (
                    "espn",
                    "reviewed_espn_game_summary",
                    "unified_source_selection/1",
                    "weekly_8d_10d_v1",
                ),
                (
                    "nba_api",
                    "reviewed_nba_traditional_boxscore_v3",
                    "nba_manual_selection/1",
                    "weekly_8d_10d_v1",
                ),
            }
            or policy["scope"]["provider"] != policy["provider"]
            or set(policy["fields"])
            != set(PROFILE["assets"]["fct_player_game_stats"]["required_fields"])
            or policy["semantics"] != PROFILE["semantics_version"]
            or not timestamp(policy["valid_from"])
            <= now
            < timestamp(policy["valid_until"])
            or not timestamp(registry["checked_at"])
            <= now
            < timestamp(registry["valid_until"])
        ):
            raise ValueError("production source authority unavailable")
        weekly_scope(policy["scope"])
        return policy, registry

    def check(self, manifest, binding):
        try:
            return self._check(manifest, binding)
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise ValueError("production release authority unavailable") from exc

    def _check(self, manifest, binding):
        policy, registry = self.policy("serving")
        validate_binding(binding, manifest)
        if (
            manifest["version"] != "publication-manifest/2"
            or manifest["purpose"] != "production"
            or manifest["evidence_kind"] != "retained"
        ):
            raise ValueError("production requires a distinct retained weekly manifest")
        scope, allowed = manifest["scope"], policy["scope"]
        if (
            any(scope[k] != allowed[k] for k in ("season", "phase", "provider"))
            or manifest["versions"]["source_policy"] != policy["source_selection"]
            or manifest["freshness"]["policy"] != policy["freshness"]
            or not allowed["first"]
            <= scope["first"]
            <= scope["last"]
            <= allowed["last"]
        ):
            raise ValueError("production scope outside operator policy")
        if manifest["publication_id"] in registry["revoked_publications"]:
            raise ValueError("production publication revoked")
        inputs = {r["role"]: r for r in manifest["inputs"]}
        if inputs["source_authority"]["digest"] != self.policy_digest or any(
            i["digest"] in registry["revoked_dependencies"] for i in inputs.values()
        ):
            raise ValueError("production dependency authority differs")
        validation = registry["validated_releases"].get(manifest["publication_id"])
        if not validation or validation != {
            "manifest_digest": digest(manifest),
            "binding_digest": digest(binding),
            "input_fingerprint": manifest["input_fingerprint"],
            "code_versions": manifest["versions"],
            "check_receipts": {
                c["name"]: c["receipt_digest"] for c in manifest["checks"]
            },
        }:
            raise ValueError("independent release validation missing or changed")
        now = timestamp(self.clock())
        if (
            not timestamp(manifest["validation_as_of"])
            <= now
            < min(
                timestamp(manifest["freshness"]["expires_at"]),
                timestamp(policy["valid_until"]),
            )
        ):
            raise ValueError("production source expired")
        return True
