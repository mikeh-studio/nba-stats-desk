"""Renderer-owned exact mappings. Each adapter names fields, never searches values."""

from app.agent.claims import bind


def identities(payload, players):
    payload.setdefault("claim_evidence", {})["identities"] = {
        str(p["player_id"]): {k: p[k] for k in ("player_id", "player_name")}
        for p in players
    }


def metric_tables(payload, players):
    evidence = payload["semantic_evidence"]
    sections = (
        [("current", evidence["current"]), ("baseline", evidence["baseline"])]
        if "current" in evidence
        else [(None, evidence)]
    )
    used_ids = {
        row.get("player_id", section["scope"].get("player_id"))
        for _, section in sections
        for row in section["rows"]
    }
    for query in payload.get("query_plan", {}).get("queries", []):
        used_ids.update(query.get("excluded_player_ids") or [])
    identities(payload, [p for p in players if p["player_id"] in used_ids])
    for ti, (side, section) in enumerate(sections):
        base = ["semantic_evidence"] + ([side] if side else [])
        scope = base + ["scope"]
        metric = section["metric"]
        ratio = metric["unit"] == "ratio"
        for ri, row in enumerate(section["rows"]):
            rp = base + ["rows", ri]
            pid = row.get("player_id", section["scope"].get("player_id"))
            named = str(pid) in payload["claim_evidence"]["identities"]
            ip = (
                ["claim_evidence", "identities", str(pid)]
                if named
                else rp + ["player_id"]
                if "player_id" in row
                else scope + ["player_id"]
            )

            def cell(column, field, spec=None, *, key=None, unit=None, source=None):
                bind(
                    payload,
                    sources=[source or rp + [field]],
                    destination=["tables", ti, "rows", ri, column],
                    metric=key or metric["key"],
                    unit=unit or metric["unit"],
                    identity=ip,
                    scope=scope,
                    spec=spec,
                    context=[base + ["metric"]],
                )

            if section["scope"]["operation"] == "game_log":
                for ci, field in enumerate(
                    (
                        "game_date",
                        "game_id",
                        "season_type",
                        "team_abbr",
                        "opponent_abbr",
                    )
                ):
                    cell(
                        ci,
                        field,
                        {"string": True, "null": "Unknown"},
                        key=field,
                        unit="label",
                    )
                cell(
                    5,
                    "value",
                    {
                        "digits": 1,
                        "scale": 100 if ratio else 1,
                        "suffix": "%" if ratio else "",
                        "null": "Unavailable",
                    },
                )
                # All-or-nothing chart policy preserves point-row correspondence.
                if payload["charts"] and len(
                    payload["charts"][0]["series"][0]["points"]
                ) == len(section["rows"]):
                    for field, source, spec in (
                        ("y", "value", {"scale": 100 if ratio else 1, "round": 1}),
                        ("x", "game_date", {"string": True}),
                    ):
                        bind(
                            payload,
                            sources=[rp + [source]],
                            destination=["charts", 0, "series", 0, "points", ri, field],
                            metric=metric["key"],
                            unit=metric["unit"],
                            identity=ip,
                            scope=scope,
                            spec=spec,
                            context=[base + ["metric"], rp + ["game_id"]],
                        )
            else:
                cell(
                    0,
                    "",
                    source=ip + ["player_name"] if named else ip,
                    spec=None if named else {"string": True},
                    key="player_name",
                    unit="label",
                )
                cell(
                    1,
                    "value",
                    {
                        "digits": 1,
                        "scale": 100 if ratio else 1,
                        "suffix": "%" if ratio else "",
                        "null": "Unavailable",
                    },
                )
                for ci, field in ((2, "observed_games"), (3, "valid_games")):
                    cell(ci, field, {"string": True}, key=field, unit="games")
                cell(
                    4, "rank", {"string": True, "null": "—"}, key="rank", unit="ordinal"
                )
                cell(
                    5,
                    "percentile",
                    {"digits": 1, "null": "Unavailable"},
                    key="percentile",
                    unit="percentile",
                )
    from app.agent.claim_rules import metric_assumption_contracts

    for index, (sources, identity, scope, context, spec) in metric_assumption_contracts(
        payload
    ).items():
        bind(
            payload,
            sources=sources,
            destination=["assumptions", index],
            metric="scope",
            unit="label",
            identity=identity,
            scope=scope,
            context=context,
            spec=spec,
        )


