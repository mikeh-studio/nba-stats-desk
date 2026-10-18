"""Independent destination-to-source schemas for instrumented renderer fields.

These rules consume route evidence shape and destination coordinates, never the
rendered values. They prevent a declaration from substituting an equal-valued
operand from another identity, metric, or scope.
"""


def mapping(payload, destination):
    if (payload.get("claim_evidence") or {}).get("frozen_study"):
        return frozen_contracts()[tuple(destination)][:4]
    root, *rest = destination
    if root == "assumptions" and len(rest) == 1:
        return metric_assumption_contracts(payload)[rest[0]][:4]
    e = payload.get("semantic_evidence") or {}
    if root == "tables" and len(rest) == 4 and rest[1] == "rows":
        ti, _, ri, ci = rest
        if (
            payload.get("reference_evidence")
            and ti == 0
            and (ci == 6 or ri == len(e["rows"]))
        ):
            base = ["reference_evidence"]
            rp = base + ["rows", 0]
            ip = [
                "claim_evidence",
                "identities",
                str(payload["reference_evidence"]["rows"][0]["player_id"]),
            ]
            if ri < len(e["rows"]):
                return (
                    [["semantic_evidence", "rows", ri, "value"], rp + ["value"]],
                    ["claim_evidence", "identities"],
                    ["semantic_evidence", "scope"],
                    [
                        ["semantic_evidence", "metric"],
                        base + ["metric"],
                        base + ["scope"],
                    ],
                )
            sources = (
                [rp + ["value"], rp + ["value"]]
                if ci == 6
                else [
                    ip + ["player_name"]
                    if ci == 0
                    else rp
                    + [
                        {
                            1: "value",
                            2: "observed_games",
                            3: "valid_games",
                            4: "rank",
                            5: "percentile",
                        }[ci]
                    ]
                ]
            )
            return sources, ip, base + ["scope"], [base + ["metric"]]
        study = (payload.get("claim_evidence") or {}).get("study")
        if study and ti == 0:
            base = ["claim_evidence", "study"]
            mp = base + ["metrics", ri]
            field = {
                0: "label",
                1: "participated",
                2: "reported_out",
                3: "difference",
                4: "unit",
            }[ci]
            source = mp + ([field] if ci in (0, 4) else ["descriptive", field])
            return (
                [source],
                base + ["player_id"],
                base + ["scope"],
                [mp, base + ["teammate_id"]],
            )
        if e.get("kind") == "player_split" and ti == 0:
            base = ["semantic_evidence"]
            rp = base + ["comparisons", ri]
            if ci in (1, 2):
                side = "current" if ci == 1 else "baseline"
                return (
                    [rp + [side, "rows", 0, "value"]],
                    base + ["request", "player"],
                    rp + [side, "scope"],
                    [rp + [side, "metric"]],
                )
            if ci == 3:
                return (
                    [rp + ["difference"]],
                    base + ["request", "player"],
                    base + ["request"],
                    [rp + ["current", "metric"]],
                )
        if "research" in payload:
            index = 0
            for pi, p in enumerate(payload["research"]["players"]):
                for mi, m in enumerate(p["metrics"]):
                    if index == ri and ti == 0:
                        pp = ["research", "players", pi]
                        mp = pp + ["metrics", mi]
                        sources = [
                            pp + ["player_name"],
                            mp + ["label"],
                            mp + ["result", "value"],
                            mp + ["unit"],
                            mp + ["aggregation"],
                            mp + ["result", "valid_games"],
                            mp + ["result", "observed_games"],
                            pp + ["filter_missing_games"],
                        ]
                        return (
                            [sources[ci]],
                            pp + ["player_id"],
                            ["research", "scope"],
                            [mp],
                        )
                    index += 1
        if "appearance_evidence" in payload and ti == 0:
            base = ["appearance_evidence", ri]
            pid = payload["appearance_evidence"][ri]["rows"][0]["player_id"]
            ip = ["claim_evidence", "identities", str(pid)]
            return (
                [[ip + ["player_name"], base + ["rows", 0, "observed_games"]][ci]],
                ip,
                base + ["scope"],
                [],
            )
        if "availability_evidence" in payload and ti == 0:
            base = ["availability_evidence"]
            mp = base + ["metrics", ri]
            if ci in (5, 6):
                group = "both_played" if ci == 5 else "did_not_play"
                return (
                    [
                        mp + ["groups", group, f]
                        for f in ("valid_games", "observed_games")
                    ],
                    base + ["request", "player_id"],
                    base + ["request"],
                    [mp, base + ["request", "teammate_id"]],
                )
            sources = [
                mp + ["label"],
                mp + ["groups", "both_played", "value"],
                mp + ["groups", "did_not_play", "value"],
                mp + ["difference"],
            ]
            return (
                [sources[ci]],
                base + ["request", "player_id"],
                base + ["request"],
                [mp, base + ["request", "teammate_id"]],
            )
        kind = e.get("kind")
        base = ["semantic_evidence"]
        if kind in ("similarity", "recent_statistical_similarity") and ti == 0:
            recent = kind == "recent_statistical_similarity"
            rp = base + (
                ["player"] if recent and ri == 0 else ["rows", ri - 1 if recent else ri]
            )
            contexts = [rp + ["player_name"]]
            if ci == 0:
                sources = [rp + ["player_name"]]
            elif not recent:
                sources = [rp + ["similarity_score"]]
            elif ci <= len(e["metrics"]):
                sources = [rp + ["values", e["metrics"][ci - 1]]]
                contexts.append(base + ["metrics"])
            elif ci == len(e["metrics"]) + 1:
                sources = [rp + ["games"]]
            elif ci == len(e["metrics"]) + 2:
                sources = [rp + ["start_date"], rp + ["end_date"]]
            else:
                sources = [rp + ["similarity_score"]]
            return sources, rp + ["player_id"], base + ["scope"], contexts
        if kind == "league_baseline" and ti == 0:
            ip = base + (["player_id"] if ri == 0 else ["baseline_scope", "population"])
            scope = base + (["scope"] if ri == 0 else ["baseline_scope"])
            if ci == 0 and ri == 0:
                return (
                    [
                        [
                            "claim_evidence",
                            "identities",
                            str(e["player_id"]),
                            "player_name",
                        ]
                    ],
                    ip,
                    scope,
                    [],
                )
            field = {1: "value", 2: "observed_games", 3: "valid_games"}[ci]
            return (
                [base + ["player" if ri == 0 else "baseline", field]],
                ip,
                scope,
                [base + ["metric"]],
            )
        if "metric" in e or "current" in e:
            side = ("current", "baseline")[ti] if "current" in e else None
            section = e[side] if side else e
            base += [side] if side else []
            row = section["rows"][ri]
            rp = base + ["rows", ri]
            pid = row.get("player_id", section["scope"].get("player_id"))
            named = str(pid) in payload["claim_evidence"]["identities"]
            ip = (
                ["claim_evidence", "identities", str(pid)]
                if named
                else rp + ["player_id"]
                if "player_id" in row
                else base + ["scope", "player_id"]
            )
            if section["scope"]["operation"] == "game_log":
                source = rp + [
                    (
                        "game_date",
                        "game_id",
                        "season_type",
                        "team_abbr",
                        "opponent_abbr",
                        "value",
                    )[ci]
                ]
            else:
                source = (
                    (ip + ["player_name"] if named else ip)
                    if ci == 0
                    else rp
                    + [
                        {
                            1: "value",
                            2: "observed_games",
                            3: "valid_games",
                            4: "rank",
                            5: "percentile",
                        }[ci]
                    ]
                )
            return [source], ip, base + ["scope"], [base + ["metric"]]
    if (
        root == "charts"
        and len(rest) == 6
        and rest[:3] == [0, "series", 0]
        and rest[3] == "points"
    ):
        ri, field = rest[4:]
        rp = ["semantic_evidence", "rows", ri]
        pid = e["scope"]["player_id"]
        return (
            [rp + [{"x": "game_date", "y": "value"}[field]]],
            ["claim_evidence", "identities", str(pid)],
            ["semantic_evidence", "scope"],
            [["semantic_evidence", "metric"], rp + ["game_id"]],
        )
    raise ValueError("Unregistered presentation destination")


