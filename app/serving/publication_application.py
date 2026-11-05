"""Opt-in loopback ASGI composition for the isolated warehouse protocol."""

import threading
from collections import OrderedDict

from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.serving.core_application import create_core_application
from app.serving.cutover import publication_token
from app.serving.publication_contract import digest


class PublicationApplication:
    def __init__(self, resolver):
        self.resolver = resolver
        self.apps = OrderedDict()
        self.lock = threading.Lock()

    def application(self, token):
        release = self.resolver.resolve(token)
        with self.lock:
            if release.cache_key not in self.apps:
                models = {n: release.asset(n) for n in release.receipt["assets"]}
                accepted = dict(
                    status="accepted",
                    authority_scope="isolated_warehouse_rehearsal",
                    runtime_eligible=False,
                    scope=release.receipt["scope"],
                    games=models["accepted_game_results"],
                    appearances=models["accepted_player_appearances"],
                    participants=models["participant_manifest"],
                    players=models["dim_player"],
                    teams=models["dim_team"],
                )
                bundle = dict(
                    manifest=release.receipt,
                    binding={},
                    models=models,
                    accepted=accepted,
                )

                def authorize(*_):
                    release.verify_assets()
                    return True

                self.apps[release.cache_key] = create_core_application(
                    bundle,
                    expected_digest=digest(bundle),
                    clock=self.resolver.clock,
                    authorize=authorize,
                    warehouse_context=dict(
                        project=self.resolver.project, namespace=self.resolver.namespace
                    ),
                )
                while len(self.apps) > 2:
                    self.apps.popitem(last=False)
            self.apps.move_to_end(release.cache_key)
            return self.apps[release.cache_key]

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                event = await receive()
                if event["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif event["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            return
        if not scope.get("client") or scope["client"][0] not in {"127.0.0.1", "::1"}:
            return await JSONResponse(
                {"detail": "isolated loopback rehearsal only"}, status_code=403
            )(scope, receive, send)
        try:
            app = await run_in_threadpool(self.application, publication_token(scope))
        except (ValueError, KeyError, UnicodeError):
            return await JSONResponse(
                {"detail": "release unavailable; refresh the whole flow"},
                status_code=503,
            )(scope, receive, send)
        await app(scope, receive, send)