def reference_tables(payload, player):
    identities(payload, [player])
    e = payload["semantic_evidence"]
    base = ["semantic_evidence"]
    scope = base + ["scope"]
    kind = e["kind"]
    if kind in {"similarity", "recent_statistical_similarity"}:
        rows = [(base + ["rows", i], r) for i, r in enumerate(e["rows"])]
        if kind == "recent_statistical_similarity":
            rows.insert(0, (base + ["player"], e["player"]))
        for ri, (rp, row) in enumerate(rows):

            def cell(ci, sources, metric, unit, spec=None):
                bind(
                    payload,
                    sources=[rp + [s] for s in sources],
                    destination=["tables", 0, "rows", ri, ci],
                    metric=metric,
                    unit=unit,
                    identity=rp + ["player_id"],
                    scope=scope,
                    spec=spec,
                    context=[rp + ["player_name"]],
                )

            cell(0, ["player_name"], "player_name", "label")
            if kind == "similarity":
                cell(
                    1,
                    ["similarity_score"],
                    "similarity_score",
                    "dimensionless",
                    {"digits": 4},
                )
            else:
                for ci, metric in enumerate(e["metrics"], 1):
                    bind(
                        payload,
                        sources=[rp + ["values", metric]],
                        destination=["tables", 0, "rows", ri, ci],
                        metric=metric,
                        unit="per game",
                        identity=rp + ["player_id"],
                        scope=scope,
                        spec={"digits": 1},
                        context=[rp + ["player_name"], base + ["metrics"]],
                    )
                offset = len(e["metrics"]) + 1
                cell(offset, ["games"], "gp", "games", {"string": True})
                cell(
                    offset + 1,
                    ["start_date", "end_date"],
                    "sample_dates",
                    "date",
                    {"op": "template", "template": "{0} – {1}"},
                )
                if ri:
                    cell(
                        offset + 2,
                        ["similarity_score"],
                        "similarity_score",
                        "dimensionless",
                        {"digits": 4},
                    )
    elif kind == "league_baseline":
        metric = e["metric"]
        ratio = metric["unit"] == "ratio"
        for ri, side in enumerate(("player", "baseline")):
            rp = base + [side]
            ip = (
                base + ["player_id"]
                if ri == 0
                else base + ["baseline_scope", "population"]
            )
            for ci, field in ((1, "value"), (2, "observed_games"), (3, "valid_games")):
                bind(
                    payload,
                    sources=[rp + [field]],
                    destination=["tables", 0, "rows", ri, ci],
                    metric=metric["key"] if ci == 1 else field,
                    unit=metric["unit"] if ci == 1 else "games",
                    identity=ip,
                    scope=scope if ri == 0 else base + ["baseline_scope"],
                    context=[base + ["metric"]],
                    spec={
                        "digits": 1,
                        "scale": 100 if ratio else 1,
                        "suffix": "%" if ratio else " per game",
                        "null": "Unavailable",
                    }
                    if ci == 1
                    else {"string": True},
                )
        bind(
            payload,
            sources=[
                [
                    "claim_evidence",
                    "identities",
                    str(player["player_id"]),
                    "player_name",
                ]
            ],
            destination=["tables", 0, "rows", 0, 0],
            metric="player_name",
            unit="label",
            identity=base + ["player_id"],
            scope=scope,
        )


def research_table(payload):
    result = payload["research"]
    ri = 0
    for pi, player in enumerate(result["players"]):
        pp = ["research", "players", pi]
        for mi, metric in enumerate(player["metrics"]):
            mp = pp + ["metrics", mi]
            for ci, path in enumerate(
                (
                    pp + ["player_name"],
                    mp + ["label"],
                    mp + ["result", "value"],
                    mp + ["unit"],
                    mp + ["aggregation"],
                    mp + ["result", "valid_games"],
                    mp + ["result", "observed_games"],
                    pp + ["filter_missing_games"],
                )
            ):
                # A missing result is explicitly unavailable; no invented source zero.
                if ci in (2, 5, 6) and not metric.get("result"):
                    continue
                bind(
                    payload,
                    sources=[path],
                    destination=["tables", 0, "rows", ri, ci],
                    metric=metric["metric"]
                    if "metric" in metric
                    else metric.get("key", "value"),
                    unit=metric["unit"] if ci == 2 else "games" if ci >= 5 else "label",
                    identity=pp + ["player_id"],
                    scope=["research", "scope"],
                    context=[mp],
                    spec={
                        "scale": 100 if metric["unit"] == "percent" else 1,
                        "round": 1,
                    }
                    if ci == 2
                    else None,
                )
            ri += 1