def transformation(payload, destination):
    """Registered formatting contract; declarations cannot choose easier formats."""
    from app.agent.claims import resolve

    if (payload.get("claim_evidence") or {}).get("frozen_study"):
        return frozen_contracts()[tuple(destination)][4]
    if destination[0] == "assumptions":
        return metric_assumption_contracts(payload)[destination[1]][4]
    sources, _, _, contexts = mapping(payload, destination)
    field = sources[0][-1]
    e = payload.get("semantic_evidence") or {}
    ci = destination[-1]
    if (
        payload.get("reference_evidence")
        and destination[0] == "tables"
        and (ci == 6 or destination[3] == len(e["rows"]))
    ):
        if destination[3] < len(e["rows"]):
            return {
                "op": "difference",
                "scale": 100 if e["metric"]["unit"] == "ratio" else 1,
                "digits": 1,
                "signed": True,
            }
        return {
            0: {"op": "identity"},
            1: {"digits": 1},
            2: {"string": True},
            3: {"string": True},
            4: {"string": True},
            5: {"digits": 1, "null": "Unavailable"},
            6: {"op": "difference", "digits": 1},
        }[ci]
    if destination[0] == "charts":
        return (
            {"string": True}
            if ci == "x"
            else {"scale": 100 if e["metric"]["unit"] == "ratio" else 1, "round": 1}
        )
    if "research" in payload:
        metric = resolve(payload, contexts[0])
        return (
            {"scale": 100 if metric["unit"] == "percent" else 1, "round": 1}
            if ci == 2
            else {"op": "identity"}
        )
    if "appearance_evidence" in payload:
        return {"op": "identity"}
    if "availability_evidence" in payload:
        if ci in (5, 6):
            return {"op": "template", "template": "{0} of {1}"}
        metric = resolve(payload, contexts[0])
        return (
            {
                "digits": 1,
                "scale": 100 if ci in (1, 2) and metric["unit"] == "%" else 1,
                "null": "unavailable",
            }
            if ci
            else {"op": "identity"}
        )
    if (payload.get("claim_evidence") or {}).get("study"):
        return (
            {
                "digits": 1,
                "null": "Not available",
                **(
                    {"signed": True, "normalize_zero": True, "unsigned_zero": True}
                    if ci == 3
                    else {}
                ),
            }
            if ci in (1, 2, 3)
            else {"op": "identity"}
        )
    if e.get("kind") == "player_split":
        ratio = resolve(payload, contexts[0])["unit"] == "ratio"
        return (
            {
                "digits": 2,
                "signed": True,
                "suffix": " pp" if ratio else "",
                "null": "Unavailable",
            }
            if ci == 3
            else {
                "digits": 2,
                "scale": 100 if ratio else 1,
                "suffix": "%" if ratio else "",
                "null": "Unavailable",
            }
        )
    if e.get("kind") in ("similarity", "recent_statistical_similarity"):
        if field == "player_name":
            return {"op": "identity"}
        if field == "similarity_score":
            return {"digits": 4}
        if field == "games":
            return {"string": True}
        if field == "start_date":
            return {"op": "template", "template": "{0} – {1}"}
        return {"digits": 1}
    if e.get("kind") == "league_baseline":
        if ci == 0:
            return {"op": "identity"}
        ratio = e["metric"]["unit"] == "ratio"
        return (
            {
                "digits": 1,
                "scale": 100 if ratio else 1,
                "suffix": "%" if ratio else " per game",
                "null": "Unavailable",
            }
            if ci == 1
            else {"string": True}
        )
    metric = resolve(payload, contexts[0])
    ratio = metric["unit"] == "ratio"
    if field == "player_id":
        return {"string": True}
    if field == "player_name":
        return {"op": "identity"}
    if field in ("observed_games", "valid_games"):
        return {"string": True}
    if field == "rank":
        return {"string": True, "null": "—"}
    if field == "percentile":
        return {"digits": 1, "null": "Unavailable"}
    if field == "value":
        return {
            "digits": 1,
            "scale": 100 if ratio else 1,
            "suffix": "%" if ratio else "",
            "null": "Unavailable",
        }
    return {"string": True, "null": "Unknown"}


