"""Concise, deterministic relationships over governed overview evidence."""

from app.agent.overview_observations import NAMES, changed, comparison_change, complete

VERSION = "ask_overview_insights/4"
ACTIVITY_PRIORITY = {"reb": 3, "stl": 2, "blk": 1}


def qualified_peer(metric):
    """Require the same complete, versioned cohort used by the displayed rank."""
    if "peer_comparison" in metric:
        p = metric["peer_comparison"]
        return (
            complete(metric)
            and p["mode"] != "withheld"
            and p["distribution"]["among_players"]["focal_in_cohort"]
            and p["distribution"]["among_players"]["summary"]["n"] == p["cohort_size"]
        )
    policy = metric.get("percentile_policy", {})
    cohort = metric.get("distribution", {}).get("among_players", {})
    summary = cohort.get("summary", {})
    return (
        complete(metric)
        and metric.get("percentile_status") == "ranked"
        and policy.get("version") == "ask_overview_percentiles/1"
        and cohort.get("focal_in_cohort")
        and summary.get("n", 0) == metric.get("cohort_size")
        and summary.get("n", 0) >= 2
        and metric.get("percentile") is not None
    )


def peer_context(metric, phase):
    if not qualified_peer(metric):
        return ""
    label = NAMES.get(metric["key"], metric.get("label", metric["key"]).lower())
    if "peer_comparison" in metric:
        from app.agent.overview_positions import comparison_text

        return comparison_text(metric, phase, label)
    pct = metric["percentile"]
    # Only exact, unrounded 100 proves all other qualifiers were strictly worse.
    if pct == 100:
        return f"led qualifying {phase} players in {label} per game"
    if metric.get("percentile_policy", {}).get("direction") == "lower":
        return f"had fewer {label} per game than {pct:.1f}% of other qualifying {phase} players"
    if pct > 99.9:
        return f"exceeded 99.9% of other qualifying {phase} players in {label} per game"
    return (
        f"exceeded {pct:.1f}% of other qualifying {phase} players in {label} per game"
    )


def baseline_label(scope):
    if scope.get("previous_start"):
        phases = " + ".join(scope.get("previous_phases", scope.get("phases", [])))
        return f"{phases.lower()} from {scope['previous_start']} through {scope['previous_end']}"
    return scope.get("comparison_label") or "the previous period"