def appearance_table(payload, players):
    identities(payload, players)
    for i, result in enumerate(payload["appearance_evidence"]):
        rp = ["appearance_evidence", i]
        pid = result["rows"][0]["player_id"]
        ip = ["claim_evidence", "identities", str(pid)]
        for ci, source, metric, unit in (
            (0, ip + ["player_name"], "player_name", "label"),
            (1, rp + ["rows", 0, "observed_games"], "gp", "games"),
        ):
            bind(
                payload,
                sources=[source],
                destination=["tables", 0, "rows", i, ci],
                metric=metric,
                unit=unit,
                identity=ip,
                scope=rp + ["scope"],
            )


def availability_table(payload):
    e = payload["availability_evidence"]
    base = ["availability_evidence"]
    for i, metric in enumerate(e["metrics"]):
        mp = base + ["metrics", i]
        for ci, path in (
            (0, mp + ["label"]),
            (1, mp + ["groups", "both_played", "value"]),
            (2, mp + ["groups", "did_not_play", "value"]),
            (3, mp + ["difference"]),
        ):
            bind(
                payload,
                sources=[path],
                destination=["tables", 0, "rows", i, ci],
                metric=metric["metric"],
                unit=metric["unit"]
                if ci != 3
                else "percentage points"
                if metric["unit"] == "%"
                else metric["unit"],
                identity=base + ["request", "player_id"],
                scope=base + ["request"],
                context=[mp, base + ["request", "teammate_id"]],
                spec={
                    "digits": 1,
                    "scale": 100 if ci in (1, 2) and metric["unit"] == "%" else 1,
                    "null": "unavailable",
                }
                if ci
                else None,
            )
        for ci, group in ((5, "both_played"), (6, "did_not_play")):
            bind(
                payload,
                sources=[
                    mp + ["groups", group, f] for f in ("valid_games", "observed_games")
                ],
                destination=["tables", 0, "rows", i, ci],
                metric="sample_count",
                unit="games",
                identity=base + ["request", "player_id"],
                scope=base + ["request"],
                context=[mp, base + ["request", "teammate_id"]],
                spec={"op": "template", "template": "{0} of {1}"},
            )


def split_table(payload):
    e = payload["semantic_evidence"]
    base = ["semantic_evidence"]
    for i, result in enumerate(e["comparisons"]):
        rp = base + ["comparisons", i]
        metric = result["current"]["metric"]
        ratio = metric["unit"] == "ratio"
        for ci, side in ((1, "current"), (2, "baseline")):
            if not result[side]["rows"]:
                continue
            bind(
                payload,
                sources=[rp + [side, "rows", 0, "value"]],
                destination=["tables", 0, "rows", i, ci],
                metric=metric["key"],
                unit=metric["unit"],
                identity=base + ["request", "player"],
                scope=rp + [side, "scope"],
                context=[rp + [side, "metric"]],
                spec={
                    "digits": 2,
                    "scale": 100 if ratio else 1,
                    "suffix": "%" if ratio else "",
                    "null": "Unavailable",
                },
            )
        bind(
            payload,
            sources=[rp + ["difference"]],
            destination=["tables", 0, "rows", i, 3],
            metric=metric["key"],
            unit=metric["unit"],
            identity=base + ["request", "player"],
            scope=base + ["request"],
            context=[rp + ["current", "metric"]],
            spec={
                "digits": 2,
                "signed": True,
                "suffix": " pp" if ratio else "",
                "null": "Unavailable",
            },
        )


