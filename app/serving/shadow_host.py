"""Explicit isolated PTS HTTP host; never imported by the production factory.

The caller constructs an independently authorized selection. This factory does
not discover data, grant source permission or load a warehouse/provider fallback.
"""

from __future__ import annotations

import json

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.agent.snapshot_source import CurrentPTSAskShadow, HistoricalPTSAskShadow
from app.serving.comparison import (
    CurrentPTSComparisonShadow,
    HistoricalPTSComparisonShadow,
    PTSComparisonRequest,
)
from app.serving.composite import TwoSeasonPTS, bounded_json
from app.serving.context import PTSShadowRequest
from app.serving.contract import ServingError
from app.serving.player import CurrentPlayerPTSShadow, PlayerPTSShadow

MAX_REQUEST_BYTES = 2048
SEASONS = ("2025-26", "2026-27", "both")
HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


def _require(ok, code="invalid_request"):
    if not ok:
        raise ServingError(code)


def _request(data, *, comparison=False):
    _require(
        type(data) is dict and set(data) == {"season", "player_ids", "aggregation"}
    )
    _require(type(data["season"]) is str and data["season"] in SEASONS)
    _require(
        type(data["aggregation"]) is str and data["aggregation"] in ("total", "average")
    )
    players = data["player_ids"]
    _require(type(players) is list and len(players) == (2 if comparison else 1))
    _require(all(type(p) is int and 0 < p <= 2147483647 for p in players))
    _require(len(set(players)) == len(players))
    return data["season"], tuple(players), data["aggregation"]


async def _body(request):
    _require(
        request.headers.get("content-type", "").split(";")[0] == "application/json"
    )
    body = bytearray()
    async for part in request.stream():
        _require(len(body) + len(part) <= MAX_REQUEST_BYTES, "request_too_large")
        body.extend(part)

    def pairs(items):
        result = {}
        for key, value in items:
            _require(key not in result)
            result[key] = value
        return result

    try:
        return json.loads(
            body, object_pairs_hook=pairs, parse_constant=lambda _: _require(False)
        )
    except (ValueError, RecursionError) as exc:
        raise ServingError("invalid_request") from exc


def create_shadow_app(selection: TwoSeasonPTS, *, evidence_kind: str) -> FastAPI:
    """Construct only a dedicated validation app, with explicit evidence labeling.

    Bind its server to loopback with proxy headers disabled. The normal app does
    not import this module. Both evidence labels deliberately deny certification.
    """
    _require(type(selection) is TwoSeasonPTS, "explicit_selection_required")
    _require(
        evidence_kind in ("synthetic", "retained-unapproved"), "evidence_label_required"
    )
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]"]
    )

    @app.middleware("http")
    async def same_origin(request: Request, call_next):
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            return Response(status_code=403, headers=HEADERS)
        return await call_next(request)

    def answer(season, players, mode, consumer):
        _require(
            set(players) <= selection._authority.approved_player_ids,
            "identity_compatibility_denied",
        )
        if season == "both":
            data = selection.answer_json(players, mode, consumer=consumer)
            return data
        index = SEASONS.index(season)
        context = selection._contexts[index]
        scope = context.binding["scope"]
        if len(players) == 2:
            adapter = (HistoricalPTSComparisonShadow, CurrentPTSComparisonShadow)[
                index
            ](context)
            payload = adapter.answer(
                PTSComparisonRequest.create(players, mode, scope=scope)
            )
        else:
            request = PTSShadowRequest.create(players[0], mode, scope=scope)
            if consumer == "player":
                payload = (PlayerPTSShadow, CurrentPlayerPTSShadow)[index](
                    context
                ).get_pts(request)
            else:
                payload = (HistoricalPTSAskShadow, CurrentPTSAskShadow)[index](
                    context
                ).answer(request)
        data = bounded_json(payload)
        context.check_emission(payload)
        return data

    async def respond(request, *, consumer="ask", comparison=False, stream=False):
        try:
            season, players, mode = _request(
                await _body(request), comparison=comparison
            )
            data = answer(season, players, mode, consumer)
            # One terminal event is fully validated before returning HTTP 200.
            # There is no partial prose, late exception or fallback success.
            if stream:
                data = b"event: final\ndata: " + data + b"\n\n"
            return Response(
                data,
                media_type="text/event-stream" if stream else "application/json",
                headers=HEADERS,
            )
        except ServingError as exc:
            code = exc.code
            status = (
                400
                if code == "invalid_request"
                else 413
                if code == "request_too_large"
                else 503
            )
            return Response(
                bounded_json(
                    {"status": "unavailable", "reason": code, "runtime_eligible": False}
                ),
                status_code=status,
                media_type="application/json",
                headers=HEADERS,
            )

    @app.post("/api/ask")
    async def ask(request: Request):
        return await respond(request)

    @app.post("/api/ask/stream")
    async def stream(request: Request):
        return await respond(request, stream=True)

    @app.post("/api/player")
    async def player(request: Request):
        return await respond(request, consumer="player")

    @app.post("/api/comparison")
    async def comparison(request: Request):
        return await respond(request, comparison=True)

    @app.get("/ask", response_class=HTMLResponse)
    async def page():
        return HTMLResponse(
            _PAGE.replace("EVIDENCE_KIND", evidence_kind), headers=HEADERS
        )

    return app


