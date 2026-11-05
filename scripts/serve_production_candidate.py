"""Explicit production composition; importing main.app keeps the legacy default.

Operator config and independently controlled authority files must be mounted
read-only. No .env files, implicit target, or automatic scheduler activation.
"""

import argparse
import json
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["PYTHON_DOTENV_DISABLED"] = "1"


def compose_legacy(app, config):
    """Preserve the existing, loopback-restricted Jobs monitor when configured."""
    if "jobs_state_dir" not in config:
        return app
    from app.jobs import create_jobs_app
    from fastapi import FastAPI

    @asynccontextmanager
    async def legacy_lifespan(_):
        async with app.router.lifespan_context(app):
            yield

    combined = FastAPI(lifespan=legacy_lifespan)
    combined.mount(
        "/jobs-monitor",
        create_jobs_app(
            config["jobs_state_dir"], snapshot_root=config.get("jobs_snapshot_root")
        ),
    )
    app.state.jobs_monitor_enabled = True
    combined.mount("/", app)
    return combined


def attach_publication(app, core, mode):
    """Expose verified publication data alongside the existing app's own data."""
    from app.serving.production_workers import PublicationWorkers
    from starlette.responses import JSONResponse

    workers = PublicationWorkers()

    async def publication(scope, receive, send):
        return await workers.serve(serve, scope, receive, send)

    async def serve(scope, receive, send):
        try:
            current_mode = await workers.run(mode)
        except (OSError, ValueError, KeyError, TypeError):
            current_mode = None
        if current_mode == "legacy":
            return await JSONResponse(
                {"detail": "production publication disabled"}, status_code=409
            )(scope, receive, send)
        if current_mode != "production":
            return await JSONResponse(
                {"detail": "production publication unavailable"}, status_code=503
            )(scope, receive, send)
        return await core(scope, receive, send)

    app.mount("/publication-core", publication)
    app.state.production_publication_enabled = True


def create(config_path):
    return create_config(json.loads(Path(config_path).read_text()))


def create_config(config):
    from app.main import app as legacy
    from app.serving.production_application import ProductionApplication
    from app.serving.source_authority import ProviderAuthorities, SourceAuthority
    from google.cloud import bigquery

    def clock():
        return datetime.now(timezone.utc).isoformat()

    if "source_authorities" in config:
        authorities = {
            provider: SourceAuthority(
                policy_path=entry["policy_path"],
                policy_digest=entry["policy_digest"],
                registry_path=entry["registry_path"],
                clock=clock,
                cache_ttl=30,
            )
            for provider, entry in config["source_authorities"].items()
        }
        authority = ProviderAuthorities(authorities, clock=clock)
    else:
        authority = SourceAuthority(
            policy_path=config["policy_path"],
            policy_digest=config["policy_digest"],
            registry_path=config["registry_path"],
            clock=clock,
            cache_ttl=30,
        )
    core = ProductionApplication(
        client=bigquery.Client(project=config["project"]),
        project=config["project"],
        namespace=config["namespace"],
        authority=authority,
    )
    # Reuse the generation-pinned, bounded GCS control reader used for source
    # controls; local paths remain supported for explicitly local operation.
    mode_reader = SourceAuthority(
        policy_path=config["mode_path"],
        policy_digest="",
        registry_path=config["mode_path"],
        clock=clock,
        cache_ttl=30,
    )
    attach_publication(
        legacy, core, lambda: mode_reader.read_document(mode_reader.policy_path)["mode"]
    )
    return compose_legacy(legacy, config)


if __name__ == "__main__":
    import uvicorn

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", required=True, type=Path)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8036)
    a = p.parse_args()
    uvicorn.run(create(a.config), host=a.host, port=a.port, proxy_headers=False)
