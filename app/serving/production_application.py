"""Disabled-by-default production composition with independently pinned authority.

The server supplies the exact project/control namespace and SourceAuthority.
Rehearsal receipts and caller-authored relation maps are never accepted.
"""

import json
import re
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

from fastapi import HTTPException
from pydantic import ValidationError
from starlette.responses import JSONResponse

from app.serving.control_cache import ControlCache
from app.serving.core_application import create_core_application
from app.serving.cutover import publication_token
from app.serving.metric_products import (
    MetricSelection,
    make_core_product,
    validate_core_product,
)
from app.serving.production_workers import PublicationWorkers
from app.serving.publication import Resolver, ordered
from app.serving.publication_contract import digest, timestamp


class ProductionApplication:
    production_reader = True

    def __init__(
        self,
        *,
        client,
        project,
        namespace,
        authority,
        cache_ttl=5,
        metric_product_resolver=None,
        shot_product_resolver=None,
        shot_authorize=None,
    ):
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
        self.workers = PublicationWorkers()
        self.metric_product_resolver = metric_product_resolver
        self.shot_product_resolver, self.shot_authorize = (
            shot_product_resolver,
            shot_authorize,
        )

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
                or (
                    table.expires is not None
                    and table.expires < timestamp(manifest["freshness"]["expires_at"])
                )
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

    def application(self, token, selected=False, product_id=None):
        # Fail closed before warehouse reads, but never prevent legacy startup.
        self.authority.policy("serving")
        envelope = self.release(token)
        manifest, binding = envelope["manifest"], envelope["binding"]
        product = None
        shot_product = None
        if selected == "shot_events":
            if self.shot_product_resolver is None or self.shot_authorize is None:
                raise ValueError("shot product not configured")
            shot_product = self.shot_product_resolver(envelope, product_id)
            product = make_core_product(manifest, binding)
        elif selected and self.metric_product_resolver is not None:
            product = validate_core_product(
                self.metric_product_resolver(envelope), manifest, binding
            )
        key = (
            manifest["publication_id"],
            digest(envelope),
            manifest["input_fingerprint"],
            digest(manifest["scope"]),
            digest(product),
            digest(shot_product),
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
                    run_blocking=self.workers.run,
                    metric_product=product,
                    shot_product=shot_product,
                    shot_authorize=self.shot_authorize,
                    metric_product_options=(
                        ["core_box_score"]
                        if self.metric_product_resolver is not None
                        else []
                    )
                    + (
                        ["shot_events"]
                        if self.shot_product_resolver is not None
                        else []
                    ),
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
        await self.workers.serve(self.serve_http, scope, receive, send)

    async def serve_http(self, scope, receive, send):
        try:
            token = publication_token(scope)
            selected_request = False
            selected_product_id = None
            if (
                (
                    self.metric_product_resolver is not None
                    or self.shot_product_resolver is not None
                )
                and scope.get("method") == "POST"
                and scope["path"].endswith(("/api/agent/ask", "/api/agent/ask/stream"))
            ):
                body = bytearray()
                while True:
                    event = await receive()
                    if event["type"] != "http.request":
                        raise HTTPException(400, "Ask body unavailable")
                    body.extend(event.get("body", b""))
                    if len(body) > 16384:
                        raise HTTPException(413, "Ask body too large")
                    if not event.get("more_body"):
                        break
                try:
                    payload = json.loads(body)
                except (ValueError, UnicodeError) as exc:
                    raise HTTPException(422, "invalid Ask body") from exc
                if not isinstance(payload, dict):
                    raise HTTPException(422, "invalid Ask body")
                selected_request = payload.get("metric_product") or False
                if "selection" in payload and payload["selection"] is not None:
                    try:
                        selection = MetricSelection.model_validate(payload["selection"])
                    except ValidationError as exc:
                        raise HTTPException(422, "invalid metric selection") from exc
                    pinned = selection.product.get("publication_id")
                    selected_kind = selection.product.get("product")
                    if selected_request and selected_request != selected_kind:
                        raise HTTPException(409, "conflicting metric products")
                    selected_request = selected_kind
                    selected_product_id = selection.product.get("product_id")
                    if not pinned or (token is not None and pinned != token):
                        raise HTTPException(409, "conflicting metric selection")
                    token = pinned
                original_receive = receive
                delivered = False

                async def receive():
                    nonlocal delivered
                    if not delivered:
                        delivered = True
                        return {
                            "type": "http.request",
                            "body": bytes(body),
                            "more_body": False,
                        }
                    return await original_receive()

            app = (
                await self.workers.run(
                    self.application, token, selected_request, selected_product_id
                )
                if selected_request
                else await self.workers.run(self.application, token)
            )
        except HTTPException as exc:
            return await JSONResponse(
                {"detail": exc.detail}, status_code=exc.status_code
            )(scope, receive, send)
        except Exception:
            return await JSONResponse(
                {"detail": "publication unavailable; refresh the flow"}, status_code=503
            )(scope, receive, send)
        await app(scope, receive, send)
