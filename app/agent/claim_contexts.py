"""Declared surrounding-label schemas, separate from rendered presentation values."""


def contracts(payload):
    if (payload.get("semantic_evidence") or {}).get("kind") == "observed_pts_shadow":
        from app.serving.presentation import claim_contracts

        return claim_contracts(payload)
    e = payload.get("semantic_evidence") or {}
    result = {}

    def add(
        destination, sources, identity, scope, context, template=None, formats=None
    ):
        spec = (
            {"op": "identity"}
            if template is None
            else {"op": "template", "template": template}
        )
        if formats:
            spec["formats"] = formats
        result[tuple(destination)] = (sources, identity, scope, context, spec)

    if payload.get("availability_evidence"):
        evidence = payload["availability_evidence"]
        base = ["availability_evidence"]
        scope, identity = base + ["request"], base + ["request", "player_id"]
        paths = [
            (base + ["groups", group, i], row, False)
            for group, rows in evidence["groups"].items()
            for i, row in enumerate(rows)
        ]
        paths += [
            (base + ["excluded_games", i], row, True)
            for i, row in enumerate(evidence["excluded_games"])
        ]
        paths.sort(key=lambda item: (item[1]["game_date"], item[1]["game_id"]))
        for ri, (rp, row, excluded) in enumerate(paths):
            for ci, field, format_name in (
                (0, "game_date", None),
                (1, "opponent", None),
                (2, "participation", "words"),
                (3, "reason", "words"),
                (4, "report_status", None),
                (6, "warnings", "warnings"),
            ):
                if ci == 3 and not excluded:
                    continue  # Included is a group-membership rule, not an imported reason.
                add(
                    ["tables", 1, "rows", ri, ci],
                    [rp + [field]],
                    identity,
                    scope,
                    [rp + ["game_id"]],
                    "{0}" if format_name else None,
                    [format_name] if format_name else None,
                )
        return result
    if payload.get("appearance_evidence"):
        scope = ["appearance_evidence", 0, "scope"]
        sources, fragments = [], []
        for ri, evidence in enumerate(payload["appearance_evidence"]):
            pid = evidence["rows"][0]["player_id"]
            offset = len(sources)
            sources.extend(
                [
                    ["claim_evidence", "identities", str(pid), "player_name"],
                    ["appearance_evidence", ri, "rows", 0, "observed_games"],
                ]
            )
            fragments.append(
                "{" + str(offset) + "}: {" + str(offset + 1) + "} games played"
            )
        offset = len(sources)
        sources.extend([scope + ["season"], scope + ["season_type"]])
        template = (
            "; ".join(fragments)
            + ". {"
            + str(offset)
            + "} · {"
            + str(offset + 1)
            + "} · "
        )
        formats = ["identity"] * len(sources)
        formats[-1] = "phase_lower"
        if payload["appearance_evidence"][0]["scope"]["window"] == "date_range":
            template += "from {" + str(len(sources)) + "} "
            sources.append(scope + ["window_start"])
            formats.append("identity")
        template += (
            "through {"
            + str(len(sources))
            + "}. These are each player's recorded appearances in this period, across teams."
        )
        sources.append(scope + ["as_of"])
        formats.append("identity")
        request = ["claim_evidence", "appearance_request"]
        if payload["claim_evidence"]["appearance_request"]["prior_availability"]:
            template += " The prior teammate-availability filter is not applied."
        if payload["claim_evidence"].get("name_resolution_notice"):
            template = "{" + str(len(sources)) + "}\n\n" + template
            sources.append(["claim_evidence", "name_resolution_notice"])
            formats.append("identity")
        add(
            ["answer"],
            sources,
            ["claim_evidence", "identities"],
            scope,
            [request],
            template,
            formats,
        )
        return result
    if e.get("kind") == "player_split":
        base = ["semantic_evidence"]
        scope = base + ["request"]
        identity = scope + ["player"]
        for ci, group in ((1, 0), (2, 1), (4, 0), (5, 1)):
            add(
                ["tables", 0, "columns", ci, "label"],
                [scope + ["groups", group, 0]],
                identity,
                scope,
                [],
                "Games with data — {0}" if ci >= 4 else None,
            )
        add(
            ["assumptions", 0],
            [scope + [f] for f in ("season", "start", "end", "aggregation")],
            identity,
            scope,
            [],
            "{0}; {1} through {2}; {3} counts, pooled shooting ratios.",
        )
        for ri, comparison in enumerate(e["comparisons"]):
            rp = base + ["comparisons", ri]
            mp = rp + ["current", "metric"]
            aggregation = comparison["current"]["scope"]["aggregation"]
            suffix = (
                " per game"
                if aggregation == "average"
                else " total"
                if aggregation == "total"
                else ""
            )
            add(
                ["tables", 0, "rows", ri, 0],
                [mp + ["label"]],
                identity,
                scope,
                [mp],
                "{0}" + suffix,
            )
            for ci, side in ((4, "current"), (5, "baseline")):
                if comparison[side]["rows"]:
                    add(
                        ["tables", 0, "rows", ri, ci],
                        [
                            rp + [side, "rows", 0, f]
                            for f in ("valid_games", "observed_games")
                        ],
                        identity,
                        rp + [side, "scope"],
                        [rp + [side, "metric"]],
                        "{0} of {1}",
                    )
        return result
    if not ("metric" in e or "current" in e) or e.get("kind"):
        return result
    sections = (
        [("current", e["current"]), ("baseline", e["baseline"])]
        if "current" in e
        else [(None, e)]
    )
    for ti, (side, section) in enumerate(sections):
        base = ["semantic_evidence"] + ([side] if side else [])
        scope = base + ["scope"]
        identity = scope + ["player_id"]
        mp = base + ["metric"]
        seasons = scope + (
            ["seasons"] if section["scope"].get("seasons") else ["season"]
        )
        sources = [
            mp + ["label"],
            seasons,
            scope + ["season_type"],
            scope + ["window"],
            scope + ["as_of"],
        ]
        formats = [
            "identity",
            "join_comma" if section["scope"].get("seasons") else "identity",
            "identity",
            "identity",
            "identity",
        ]
        template = (
            "Current" if side == "current" else "Baseline" if side else "Result"
        ) + ": {0} — {1} {2}; {3}; as of {4}"
        if section["scope"]["operation"] == "game_log":
            pid = section["scope"]["player_id"]
            ip = ["claim_evidence", "identities", str(pid)]
            name = (
                ip + ["player_name"]
                if str(pid) in payload["claim_evidence"]["identities"]
                else identity
            )
            sources.append(name)
            formats.append("identity")
            template = "{5}: {0} game log — {1} {2}; {3}; as of {4}"
            add(
                ["tables", ti, "columns", 5, "label"],
                [mp + ["label"]],
                identity,
                scope,
                [mp],
            )
            from app.agent.claims import chart_sections

            candidate_index = next(
                (
                    i
                    for i, (candidate_side, _) in enumerate(chart_sections(payload))
                    if candidate_side == side
                ),
                None,
            )
            selection = (payload.get("claim_bindings") or {}).get("chart_selection", [])
            if candidate_index in selection:
                chart_index = selection.index(candidate_index)
                add(
                    ["charts", chart_index, "title"],
                    [name, mp + ["label"]],
                    identity,
                    scope,
                    [mp],
                    "{0}: {1} by appearance",
                )
                add(
                    ["charts", chart_index, "y_label"],
                    [mp + ["label"]],
                    identity,
                    scope,
                    [mp],
                    "{0}" + (" (%)" if section["metric"]["unit"] == "ratio" else ""),
                )
                add(
                    ["charts", chart_index, "series", 0, "label"],
                    [name],
                    identity,
                    scope,
                    [mp],
                    "{0}",
                )
                add(
                    ["charts", chart_index, "series", 0, "key"],
                    [mp + ["key"]],
                    identity,
                    scope,
                    [mp],
                )
        # The reference composition intentionally replaces the ordinary title.
        if not payload.get("reference_evidence"):
            add(
                ["tables", ti, "title"],
                sources,
                identity,
                scope,
                [mp],
                template,
                formats,
            )
    return result


