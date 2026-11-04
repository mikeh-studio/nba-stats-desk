"""Dedicated, explicit process composition of the real application routes.

This local candidate never grants live authority and is never selected by an
HTTP request. Instantiate only in a fresh process before application lifespan.
"""

from __future__ import annotations

import json
import os
import re

from starlette.requests import Request
from starlette.responses import JSONResponse


class _CandidateBoundary:
    def __init__(self, app, *, jobs_enabled=False):
        self.app = app
        self.jobs_enabled = jobs_enabled

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            from app.security import is_direct_local

            request = Request(scope)
            path, method = scope["path"], scope["method"]
            allowed = (
                method == "GET"
                and (
                    path in {"/ask", "/compare", "/api/compare"}
                    or (
                        self.jobs_enabled
                        and (
                            path == "/jobs-monitor" or path.startswith("/jobs-monitor/")
                        )
                    )
                    or re.fullmatch(r"/players/[1-9][0-9]*(?:/content)?", path)
                    or re.fullmatch(r"/api/players/[1-9][0-9]*", path)
                )
            ) or (
                method == "POST" and path in {"/api/agent/ask", "/api/agent/ask/stream"}
            )
            if not allowed or not is_direct_local(request):
                await JSONResponse(
                    {"detail": "Operation is outside the offline candidate."},
                    status_code=403,
                    headers={"Cache-Control": "no-store"},
                )(scope, receive, send)
                return
            if method == "POST":
                chunks = []
                total = 0
                for _ in range(1024):
                    message = await receive()
                    if message["type"] != "http.request":
                        return
                    chunk = message.get("body", b"")
                    total += len(chunk)
                    if total > 32768:
                        await JSONResponse(
                            {"detail": "Request too large."}, status_code=413
                        )(scope, receive, send)
                        return
                    chunks.append(chunk)
                    if not message.get("more_body", False):
                        break
                else:
                    await JSONResponse(
                        {"detail": "Request too large."}, status_code=413
                    )(scope, receive, send)
                    return
                body = b"".join(chunks)

                def unique(pairs):
                    result = {}
                    for key, value in pairs:
                        if key in result:
                            raise ValueError("duplicate key")
                        result[key] = value
                    return result

                try:
                    parsed = json.loads(
                        body,
                        object_pairs_hook=unique,
                        parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
                    )
                    if (
                        type(parsed) is not dict
                        or not set(parsed) <= {"question", "conversation_id"}
                        or type(parsed.get("question")) is not str
                    ):
                        raise ValueError("unsupported body")
                except (ValueError, UnicodeDecodeError, RecursionError):
                    await JSONResponse(
                        {"detail": "Invalid candidate request."}, status_code=400
                    )(scope, receive, send)
                    return
                sent = False
                original_receive = receive

                async def replay():
                    nonlocal sent
                    if not sent:
                        sent = True
                        return {
                            "type": "http.request",
                            "body": body,
                            "more_body": False,
                        }
                    return await original_receive()

                receive = replay
            scope = dict(scope)
            scope["offline_pts_candidate"] = True
        await self.app(scope, receive, send)


def create_candidate_application(
    service, *, settings, jobs_app=None, service_provider=None
):
    """Compose one trusted candidate into the real app in a dedicated process."""
    if os.environ.get("PYTHON_DOTENV_DISABLED") != "1":
        raise ValueError("dotenv must be disabled before importing the application")
    from app import main
    from app.agent.runtime_policy import get_runtime_policy
    from app.config import Settings
    from app.serving.candidate_service import CandidatePTSService

    if (
        type(service) is not CandidatePTSService
        or (service_provider is not None and not callable(service_provider))
        or type(settings) is not Settings
        or settings.ask_intelligence_provider != "reference"
        or settings.ask_local_espn_dev
        or settings.agent_history_enabled
        or settings.agent_rate_limit_redis_url
        or settings.agent_trace_enabled
        or settings.performance_cache_prewarm_enabled
        or settings.openai_api_key
        or settings.anthropic_api_key
        or settings.openrouter_api_key
        or get_runtime_policy() is not None
        or main.app.middleware_stack is not None
        or main.app.dependency_overrides
        or getattr(main.app.state, "offline_candidate_claimed", False)
    ):
        raise ValueError("fresh offline candidate process required")
    service.check()
    original_get_settings = main.get_settings
    main.app.dependency_overrides[original_get_settings] = lambda: settings
    main.app.dependency_overrides[main.get_candidate_service] = (
        service_provider if service_provider is not None else lambda: service
    )
    main.app.dependency_overrides[main.get_repository] = lambda: None
    # Middleware uses this module-global getter directly; keep that lookup on
    # the explicitly supplied nonsecret settings in this dedicated process.
    main.get_settings = lambda: settings
    main.app.state.offline_candidate_claimed = True
    if jobs_app is not None:
        main.app.mount("/jobs-monitor", jobs_app)
        main.app.state.jobs_monitor_enabled = True
    return _CandidateBoundary(main.app, jobs_enabled=jobs_app is not None)