def published_study_table(payload, study, selected):
    # Bounded validated publication fields, not the presentation or raw report.
    from copy import deepcopy

    public = {
        k: deepcopy(study[k])
        for k in (
            "pair_id",
            "player_id",
            "player_name",
            "teammate_id",
            "teammate_name",
            "scope",
        )
        if k in study
    }
    public["metrics"] = [
        {
            k: deepcopy(m[k])
            for k in ("metric", "label", "unit", "descriptive")
            if k in m
        }
        for m in study["metrics"]
        if m["metric"] in selected
    ]
    for item in public["metrics"]:
        item.setdefault("unit", "per game")  # Published study v1 contract default.
    payload.setdefault("claim_evidence", {})["study"] = public
    if not public.get("scope"):
        return
    base = ["claim_evidence", "study"]
    for i, metric in enumerate(public["metrics"]):
        mp = base + ["metrics", i]
        for ci, field in (
            (0, "label"),
            (1, "participated"),
            (2, "reported_out"),
            (3, "difference"),
            (4, "unit"),
        ):
            if ci in (0, 4) and field not in metric:
                continue
            if ci in (1, 2, 3) and (
                not metric.get("descriptive") or field not in metric["descriptive"]
            ):
                continue
            source = mp + ([field] if ci in (0, 4) else ["descriptive", field])
            bind(
                payload,
                sources=[source],
                destination=["tables", 0, "rows", i, ci],
                metric=metric["metric"],
                unit=metric.get("unit", "per game"),
                identity=base + ["player_id"],
                scope=base + ["scope"],
                context=[mp, base + ["teammate_id"]],
                spec={
                    "digits": 1,
                    "null": "Not available",
                    **(
                        {"signed": True, "normalize_zero": True, "unsigned_zero": True}
                        if ci == 3
                        else {}
                    ),
                }
                if ci in (1, 2, 3)
                else None,
            )


def frozen_study_table(payload, study):
    from copy import deepcopy

    from app.agent.teammate_ask import public_scope

    stats = study["statistics"]
    public = {
        "study_id": study["study_id"],
        "scope": public_scope(study),
        "statistics": {
            k: deepcopy(stats[k])
            for k in (
                "original_raw_difference",
                "complete_case_raw_difference",
                "original_played_n",
                "original_out_n",
            )
        },
    }
    public["statistics"]["adjusted"] = {
        k: deepcopy(stats["adjusted"][k])
        for k in (
            "difference",
            "played_n",
            "out_n",
            "nominal_cluster_95_ci",
            "nominal_p",
        )
    }
    payload.setdefault("claim_evidence", {})["frozen_study"] = public
    from app.agent.claim_rules import frozen_contracts

    for destination, (
        sources,
        identity,
        scope,
        context,
        spec,
    ) in frozen_contracts().items():
        bind(
            payload,
            sources=sources,
            destination=list(destination),
            metric="ast",
            unit="assists per game",
            identity=identity,
            scope=scope,
            context=context,
            spec=spec,
        )


def leaderboard_reference(payload):
    e, ref = payload["semantic_evidence"], payload["reference_evidence"]
    base = ["reference_evidence"]
    rp = base + ["rows", 0]
    ip = ["claim_evidence", "identities", str(ref["rows"][0]["player_id"])]
    context = [["semantic_evidence", "metric"], base + ["metric"], base + ["scope"]]
    scope = ["semantic_evidence", "scope"]
    ratio = ref["metric"]["unit"] == "ratio"
    for i, _ in enumerate(e["rows"]):
        bind(
            payload,
            sources=[["semantic_evidence", "rows", i, "value"], rp + ["value"]],
            destination=["tables", 0, "rows", i, 6],
            metric=ref["metric"]["key"],
            unit=ref["metric"]["unit"],
            identity=["claim_evidence", "identities"],
            scope=scope,
            context=context,
            spec={
                "op": "difference",
                "scale": 100 if ratio else 1,
                "digits": 1,
                "signed": True,
            },
        )
    i = len(e["rows"])
    for ci, field, spec in (
        (0, "player_name", None),
        (1, "value", {"digits": 1}),
        (2, "observed_games", {"string": True}),
        (3, "valid_games", {"string": True}),
        (4, "rank", {"string": True}),
        (5, "percentile", {"digits": 1, "null": "Unavailable"}),
    ):
        bind(
            payload,
            sources=[ip + [field] if ci == 0 else rp + [field]],
            destination=["tables", 0, "rows", i, ci],
            metric=ref["metric"]["key"],
            unit=ref["metric"]["unit"],
            identity=ip,
            scope=base + ["scope"],
            context=[base + ["metric"]],
            spec=spec,
        )
    bind(
        payload,
        sources=[rp + ["value"], rp + ["value"]],
        destination=["tables", 0, "rows", i, 6],
        metric=ref["metric"]["key"],
        unit=ref["metric"]["unit"],
        identity=ip,
        scope=base + ["scope"],
        context=[base + ["metric"]],
        spec={"op": "difference", "digits": 1},
    )