def select_insights(metrics, scope, phase, profile="general"):
    if scope.get("comparison_intent", {}).get("baseline_requested") is False:
        metrics = [{**m, "display_change": None} for m in metrics]
    lookup = {m["key"]: m for m in metrics}
    results = []

    def get(key):
        return lookup.get(key, {})

    def delta(key):
        if scope.get("comparison_intent", {}).get("baseline_requested") is False:
            return None
        return comparison_change(get(key))

    def movement(key):
        d = delta(key)
        if d is None:
            return ""
        if not changed(get(key)):
            return f"{NAMES[key]} stayed broadly similar"
        return f"{NAMES[key]} {'rose' if d > 0 else 'fell'} {abs(d):.1f} per game"

    def add(identifier, area, title, text, summary, keys, peer_keys=()):
        results.append(
            dict(
                id=identifier,
                area=area,
                title=title,
                text=text,
                summary=summary,
                metric_keys=[k for k in keys if complete(get(k))],
                peer_keys=[k for k in peer_keys if qualified_peer(get(k))],
            )
        )

    if profile in ("general", "scoring") and complete(get("pts")):
        m = get("pts")
        p, ts = delta("pts"), delta("ts_pct")
        peer = peer_context(m, phase)
        title = "Scoring production"
        lead = (
            f"He {peer.replace('points per game', 'scoring')} at {m['value']:.1f} points per game"
            if m.get("percentile") == 100 and peer
            else f"He averaged {m['value']:.1f} points per game"
        )
        if p is not None:
            lead += (
                f", {'up' if p > 0 else 'down'} {abs(p):.1f} against his baseline"
                if changed(m)
                else ", broadly in line with his baseline"
            )
        clauses = [lead + "."]
        if peer and m.get("percentile") != 100:
            clauses.append(f"That {peer}.")
        volume = [
            f"{abs(delta(k)):.1f} {'more' if delta(k) > 0 else 'fewer'} {label}"
            for k, label in (
                ("fga", "field-goal attempts"),
                ("fta", "free-throw attempts"),
            )
            if changed(get(k))
        ]
        if volume:
            clauses.append("That came with " + " and ".join(volume) + " per game.")
        efficiency = ""
        if ts is not None:
            current = get("ts_pct")["value"]
            # TS movement is in percentage points, not a relative-percent gate.
            direction = (
                "similar"
                if abs(ts) < 0.5
                else "slightly lower"
                if -1 < ts <= -0.5
                else "lower"
                if ts < 0
                else "slightly higher"
                if ts < 1
                else "higher"
            )
            efficiency = f"True shooting was {direction} ({current - ts:.1f}% to {current:.1f}%)."
            clauses.append(efficiency)
        if changed(m):
            title = "Scoring rose" if p > 0 else "Scoring fell"
            if p > 0 and any(
                delta(k) is not None and changed(get(k)) and delta(k) > 0
                for k in ("fga", "fta")
            ):
                title = "More scoring, greater shot volume"
            if p > 0 and ts is not None and ts >= 0.5:
                title = "Scoring and efficiency rose"
        if (
            changed(m)
            and volume
            and p > 0
            and ts is not None
            and ts <= -0.5
            and m.get("percentile") != 100
        ):
            clauses[0] = (
                f"His scoring rose alongside greater shot volume, despite lower efficiency ({m['value']:.1f} points per game, up {p:.1f} against his baseline)."
            )
        d = delta("pts_per36")
        if d is not None and p is not None and changed(m) and changed(get("pts_per36")):
            rate = get("pts_per36")["value"]
            clause = f"Scoring {'rose' if d > 0 else 'fell'} per 36 minutes ({rate - d:.1f} to {rate:.1f})"
            if p > 0 and d > 0 and delta("min") is not None and delta("min") > 0:
                clause += ", so extra playing time was only part of the increase"
            elif p * d < 0:
                clause += ", the opposite direction to his per-game output"
            clauses.append(clause + ".")
        summary = (
            f"{movement('pts').capitalize()} against his baseline. "
            if p is not None
            else f"{m['value']:.1f} points per game. "
        )
        if volume and efficiency:
            summary = (
                summary.rstrip().removesuffix(".")
                + f", alongside {volume[0]} per game. "
            )
        if (
            d is not None
            and p is not None
            and changed(m)
            and changed(get("pts_per36"))
            and p * d < 0
        ):
            summary += f"Scoring {'rose' if d > 0 else 'fell'} per 36 minutes, the opposite direction to per-game output. "
        summary += efficiency or (
            f"{volume[0].capitalize()} per game."
            if volume
            else (f"He {peer}." if peer else "")
        )
        add(
            "scoring_context",
            "Scoring",
            title,
            " ".join(clauses),
            summary.strip(),
            ("pts", "ts_pct", "min", "fga", "fta", "pts_per36"),
            ("pts",) if peer else (),
        )

    if profile in ("general", "contribution", "playmaking") and any(
        complete(get(k)) for k in ("ast", "tov")
    ):
        a, t = delta("ast"), delta("tov")
        peer = peer_context(get("ast"), phase)
        title = "Assists alongside ball security"
        if changed(get("ast")) and changed(get("tov")):
            title = f"{'More' if a > 0 else 'Fewer'} assists, {'more' if t > 0 else 'fewer'} turnovers"
        clauses = []
        if complete(get("ast")):
            ast = get("ast")["value"]
            clause = (
                f"His {ast:.1f} assists per game {peer.removesuffix(' in assists per game')}"
                if peer and get("ast")["percentile"] != 100
                else f"He {peer} with {ast:.1f} assists per game"
                if peer
                else f"He recorded {ast:.1f} assists per game"
            )
            if a is not None:
                connector = (
                    ", yet"
                    if peer
                    and get("ast")["percentile"] >= 75
                    and a < 0
                    and changed(get("ast"))
                    else ", and"
                )
                clause += connector + (
                    f" assists {'rose' if a > 0 else 'fell'} {abs(a):.1f} against his baseline"
                    if changed(get("ast"))
                    else " assists stayed broadly similar to his baseline"
                )
            clauses.append(clause + ".")
        if complete(get("tov")):
            clauses.append(
                (
                    f"Turnovers {'rose' if t > 0 else 'fell'} {abs(t):.1f} to {get('tov')['value']:.1f} per game."
                    if changed(get("tov"))
                    else f"He recorded {get('tov')['value']:.1f} turnovers per game."
                )
            )
        rate, d = get("ast_per36"), delta("ast_per36")
        if a is not None and changed(get("ast")) and d is not None:
            direction = "rose" if d > 0 else "fell" if d < 0 else "stayed flat"
            clause = f"Assists {direction} per 36 minutes ({rate['value'] - d:.1f} to {rate['value']:.1f})"
            if a * d < 0:
                clause += ", the opposite direction to his per-game output"
                title = "Assist volume and rate diverged"
            clauses.append(clause + ".")
        moves = [movement(k) for k in ("ast", "tov") if movement(k)]
        summary = (
            ("; ".join(moves).capitalize() + " against his baseline.")
            if moves
            else " ".join(clauses[:2])
        )
        if a is not None and d is not None and a * d < 0:
            summary += f" Assists per 36 {'rose' if d > 0 else 'fell'} {abs(d):.1f}."
        elif peer and moves:
            summary += f" He {peer}."
        add(
            "creation_context",
            "Creation",
            title,
            " ".join(clauses),
            summary,
            ("ast", "tov", "ast_per36"),
            ("ast",) if peer else (),
        )

    candidates = []
    activity_keys = (
        ()
        if profile in ("scoring", "playmaking")
        else ("reb",)
        if profile == "rebounding"
        else ("stl", "blk")
        if profile == "defense"
        else ACTIVITY_PRIORITY
    )
    for key in activity_keys:
        m = get(key)
        if not complete(m):
            continue
        peer = peer_context(m, phase)
        pct = (
            m.get("peer_comparison", {}).get("percentile", m.get("percentile"))
            if peer
            else None
        )
        substantial = changed(m) and abs(delta(key)) >= max(0.5, abs(m["value"]) * 0.1)
        # Low steals/blocks are not adverse defensive grades. Rebounds describe volume.
        notable = pct is not None and (pct >= 75 or (key == "reb" and pct <= 25))
        if substantial or notable or profile in ("rebounding", "defense"):
            candidates.append((substantial, ACTIVITY_PRIORITY[key], key, peer))
    for _, _, key, peer in (
        sorted(candidates, reverse=True)[:2]
        if profile == "defense"
        else [max(candidates)]
        if candidates
        else []
    ):
        m = get(key)
        title = (
            f"{NAMES[key].capitalize()} {'rose' if delta(key) > 0 else 'fell'}"
            if changed(m)
            else f"{NAMES[key].capitalize()} among qualifiers"
        )
        sentence = (
            f"{NAMES[key].capitalize()} {'rose' if delta(key) > 0 else 'fell'} {abs(delta(key)):.1f} to {m['value']:.1f} per game"
            if changed(m)
            else f"He recorded {m['value']:.1f} {NAMES[key]} per game"
        )
        if peer:
            sentence += f"; he {peer}"
        sentence += "."
        add(
            "activity_context_" + key if profile == "defense" else "activity_context",
            "Rebounding" if key == "reb" else "Defensive activity",
            title,
            sentence,
            sentence,
            (key,),
            (key,) if peer else (),
        )
    if not results:
        for key in ACTIVITY_PRIORITY:
            if complete(get(key)):
                sentence = f"He recorded {get(key)['value']:.1f} {NAMES[key]} per game."
                add(
                    "recorded_activity",
                    "Rebounding" if key == "reb" else "Defensive activity",
                    "Recorded production",
                    sentence,
                    sentence,
                    (key,),
                )
                break
    intent = scope.get("comparison_intent", {})
    from app.agent.overview_variation import FOCUS_METRICS, variation

    focus = FOCUS_METRICS[profile]
    if results and intent.get("requested_group"):
        primary = get(focus)
        p = primary.get("peer_comparison", {})
        if qualified_peer(primary):
            from app.agent.historical_positions import LABELS
            from app.agent.overview_positions import ordinal

            group = LABELS[p["group"]]
            label = NAMES[focus]
            peers = p["distribution"]["among_players"]["summary"]
            gap = primary["value"] - peers["median"]
            lead = (
                f"He ranked {'tied ' if p['tied'] else ''}{ordinal(p['rank'])} "
                f"of {p['cohort_size']} qualifying {phase} {group} in {p['season']}, "
                f"at {primary['value']:.1f} {label} per game."
            )
            magnitude = (
                f"That was {abs(gap):.1f} {'above' if gap >= 0 else 'below'} "
                f"the {peers['median']:.1f} peer median."
            )
            rate = p.get("per36")
            if rate:
                rate_gap = rate["value"] - rate["summary"]["median"]
                magnitude += (
                    f" Per 36 minutes, he recorded {rate['value']:.1f} versus "
                    f"the same group's median of {rate['summary']['median']:.1f}"
                )
                if gap > 0 and rate_gap > 0:
                    magnitude += (
                        ", so his advantage remained after adjusting for playing time"
                    )
                elif gap * rate_gap < 0:
                    magnitude += (
                        ", reversing his per-game standing relative to the median"
                    )
                magnitude += "."
            # Existing scoring/creation findings retain explicit baseline drivers.
            # The targeted peer answer must come first, including in the sidebar.
            if intent.get("baseline_requested"):
                results[0]["text"] = lead + " " + magnitude + " " + results[0]["text"]
            else:
                results[0]["text"] = lead + " " + magnitude
                if focus == "ast" and complete(get("tov")):
                    results[0]["text"] += (
                        f" He also recorded {get('tov')['value']:.1f} turnovers per game."
                    )
            results[0].update(
                title=f"{NAMES[focus].capitalize()} among {group}",
                summary=lead + " " + magnitude,
                peer_keys=[focus],
            )
        else:
            group = {"Guard": "guard", "Wing": "wing", "Big": "big-man"}[
                intent["requested_group"]
            ]
            lead = f"The requested {group} comparison is not available for {p.get('season') or 'this sample'}."
            results[0]["text"] = lead + " " + results[0]["text"]
    if results:
        v = variation(get(focus))
        if v:
            results[0]["game_variation"] = v
    return results