_PAGE = """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Shared Serving shadow validation</title>
<style>body{font:16px system-ui;margin:2rem auto;max-width:68rem;padding:0 1rem;background:#101418;color:#eef2f4}
h1{font-size:1.5rem}h2{font-size:1.1rem}label{display:inline-block;margin:.5rem}input,select,button{font:inherit;padding:.4rem}
table{border-collapse:collapse;width:100%;margin:1rem 0}td,th{padding:.6rem;text-align:left;border-bottom:1px solid #52606b}
aside{padding:1rem;border:1px solid #52606b}#status{min-height:1.5em}
dl{display:grid;grid-template-columns:minmax(8rem,auto) 1fr;gap:.25rem 1rem;font-size:.875rem}dt{color:#aebbc5}dd{margin:0;overflow-wrap:anywhere}.limitations{font-size:.875rem}</style>
<h1>Shared Serving shadow validation</h1>
<aside>EVIDENCE_KIND evidence · Offline candidate · Runtime not enabled · No source certification</aside>
<p>Structured PTS validation; observed appearances only. Rankings, pooled seasons and natural-language planning are unavailable.</p>
<form id="query"><label>View <select id="view"><option value="ask">Ask</option><option value="ask/stream">Ask SSE</option><option value="player">Player</option><option value="comparison">Comparison</option></select></label>
<label>Season <select id="season"><option>2025-26</option><option>2026-27</option><option value="both">Both, side by side</option></select></label>
<label>Player IDs <input id="players" value="1" aria-label="Player IDs"></label>
<label>PTS <select id="mode"><option value="average">Per valid appearance</option><option value="total">Total</option></select></label>
<button>Validate</button></form><p id="status" role="status"></p><main id="results"></main>
<script>
const get=id=>document.getElementById(id);
function node(tag,text){const e=document.createElement(tag);e.textContent=String(text ?? 'Unavailable');return e;}
function renderQualifiers(member,section){const scope=member.binding.scope,evidence=member.semantic_evidence,clocks=evidence.evidence_clocks||(evidence.sides&&evidence.sides[0].evidence_clocks)||{},authority=member.authority;
const fields=[['Phase',scope.phase],['Start date',scope.start_date],['End date',scope.end_date],['Evidence as of',scope.as_of],['Evidence cutoff',scope.cutoff],['Freshness checked',authority.freshness_checked_at],['Authority checked',authority.status_checked_at],['Source captured',clocks.source_captured_at],['Discovered',clocks.discovered_at],['Revalidated',clocks.revalidated_at],['Reviewed',clocks.reviewed_at],['Hard expiry',clocks.hard_expiry]];
const metadata=document.createElement('dl');fields.forEach(([label,value])=>{metadata.append(node('dt',label),node('dd',value??'Not recorded'));});section.append(metadata);
section.append(node('h3','Limitations'));const limits=document.createElement('ul');limits.className='limitations';member.limitations.forEach(value=>limits.append(node('li',value)));section.append(limits);}
function renderMember(member){const s=document.createElement('section');s.append(node('h2',member.binding.scope.season));renderQualifiers(member,s);
let columns,rows;if(member.player_profile){const p=member.player_profile;columns=['Player ID','PTS total','PTS per valid appearance','Observed appearances','Valid PTS appearances','Missing PTS appearances'];rows=[[p.player_id,p.total_pts,p.average_pts,p.observed_games,p.valid_games,p.missing_component_games]];}
else{columns=member.tables[0].columns.map(x=>x.label);rows=member.tables[0].rows;}
const table=document.createElement('table'),head=document.createElement('tr');columns.forEach(x=>head.append(node('th',x)));table.append(head);rows.forEach(row=>{const tr=document.createElement('tr');row.forEach(x=>tr.append(node('td',x)));table.append(tr);});s.append(table);get('results').append(s);}
get('query').onsubmit=async event=>{event.preventDefault();get('results').replaceChildren();get('status').textContent='Validating…';try{
const response=await fetch('/api/'+get('view').value,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({season:get('season').value,player_ids:get('players').value.split(',').map(x=>Number(x.trim())),aggregation:get('mode').value})});
const text=await response.text(),payload=JSON.parse(text.startsWith('event: final')?text.split('data: ')[1]:text);
if(!response.ok){get('status').textContent='Unavailable: '+payload.reason;return;}
(payload.members||[payload]).forEach(renderMember);get('status').textContent='Validated candidate response · Runtime not enabled';
}catch(_){get('results').replaceChildren();get('status').textContent='Unavailable: transport error';}};
</script></html>"""