def guards(payload):
    """Formatting constants are structural checks, not statistical evidence."""
    e = payload.get("semantic_evidence") or {}
    result = {}
    if e.get("kind") == "player_split":
        columns = {0: "Metric", 3: "Difference"}
        for ci, label in columns.items():
            result[("tables", 0, "columns", ci, "label")] = label
    elif ("metric" in e or "current" in e) and not e.get("kind"):
        sections = [e["current"], e["baseline"]] if "current" in e else [e]
        for ti, section in enumerate(sections):
            labels = (
                ["Date", "Game ID", "Phase", "Team", "Opponent"]
                if section["scope"]["operation"] == "game_log"
                else [
                    "Player",
                    "Value",
                    "Games in scope",
                    "Games with data",
                    "Rank",
                    "Percentile",
                ]
            )
            for ci, label in enumerate(labels):
                if payload.get("reference_evidence") and ci == 1:
                    continue
                result[("tables", ti, "columns", ci, "label")] = label
    if (
        payload.get("reference_evidence")
        and payload.get("tables", [{}])[0].get("reference_row_index") is not None
    ):
        result[("tables", 0, "rows", len(e["rows"]), 6)] = "0.0"
    return result


def bind_contexts(payload):
    from app.agent.claims import bind

    for destination, (sources, identity, scope, context, spec) in contracts(
        payload
    ).items():
        bind(
            payload,
            sources=sources,
            destination=list(destination),
            metric="context",
            unit="label",
            identity=identity,
            scope=scope,
            context=context,
            spec=spec,
        )