def scope_context(metrics, scope, appearances, phase, findings):
    parts = [f"{appearances} {phase} appearances"]
    if scope.get("comparison_intent", {}).get(
        "baseline_requested"
    ) is not False and any(comparison_change(m) is not None for m in metrics):
        count = scope.get("previous_appearances")
        parts.append(
            f"baseline: {str(count) + ' appearances, ' if count is not None else ''}{baseline_label(scope)}"
        )
    groups = {}
    selected = {k for f in findings for k in f.get("peer_keys", [])}
    for m in sorted(metrics, key=lambda m: m["key"]):
        if m["key"] not in selected:
            continue
        if "peer_comparison" in m:
            p = m["peer_comparison"]
            label = {
                "Guard": "guards",
                "Wing": "wings/forwards",
                "Big": "bigs/centers",
            }.get(p["group"], "players league-wide")
            group = (p["cohort_size"], p["qualification"]["min_games"], None, label)
            groups.setdefault(group, []).append(NAMES.get(m["key"], m["key"]))
            continue
        p = m["percentile_policy"]
        group = (
            m["cohort_size"],
            p["min_games"],
            p.get("min_attempts"),
            p.get("attempt_field"),
        )
        groups.setdefault(group, []).append(NAMES.get(m["key"], m["key"]))
    for (n, minimum, attempts, field), labels in groups.items():
        parts.append(
            f"{', '.join(labels)}: {n} qualifying {phase} {field if attempts is None and field else 'players'} with at least {minimum} appearances"
            + (f" and {attempts} {field.upper()}" if attempts else "")
        )
    if appearances < 5:
        parts.append("small sample")
    return "Scope: " + " · ".join(parts) + "."


