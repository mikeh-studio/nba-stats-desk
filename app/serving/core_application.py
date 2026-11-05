"""Isolated loopback core-stat app with pinned offline or warehouse evidence."""

from __future__ import annotations

import copy
import json
import re
from datetime import date
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel, ConfigDict, Field
from scripts.validate_publication_contract import (
    PROFILE,
    digest,
    public_status,
    timestamp,
    validate_binding,
)
from starlette.concurrency import run_in_threadpool
from starlette.routing import Match

from app.agent.semantics import Evidence, Query, SemanticError, run_query
from app.serving.core_models import build_models, metrics
from app.serving.cutover import publication_token
from app.serving.publication import equal_values, ordered, validate_receipt


class Ask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=500)


class StatQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    player_id: int = Field(gt=0)
    metric: str
    aggregation: str
    operation: str = "summary"
    first: date | None = None
    last: date | None = None


def create_core_application(
    bundle,
    *,
    expected_digest,
    clock,
    authorize,
    warehouse_context=None,
    production_authority=None,
):
    """Caller supplies pinned offline bundle, trusted replay clock and authority.

    `authorize` must recheck its local trust/revocation state each request. Static
    manifest validity is never used as a replacement for that callback.
    """
    if digest(bundle) != expected_digest:
        raise ValueError("bundle integrity differs")
    bundle = copy.deepcopy(bundle)
    manifest, binding, models = bundle["manifest"], bundle["binding"], bundle["models"]
    if warehouse_context is not None and production_authority is not None:
        raise ValueError("rehearsal cannot be promoted by a reader flag")
    warehouse_receipt = None
    if warehouse_context is None:
        validate_binding(binding, manifest)
        if manifest["purpose"] != "offline_replay":
            if (
                production_authority is None
                or production_authority.check(manifest, binding) is not True
            ):
                raise ValueError("production authority required")
    else:
        warehouse_receipt = copy.deepcopy(manifest)
        validate_receipt(warehouse_receipt, **warehouse_context, now=clock())
        manifest = {
            **manifest,
            "profile": "core_stats_v1",
            "evidence_kind": "retained",
            "data_through": manifest["scope"]["last"],
            "capabilities": list(PROFILE["capabilities"]),
            "materialized_at": manifest["validation_as_of"],
            "assets": [dict(name=n, **v) for n, v in manifest["assets"].items()],
        }
        binding = {"binding_id": manifest["publication_id"]}

    scope = manifest["scope"]
    first_day, last_day = (
        date.fromisoformat(scope["first"]),
        date.fromisoformat(scope["last"]),
    )
    rolling = manifest.get("version") == "publication-manifest/2"

    def authorized():
        return authorize(binding["binding_id"], expected_digest)

    def status():
        if warehouse_receipt is None:
            return public_status(manifest, clock())
        validate_receipt(warehouse_receipt, **warehouse_context, now=clock())
        return {
            **{
                k: manifest[k]
                for k in (
                    "publication_id",
                    "profile",
                    "data_through",
                    "capabilities",
                    "scope",
                    "evidence_kind",
                    "purpose",
                )
            },
            "freshness": "fresh",
            "runtime_eligible": False,
        }

    for asset in manifest["assets"]:
        if (
            digest(models[asset["name"]])
            if warehouse_receipt is None and production_authority is None
            else digest(ordered(asset["name"], models[asset["name"]]))
        ) != asset["digest"] or len(models[asset["name"]]) != asset["rows"]:
            raise ValueError("model content differs")
    # Recompute, rather than trusting a bundle whose fields merely look valid.
    rebuilt = build_models(
        bundle["accepted"],
        materialized_at=manifest["materialized_at"],
        release_scope=scope if rolling else None,
    )
    if (
        rebuilt != models
        if warehouse_receipt is None and production_authority is None
        else any(
            not equal_values(ordered(n, rebuilt[n]), ordered(n, models[n]))
            for n in rebuilt
        )
    ):
        raise ValueError("core model arithmetic differs")
    if warehouse_receipt is None:
        selected = {g["game_id"]: g for g in manifest["selected_games"]}
        games = {g["game_id"]: g for g in models["accepted_game_results"]}
        if set(selected) != set(games):
            raise ValueError("manifest/model game identities differ")
        for gid, game in games.items():
            record = selected[gid]
            appearances = [
                a for a in models["accepted_player_appearances"] if a["game_id"] == gid
            ]
            participants = [
                a for a in models["participant_manifest"] if a["game_id"] == gid
            ]
            if (
                record["game_uid"] != game["game_uid"]
                or record["result_revision"] != game["result_revision"]
                or record["played"] != len(appearances)
                or record["participants"] != len(participants)
                or any(
                    a["observation_id"] != record["observation_id"]
                    or a["fact_revision"] != record["fact_revision"]
                    or timestamp(a["source_captured_at"])
                    != timestamp(record["captured_at"])
                    or timestamp(a["source_verified_at"])
                    != timestamp(record["last_verified_at"])
                    for a in appearances
                )
            ):
                raise ValueError("manifest/model selected revision differs")
        for field in manifest["fields"]:
            missing = sum(
                row[field["name"]] is None for row in models["fct_player_game_stats"]
            )
            if missing != field["missing_count"]:
                raise ValueError("manifest/model missingness differs")
    app = FastAPI(
        title="NBA core stats"
        if production_authority is not None
        else "Core stats offline replay"
    )
    facts = models["fct_player_game_stats"]
    identities = {p["player_id"]: p for p in models["core_player_search"]}
    evidence = Evidence(
        rows=facts,
        covered_scopes=frozenset({(scope["season"], scope["phase"])}),
        source="validated weekly publication"
        if production_authority is not None
        else "pinned offline core bundle",
        snapshot_id=manifest["publication_id"],
        data_through={(scope["season"], scope["phase"]): scope["last"]},
        complete=True,
        cohort_complete=False,
    )
    evidence.validate()

    @app.middleware("http")
    async def gate(request, call_next):
        from fastapi.responses import JSONResponse

        if production_authority is None and (
            request.client is None
            or request.client.host not in {"127.0.0.1", "::1"}
            or request.url.hostname not in {"127.0.0.1", "localhost", "::1"}
            or request.headers.get("forwarded")
            or request.headers.get("x-forwarded-for")
        ):
            return JSONResponse(
                {"detail": "loopback offline replay only"}, status_code=403
            )
        try:
            if production_authority is not None:
                await run_in_threadpool(production_authority.check, manifest, binding)
            if await run_in_threadpool(authorized) is not True:
                raise ValueError("offline authority unavailable")
            current_status = status()
            if current_status["freshness"] in {"expired", "unavailable"}:
                raise ValueError("replay source expired")
            try:
                token = publication_token(request.scope)
            except ValueError:
                return JSONResponse(
                    {"detail": "invalid publication token"}, status_code=409
                )
            if token and token != manifest["publication_id"]:
                return JSONResponse(
                    {"detail": "publication changed; refresh the whole flow"},
                    status_code=409,
                )
            if (
                request.query_params.get("season", scope["season"]) != scope["season"]
                or request.query_params.get("phase", scope["phase"]) != scope["phase"]
            ):
                return JSONResponse(
                    {"detail": "scope unavailable in core replay"}, status_code=422
                )
            # Middleware runs before routing; match the declared route without
            # invoking its handler so unsupported filters cannot trigger work.
            route = next(
                (
                    r
                    for r in app.router.routes
                    if r.matches(request.scope)[0] == Match.FULL
                ),
                None,
            )
            query_fields = {
                p.name
                for p in getattr(getattr(route, "dependant", None), "query_params", [])
            }
            if (
                set(request.query_params)
                - query_fields
                - {"season", "phase", "publication_id"}
            ):
                return JSONResponse(
                    {
                        "detail": "unsupported query parameter; filters are never silently ignored"
                    },
                    status_code=422,
                )
            response = await call_next(request)
            # Recheck immediately before emission, including request-time revocation.
            if production_authority is not None:
                await run_in_threadpool(production_authority.check, manifest, binding)
            if (
                await run_in_threadpool(authorized) is not True
                or status()["freshness"] == "expired"
            ):
                raise ValueError("authority expired before emission")
            response.headers["X-Publication-ID"] = manifest["publication_id"]
            response.headers["Cache-Control"] = "no-store"
            return response
        except ValueError:
            return JSONResponse(
                {"detail": "offline authority or freshness unavailable"},
                status_code=503,
            )

    def wrap(data):
        return dict(
            status=status(),
            publication_id=manifest["publication_id"],
            profile=manifest["profile"],
            scope=manifest["scope"],
            evidence_kind=manifest["evidence_kind"],
            replay_as_of=clock(),
            data=data,
        )

    def player(pid):
        if pid not in identities:
            raise HTTPException(404, "player not in this release")
        return wrap(
            dict(
                identity=identities[pid],
                summary=next(
                    p for p in models["core_player_summary"] if p["player_id"] == pid
                ),
                windows=[
                    p for p in models["core_player_windows"] if p["player_id"] == pid
                ],
                trends=[
                    p for p in models["core_player_trends"] if p["player_id"] == pid
                ],
                game_log=sorted(
                    [p for p in facts if p["player_id"] == pid],
                    key=lambda p: (p["game_date"], p["game_id"]),
                    reverse=True,
                ),
            )
        )

    @app.get("/api/health")
    def health():
        return status()

    @app.get("/api/players/search")
    def search(q: str = ""):
        if len(q) > 100:
            raise HTTPException(422, "search too long")
        return wrap(
            [
                p
                for p in identities.values()
                if q.casefold() in p["player_name"].casefold()
            ][:25]
        )

    @app.get("/api/players/{player_id}")
    def detail(player_id: int):
        return player(player_id)

    @app.get("/api/players/{player_id}/game-log")
    def log(player_id: int):
        return wrap(player(player_id)["data"]["game_log"])

    def compare(player_a_id, player_b_id, window, first=None, last=None):
        player(player_a_id)
        player(player_b_id)
        if window not in PROFILE["windows"]:
            raise HTTPException(422, "unsupported window")
        rows = copy.deepcopy(
            [
                next(
                    w
                    for w in models["core_player_windows"]
                    if w["player_id"] == p and w["window_key"] == window
                )
                for p in [player_a_id, player_b_id]
            ]
        )
        if first is not None or last is not None:
            if (
                window != "scope"
                or first is None
                or last is None
                or not first_day <= first <= last <= last_day
            ):
                raise HTTPException(
                    422, "provide a bounded date range or a named window"
                )
            rows = []
            for pid in [player_a_id, player_b_id]:
                selected = [
                    r
                    for r in facts
                    if r["player_id"] == pid
                    and str(first) <= r["game_date"] <= str(last)
                ]
                rows.append(
                    dict(
                        player_id=pid,
                        window_key="date_range",
                        first_game_date=str(first),
                        last_game_date=str(last),
                        observed_games=len(selected),
                        metrics=metrics(selected),
                    )
                )
        differences = []
        for a, b in zip(rows[0]["metrics"], rows[1]["metrics"]):
            complete = (
                a["value"] is not None
                and b["value"] is not None
                and not a["missing_games"]
                and not b["missing_games"]
            )
            differences.append(
                dict(
                    metric=a["metric"],
                    aggregation=a["aggregation"],
                    difference=(a["value"] - b["value"])
                    * (100 if a["unit"] == "ratio" else 1)
                    if complete
                    else None,
                    unit="percentage_points" if a["unit"] == "ratio" else a["unit"],
                    unavailable_reason=None
                    if complete
                    else "incomplete_or_zero_denominator",
                )
            )
        distributions = []
        for pid in [player_a_id, player_b_id]:
            selected_rows = sorted(
                [r for r in facts if r["player_id"] == pid],
                key=lambda r: (r["game_date"], r["game_id"]),
                reverse=True,
            )
            if first is not None:
                selected_rows = [
                    r
                    for r in selected_rows
                    if str(first) <= r["game_date"] <= str(last)
                ]
            elif window == "last_5":
                selected_rows = selected_rows[:5]
            elif window == "prior_5":
                selected_rows = selected_rows[5:10]
            elif window == "last_10":
                selected_rows = selected_rows[:10]
            distributions.append(
                dict(
                    player_id=pid,
                    games=[
                        dict(
                            game_id=r["game_id"],
                            game_date=r["game_date"],
                            team_abbr=r["team_abbr"],
                            opponent_abbr=r["opponent_abbr"],
                            metrics=metrics([r], "core_performance"),
                        )
                        for r in selected_rows
                    ],
                )
            )
        return wrap(
            dict(players=rows, differences=differences, distributions=distributions)
        )

    @app.get("/api/compare")
    def compare_api(
        player_a_id: int,
        player_b_id: int,
        window: str = "scope",
        first: date | None = None,
        last: date | None = None,
    ):
        return compare(player_a_id, player_b_id, window, first, last)

    @app.get("/api/performance/initial")
    @app.get("/api/performance/dates")
    def dates():
        return wrap(sorted({g["game_date"] for g in models["dim_game"]}, reverse=True))

    @app.get("/api/performance/games")
    def games(game_date: date):
        if not first_day <= game_date <= last_day:
            raise HTTPException(422, "date outside replay")
        return wrap([g for g in models["dim_game"] if g["game_date"] == str(game_date)])

    @app.get("/api/performance/players")
    def performances(game_id: str):
        if game_id not in {g["game_id"] for g in models["dim_game"]}:
            raise HTTPException(404, "game not in replay")
        return wrap([r for r in models["core_performance"] if r["game_id"] == game_id])

    @app.get("/api/performance/players/{player_id}")
    def performance_player(player_id: int):
        player(player_id)
        return wrap(
            [r for r in models["core_performance"] if r["player_id"] == player_id]
        )

    def query(q):
        player(q.player_id)
        if (
            q.metric not in PROFILE["metrics"]
            or q.aggregation not in PROFILE["metrics"][q.metric]["aggregations"]
            or q.operation not in {"summary", "game_log"}
        ):
            raise HTTPException(422, "unsupported core operation")
        q = q.model_copy(
            update={"first": q.first or first_day, "last": q.last or last_day}
        )
        if not first_day <= q.first <= q.last <= last_day:
            raise HTTPException(422, "date range outside replay; no silent clamping")
        try:
            result = run_query(
                evidence,
                Query(
                    metric=q.metric,
                    season=scope["season"],
                    season_type="Regular Season",
                    aggregation="average"
                    if q.aggregation == "per_game"
                    else "ratio"
                    if q.aggregation in {"ratio", "per36"}
                    else "total",
                    operation=q.operation,
                    player_id=q.player_id,
                    window="date_range",
                    start_date=str(q.first),
                    as_of=str(q.last),
                    min_games=1 if q.operation == "summary" else None,
                ),
            )
            return wrap(result)
        except SemanticError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/core/query")
    def query_api(q: StatQuery):
        return query(q)

    def ask(question):
        # Closed rephrasings retain exact identity and the publication scope.
        natural_total = re.fullmatch(
            r"how many points did (.+?) score\??", question.strip(), re.I
        )
        natural_average = re.fullmatch(
            r"(.+?) average (points|rebounds|assists)\??", question.strip(), re.I
        )
        if natural_total:
            question = f"{natural_total.group(1)} total points"
        elif natural_average:
            question = f"{natural_average.group(1)} per game {natural_average.group(2)}"
        # A small, explicit comparison grammar; unsupported intent stays unsupported.
        comparison = re.fullmatch(
            r"compare (.+?) vs (.+?) (total|per game|ratio|per 36) (.+?)(?: from (\d{4}-\d{2}-\d{2}) to (\d{4}-\d{2}-\d{2}))?",
            question.strip(),
            re.I,
        )
        if comparison:
            a, b, mode, key, first, last = comparison.groups()
            names = []
            for name in [a, b]:
                found = [
                    p
                    for p in identities.values()
                    if p["player_name"].casefold() == name.casefold()
                ]
                if len(found) != 1:
                    raise HTTPException(422, "player identity requires clarification")
                names.append(found[0]["player_id"])
            key = {
                "points": "pts",
                "rebounds": "reb",
                "assists": "ast",
                "minutes": "min",
                "fg%": "fg_pct",
                "3p%": "fg3_pct",
                "ft%": "ft_pct",
            }.get(key.casefold(), key.casefold())
            agg = {
                "total": "total",
                "per game": "per_game",
                "ratio": "ratio",
                "per 36": "per36",
            }[mode.casefold()]
            if agg == "per36" and key in {"pts", "ast"}:
                key += "_per36"
            if (
                key not in PROFILE["metrics"]
                or agg not in PROFILE["metrics"][key]["aggregations"]
            ):
                raise HTTPException(422, "unsupported comparison metric")
            try:
                result = compare(
                    *names,
                    "scope",
                    date.fromisoformat(first) if first else None,
                    date.fromisoformat(last) if last else None,
                )
            except ValueError as exc:
                raise HTTPException(422, "invalid date range") from exc
            result["data"]["differences"] = [
                m
                for m in result["data"]["differences"]
                if m["metric"] == key and m["aggregation"] == agg
            ]
            for row in result["data"]["players"]:
                row["metrics"] = [
                    m
                    for m in row["metrics"]
                    if m["metric"] == key and m["aggregation"] == agg
                ]
            return result
        # Exact deterministic grammar, not a permissive keyword fallback.
        match = re.fullmatch(
            r"(.+?) (total|per game|ratio|per 36|game log) (.+?)(?: from (\d{4}-\d{2}-\d{2}) to (\d{4}-\d{2}-\d{2}))?",
            question.strip(),
            re.I,
        )
        if not match:
            raise HTTPException(
                422,
                "Use: Player Name total points, Player Name per game assists, or a structured core query; other intents are unsupported in this replay.",
            )
        name, mode, key, first, last = match.groups()
        found = [
            p
            for p in identities.values()
            if p["player_name"].casefold() == name.casefold()
        ]
        if len(found) != 1:
            raise HTTPException(422, "player identity requires clarification")
        aliases = {
            "points": "pts",
            "rebounds": "reb",
            "assists": "ast",
            "minutes": "min",
            "fg%": "fg_pct",
            "3p%": "fg3_pct",
            "ft%": "ft_pct",
        }
        key = aliases.get(key.casefold(), key.casefold())
        agg = {
            "total": "total",
            "per game": "per_game",
            "ratio": "ratio",
            "per 36": "per36",
            "game log": "total",
        }[mode.casefold()]
        if agg == "per36" and key in {"pts", "ast"}:
            key += "_per36"
        try:
            q = StatQuery(
                player_id=found[0]["player_id"],
                metric=key,
                aggregation=agg,
                operation="game_log" if mode.casefold() == "game log" else "summary",
                first=first or scope["first"],
                last=last or scope["last"],
            )
        except ValueError as exc:
            raise HTTPException(422, "invalid date range") from exc
        return query(q)

    @app.post("/api/agent/ask")
    def ask_api(body: Ask):
        return ask(body.question)

    @app.post("/api/agent/ask/stream")
    def ask_stream(body: Ask):
        result = ask(body.question)

        def stream():
            try:
                available = authorized() is True and status()["freshness"] != "expired"
            except ValueError:
                available = False
            if not available:
                yield 'event: error\ndata: {"detail":"authority unavailable"}\n\n'
            else:
                yield (
                    "event: final\ndata: "
                    + json.dumps(result, allow_nan=False)
                    + "\n\n"
                )

        return StreamingResponse(stream(), media_type="text/event-stream")

    template = Environment(
        loader=FileSystemLoader(Path(__file__).resolve().parents[1] / "templates"),
        autoescape=select_autoescape(),
    ).get_template("core_replay.html")

    def page(title, payload=None):
        return HTMLResponse(
            template.render(
                title=title,
                payload=payload,
                players=list(identities.values()),
                publication_id=manifest["publication_id"],
                evidence_kind=manifest["evidence_kind"],
                replay_as_of=clock(),
                scope_label=(
                    first_day.strftime("%b %d")
                    + (
                        "–" + last_day.strftime("%d, %Y")
                        if first_day.month == last_day.month
                        and first_day.year == last_day.year
                        else " – " + last_day.strftime("%b %d, %Y")
                    )
                ),
                status=status(),
            )
        )

    @app.get("/")
    @app.get("/ask")
    def ask_page():
        return page("Ask core stats")

    @app.get("/players/{player_id}")
    @app.get("/players/{player_id}/content")
    def player_page(player_id: int):
        return page("Player", player(player_id))

    @app.get("/compare")
    def compare_page(
        player_a_id: int,
        player_b_id: int,
        window: str = "scope",
        first: date | None = None,
        last: date | None = None,
    ):
        return page("Compare", compare(player_a_id, player_b_id, window, first, last))

    @app.get("/performance")
    def performance_page():
        return page("Regular Season Performance", wrap(models["core_performance"]))

    return app
