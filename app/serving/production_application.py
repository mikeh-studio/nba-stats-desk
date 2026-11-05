"""Disabled-by-default production composition with independently pinned authority.

The server supplies the exact project/control namespace and SourceAuthority.
Rehearsal receipts and caller-authored relation maps are never accepted.
"""

import json
import re
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

from scripts.validate_publication_contract import digest, timestamp
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from app.serving.control_cache import ControlCache
from app.serving.core_application import create_core_application
from app.serving.cutover import publication_token
from app.serving.publication import Resolver, ordered


class ProductionApplication:
    production_reader = True

    def __init__(self, *, client, project, namespace, authority, cache_ttl=5):
        if not re.fullmatch(
            r"[a-z][a-z0-9-]{4,61}[a-z0-9]", project
        ) or not re.fullmatch(r"nba_core_[a-z0-9_]{3,64}", namespace):
            raise ValueError("untrusted production target")
        self.client, self.project, self.namespace = client, project, namespace
        self.authority, self.apps, self.lock = (
            authority,
            OrderedDict(),
            threading.Lock(),
        )
        self.controls = ControlCache(cache_ttl)

    query = Resolver.query

    def release(self, token):
        if token is None:
            token = self.controls.get("pointer", self.active_id)
        return self.controls.get(("release", token), lambda: self._release(token))

    def active_id(self):
        rows = self.query(
            f"SELECT active_id FROM `{self.project}.{self.namespace}_control.pointer` WHERE slot='main'"
        )
        if len(rows) != 1:
            raise ValueError("production pointer unavailable")
        return rows[0]["active_id"]

    def _release(self, token):
        from google.cloud import bigquery

        if not re.fullmatch(r"pub_[0-9a-f]{16}", token or ""):
            raise ValueError("invalid publication")
        rows = self.query(
            f"SELECT * FROM `{self.project}.{self.namespace}_control.releases` WHERE publication_id=@id",
            [bigquery.ScalarQueryParameter("id", "STRING", token)],
        )
        if len(rows) != 1 or not rows[0]["eligible"] or rows[0]["revoked"]:
            raise ValueError("production release unavailable")
        envelope = json.loads(rows[0]["manifest"])
        if digest(envelope) != rows[0]["manifest_digest"]:
            raise ValueError("production registry digest differs")
        manifest = envelope["manifest"]
        if manifest["publication_id"] != token:
            raise ValueError("production registry identity differs")
        for asset in manifest["assets"]:
            if (
                asset["relation"]
                != f"{self.project}.{self.namespace}_release_{token[4:]}.{asset['name']}"
            ):
                raise ValueError("production relation outside configured target")
        return envelope

    def tables(self, manifest):
        return self.controls.get(
            ("tables", digest(manifest)), lambda: self._tables(manifest)
        )

    def _tables(self, manifest):
        def read(asset):
            table = self.client.get_table(asset["relation"], timeout=30)
            if (
                table.table_type != "SNAPSHOT"
                or table.num_rows != asset["rows"]
                or table.expires is None
                or table.expires < timestamp(manifest["freshness"]["expires_at"])
            ):
                raise ValueError("production snapshot unavailable")
            return asset["name"], table

        assets = manifest["assets"]
        if not assets:
            raise ValueError("production snapshot unavailable")
        # The release schema bounds the asset set. Keep concurrency bounded and
        # require every metadata read to pass before returning any table mapping.
        with ThreadPoolExecutor(max_workers=min(6, len(assets))) as pool:
            return dict(pool.map(read, assets))

    def application(self, token):
        # Fail closed before warehouse reads, but never prevent legacy startup.
        self.authority.policy("serving")
        envelope = self.release(token)
        manifest, binding = envelope["manifest"], envelope["binding"]
        key = (
            manifest["publication_id"],
            digest(envelope),
            manifest["input_fingerprint"],
            digest(manifest["scope"]),
        )
        with self.lock:
            if key not in self.apps:
                # Validate the exact manifest and binding before any asset read;
                # cached apps repeat this validation in their request gate.
                self.authority.check(manifest, binding)
                tables = self.tables(manifest)
                metadata = {name: table.etag for name, table in tables.items()}

                def read_asset(asset):
                    name = asset["name"]
                    rows = ordered(
                        name,
                        self.client.list_rows(
                            tables[name], max_results=asset["rows"] + 1
                        ),
                    )
                    if len(rows) != asset["rows"] or digest(rows) != asset["digest"]:
                        raise ValueError("production content differs")
                    return name, rows

                # Read immutable assets concurrently, but do not cache or serve
                # anything until every bounded read and digest check succeeds.
                assets = manifest["assets"]
                with ThreadPoolExecutor(max_workers=min(6, len(assets))) as pool:
                    models = dict(pool.map(read_asset, assets))
                accepted = dict(
                    status="accepted",
                    authority_scope="offline_model_validation_only",
                    runtime_eligible=False,
                    scope=manifest["scope"],
                    games=models["accepted_game_results"],
                    appearances=models["accepted_player_appearances"],
                    participants=models["participant_manifest"],
                    players=models["dim_player"],
                    teams=models["dim_team"],
                )
                bundle = dict(
                    manifest=manifest, binding=binding, accepted=accepted, models=models
                )

                def authorize(*_):
                    try:
                        if self.release(manifest["publication_id"]) != envelope:
                            raise ValueError("pinned production release changed")
                        if {
                            n: t.etag for n, t in self.tables(manifest).items()
                        } != metadata:
                            raise ValueError("production snapshot replaced")
                        return True
                    except Exception as exc:
                        raise ValueError("production release unavailable") from exc

                self.apps[key] = create_core_application(
                    bundle,
                    expected_digest=digest(bundle),
                    clock=self.authority.clock,
                    authorize=authorize,
                    production_authority=self.authority,
                )
                while len(self.apps) > 2:
                    self.apps.popitem(last=False)
            self.apps.move_to_end(key)
            return self.apps[key]

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            return
        try:
            app = await run_in_threadpool(self.application, publication_token(scope))
        except Exception:
            return await JSONResponse(
                {"detail": "publication unavailable; refresh the flow"}, status_code=503
            )(scope, receive, send)
        await app(scope, receive, send)
