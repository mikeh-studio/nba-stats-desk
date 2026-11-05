"""Opt-in local cutover rehearsal; never changes the default application.

The caller retains both apps. New requests follow the selected mode; a pinned
core flow stays on its validated release even after rollback to the old app.
"""

import html
import json
import re
from urllib.parse import parse_qs

from starlette.concurrency import run_in_threadpool
from starlette.responses import HTMLResponse, JSONResponse


def publication_token(scope):
    values = parse_qs(
        scope.get("query_string", b"").decode(), keep_blank_values=True
    ).get("publication_id", [])
    raw = dict(scope.get("headers", [])).get(b"x-publication-id")
    header = raw.decode() if raw else None
    if len(values) > 1 or (values and header and values[0] != header):
        raise ValueError("conflicting release tokens")
    token = header or (values[0] if values else None)
    if token is not None and not re.fullmatch(r"pub_[0-9a-f]{16}", token):
        raise ValueError("invalid release token")
    return token


def public_job(record):
    """Closed projection: no paths, object names, error bodies or evidence links."""
    if record is None:
        return {
            "version": "publication-job-status/1",
            "stages": {
                name: "unavailable"
                for name in ("collector", "candidate", "publication")
            },
            "production_active": False,
        }
    allowed = {"retained", "succeeded", "held", "failed", "pending", "running"}
    grouped = {"collector": "pending", "candidate": "pending", "publication": "pending"}
    for stage in record.get("stages", []):
        name, state = stage.get("name"), stage.get("status")
        if state not in allowed:
            raise ValueError("unknown job state")
        if name == "collection":
            grouped["collector"] = state
        elif name in {"restore", "selection_export"} and state == "failed":
            grouped["candidate"] = "failed"
        elif name == "models":
            grouped["candidate"] = state
        elif name == "production_publication":
            grouped["publication"] = state
    if record.get("status") == "failed" and grouped["candidate"] == "pending":
        grouped["candidate"] = "failed"
        grouped["publication"] = "held"
    return {
        "version": "publication-job-status/1",
        "stages": grouped,
        "production_active": False,
    }


class CutoverApplication:
    def __init__(self, *, legacy, core, mode, job=lambda: None, legacy_routes=()):
        self.legacy, self.core, self.mode, self.job = legacy, core, mode, job
        self.legacy_routes = frozenset((r["season"], r["phase"]) for r in legacy_routes)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.legacy(scope, receive, send)
        # Candidate switching and private status are local preparation features.
        if not getattr(self.core, "production_reader", False) and (
            not scope.get("client") or scope["client"][0] not in {"127.0.0.1", "::1"}
        ):
            return await JSONResponse(
                {"detail": "local cutover rehearsal only"}, status_code=403
            )(scope, receive, send)
        try:
            mode = self.mode()
            if mode not in {"legacy", "core_rehearsal", "production"}:
                raise ValueError("unreviewed reader mode")
            if mode == "production" and not getattr(
                self.core, "production_reader", False
            ):
                raise ValueError("production reader authority required")
            token = publication_token(scope)
            if scope["path"] in {"/api/publication/jobs", "/publication-jobs"}:
                projection = public_job(self.job())
                if mode == "production":
                    current = await run_in_threadpool(self.core.release, None)
                    projection.update(
                        production_active=True,
                        publication_id=current["manifest"]["publication_id"],
                    )
                if scope["path"].startswith("/api/"):
                    response = JSONResponse(projection)
                else:
                    rows = "".join(
                        f"<tr><th>{k.title()}</th><td>{v}</td></tr>"
                        for k, v in projection["stages"].items()
                    )
                    response = HTMLResponse(
                        '<!doctype html><meta name="viewport" content="width=device-width, initial-scale=1"><title>Publication jobs</title><h1>Publication jobs</h1><p>'
                        + (
                            "Production active"
                            if projection["production_active"]
                            else "Cutover rehearsal · Production inactive"
                        )
                        + "</p><table>"
                        + rows
                        + "</table><p>Collector completion does not mean the app has refreshed.</p><pre>"
                        + html.escape(json.dumps(projection, indent=2))
                        + "</pre>"
                    )
                response.headers["Cache-Control"] = "no-store"
                return await response(scope, receive, send)
            # Operational pages and their assets remain owned by the existing app.
            # They must remain reachable even when the core publication is held.
            path = scope["path"]
            if path in {"/jobs", "/api/jobs", "/jobs-monitor"} or path.startswith(
                ("/api/jobs/", "/static/", "/jobs-monitor/")
            ):
                return await self.legacy(scope, receive, send)
            query = parse_qs(scope.get("query_string", b"").decode())
            if not token and scope["path"] == "/api/agent/history":
                return await self.legacy(scope, receive, send)
            if (
                not token
                and len(query.get("season", [])) == 1
                and len(query.get("phase", ["Regular Season"])) == 1
                and (query["season"][0], query.get("phase", ["Regular Season"])[0])
                in self.legacy_routes
            ):
                return await self.legacy(scope, receive, send)
            historical = query.get("season") == ["2025-26"]
            if getattr(self.core, "production_reader", False) and (
                mode == "production" or token
            ):
                envelope = await run_in_threadpool(self.core.release, token)
                active_scope = envelope["manifest"]["scope"]
                season = query.get("season", [active_scope["season"]])
                phase = query.get("phase", [active_scope["phase"]])
                if len(season) != 1 or len(phase) != 1:
                    raise ValueError("ambiguous scope")
                route = next(
                    (
                        r
                        for r in envelope["binding"]["routes"]
                        if (r["season"], r["phase"]) == (season[0], phase[0])
                    ),
                    None,
                )
                if (
                    route is None
                    or route["mode"] == "unsupported"
                    or (token and route["mode"] != "release")
                ):
                    return await JSONResponse(
                        {"detail": "scope unavailable for this publication"},
                        status_code=422,
                    )(scope, receive, send)
                historical = route["mode"] == "legacy"
            legacy_session = scope["path"] == "/api/agent/history"
            selected = (
                self.core
                if token
                or (
                    mode in {"core_rehearsal", "production"}
                    and not historical
                    and not legacy_session
                )
                else self.legacy
            )
        except (ValueError, KeyError, UnicodeError, OSError):
            return await JSONResponse(
                {"detail": "reader configuration unavailable"}, status_code=503
            )(scope, receive, send)
        if (
            selected is self.core
            and getattr(self.core, "production_reader", False)
            and not token
        ):
            # Bind the routing decision to the same release before the app reads it.
            scope = {
                **scope,
                "headers": [
                    *scope.get("headers", []),
                    (
                        b"x-publication-id",
                        envelope["manifest"]["publication_id"].encode(),
                    ),
                ],
            }
        return await selected(scope, receive, send)