def metric_assumption_contracts(payload):
    """Stable append order from the metric renderer, including skipped gaps."""
    e = payload.get("semantic_evidence") or {}
    if not ("metric" in e or "current" in e) or e.get("kind"):
        return {}
    result, index = {}, 0
    sections = (
        [("current", e["current"]), ("baseline", e["baseline"])]
        if "current" in e
        else [(None, e)]
    )
    for side, section in sections:
        base = ["semantic_evidence"] + ([side] if side else [])
        scope = base + ["scope"]
        # Identity is query player where present, otherwise scoped ranking cohort.
        identity = scope + ["player_id"]

        def add(sources, template, *, formats=None, person=None, definition=None):
            nonlocal index
            spec = {"op": "template", "template": template}
            if formats:
                spec["formats"] = formats
            result[index] = (
                sources,
                person or identity,
                scope,
                [definition or base + ["metric"]],
                spec,
            )
            index += 1

        seasons = scope + (
            ["seasons"] if section["scope"].get("seasons") else ["season"]
        )
        add(
            [seasons, scope + ["season_type"], scope + ["window"], scope + ["as_of"]],
            "{0} {1}; {2}; as of {3}",
            formats=[
                "join_comma" if section["scope"].get("seasons") else "identity",
                "identity",
                "identity",
                "identity",
            ],
        )
        index += 1  # Contract version is metadata, not a statistical claim.
        for field, label in (
            ("team_abbr", "Appearance team"),
            ("opponent_abbr", "Opponent"),
            ("home_away", "Venue"),
        ):
            if section["scope"].get(field):
                add([scope + [field]], label + ": {0}")
        if section["scope"]["operation"] != "game_log":
            add(
                [
                    base + ["cohort", f]
                    for f in ("size", "min_games", "excluded_players")
                ],
                "Ranking cohort: {0} eligible players; minimum {1} games; {2} excluded.",
            )
            if section["cohort"]["min_attempts"] is not None:
                add(
                    [
                        base + ["cohort", "min_attempts"],
                        base + ["cohort", "attempt_field"],
                    ],
                    "Attempt minimum: {0} {1}.",
                )
        add(
            [
                base + ["provenance", f]
                for f in ("source", "snapshot_id", "data_through")
            ],
            "Source: {0}; snapshot {1}; data through {2}. Retrospective source evidence.",
        )
        index += len(section["warnings"])
        if section["scope"]["operation"] == "game_log":
            if (
                any(r["display_value"] is None for r in section["rows"])
                and section["rows"]
            ):
                index += 1
            continue
        for ri, row in enumerate(section["rows"]):
            rp = base + ["rows", ri]
            named = str(row["player_id"]) in payload["claim_evidence"]["identities"]
            ip = (
                ["claim_evidence", "identities", str(row["player_id"])]
                if named
                else rp + ["player_id"]
            )
            name_source = ip + ["player_name"] if named else ip
            if row["sample_warning"]:
                index += 1
            if row["status"] == "partial":
                add(
                    [name_source, rp + ["missing_component_games"]],
                    "{0}: missing components in {1} games; partial result.",
                    person=ip,
                )
            if section["scope"]["operation"] != "summary" and row["exclusion_reasons"]:
                index += 1
            if section["metric"]["denominator"]:
                add(
                    [name_source, rp + ["numerator"], rp + ["denominator"]],
                    "{0}: numerator {1}; denominator {2}.",
                    person=ip,
                )
    return result