def insight_paragraphs(name, metrics, scope, appearances, phase, profile):
    findings = select_insights(metrics, scope, phase, profile)
    if not findings:
        return [
            f"This assessment covers {name}'s {appearances} {phase} appearances. No complete statistics are available for this assessment."
        ]
    if appearances < 5:
        keys = {
            "scoring": ("pts", "ts_pct"),
            "playmaking": ("ast", "tov"),
            "rebounding": ("reb",),
            "defense": ("stl", "blk"),
            "contribution": ("ast", "reb"),
        }.get(profile, ("pts", "reb", "ast"))
        lookup = {m["key"]: m for m in metrics}
        values = [
            f"{lookup[k]['value']:.1f} {NAMES.get(k, k)}"
            for k in keys
            if complete(lookup.get(k, {})) and k != "ts_pct"
        ]
        return [
            f"{name} averaged {', '.join(values)} per game across these {appearances} appearances."
            + (
                " The requested position comparison needs at least five appearances in the same dates."
                if scope.get("comparison_intent", {}).get("requested_group")
                else ""
            ),
            scope_context(metrics, scope, appearances, phase, []),
        ]
    paragraphs = []
    for f in findings:
        if f["id"].startswith("activity_context") and paragraphs:
            paragraphs[-1] += " " + f["text"]
        else:
            paragraphs.append(f["text"])
    if paragraphs[0].startswith("His "):
        paragraphs[0] = name + "'s " + paragraphs[0][4:]
    elif paragraphs[0].startswith("He "):
        paragraphs[0] = name + " " + paragraphs[0][3:]
    elif paragraphs[0].startswith("The requested "):
        paragraphs[0] = (
            paragraphs[0]
            .replace(" He ", " " + name + " ", 1)
            .replace(" His ", " " + name + "'s ", 1)
        )
    v = findings[0].get("game_variation")
    if v:
        paragraphs.append(v["text"])
    return paragraphs + [scope_context(metrics, scope, appearances, phase, findings)]