def frozen_contracts():
    base = ["claim_evidence", "frozen_study"]
    stats = base + ["statistics"]
    identity, scope = base + ["scope", "focal_player_id"], base + ["scope"]
    specs = {
        (0, 1): (
            [stats + ["original_raw_difference"]],
            {"digits": 2, "signed": True, "normalize_zero": True},
        ),
        (0, 2): (
            [stats + ["original_played_n"], stats + ["original_out_n"]],
            {"op": "template", "template": "{0} played / {1} reported Out"},
        ),
        (1, 1): (
            [stats + ["complete_case_raw_difference"]],
            {"digits": 2, "signed": True, "normalize_zero": True},
        ),
        (1, 2): (
            [stats + ["adjusted", "played_n"], stats + ["adjusted", "out_n"]],
            {"op": "template", "template": "{0} played / {1} reported Out"},
        ),
        (2, 1): (
            [stats + ["adjusted", "difference"]],
            {"digits": 2, "signed": True, "normalize_zero": True},
        ),
        (3, 1): (
            [stats + ["adjusted", "nominal_cluster_95_ci", i] for i in (0, 1)],
            {
                "op": "template",
                "template": "{0} to {1}",
                "formats": ["signed2", "signed2"],
            },
        ),
        (4, 1): ([stats + ["adjusted", "nominal_p"]], {"op": "nominal_p"}),
    }
    return {
        ("tables", 0, "rows", ri, ci): (sources, identity, scope, [], spec)
        for (ri, ci), (sources, spec) in specs.items()
    }
