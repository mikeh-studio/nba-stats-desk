"""Roster-scoped, retrospective teammate comparisons; no causal estimates."""

from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from statistics import mean, stdev

from app.agent.context_metrics import context_metrics

GROUPS = ("participated", "reported_out_no_appearance")


def utc(value):
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("Timestamp must have a timezone")
    return stamp.astimezone(timezone.utc)


def validate_memberships(rows):
    """Reviewed intervals are inclusive at start, exclusive at end; gaps stay unknown."""
    by_player = defaultdict(list)
    for row in rows:
        start, end = (date.fromisoformat(row[k]) for k in ("valid_from", "valid_to"))
        if start >= end or not row["source_urls"] or not row["basis"]:
            raise ValueError("Membership needs a bounded interval and source evidence")
        if any(not u.startswith("https://") for u in row["source_urls"]):
            raise ValueError("Membership source URL must be HTTPS")
        utc(row["reviewed_at"])
        for stamp in row["source_published_at"]:
            if len(stamp) == 10:
                date.fromisoformat(stamp)
            else:
                utc(stamp)
        if len(row["source_published_at"]) != len(row["source_urls"]):
            raise ValueError("Every membership source needs a publication timestamp")
        by_player[row["season"], row["player_id"]].append(row)
    for intervals in by_player.values():
        intervals.sort(key=lambda r: r["valid_from"])
        if any(
            a["valid_to"] > b["valid_from"] for a, b in zip(intervals, intervals[1:])
        ):
            raise ValueError("Overlapping membership intervals")


def schedule_games(document, season, team):
    if document["leagueSchedule"]["seasonYear"] != season:
        raise ValueError("Schedule season mismatch")
    games = []
    seen = set()
    prefix = "002" + season[2:4]
    for day in document["leagueSchedule"]["gameDates"]:
        for g in day["games"]:
            home, away = (g[k]["teamTricode"] for k in ("homeTeam", "awayTeam"))
            if team not in (home, away) or not g["gameId"].startswith(prefix):
                continue
            if g["gameId"] in seen:
                raise ValueError("Duplicate schedule game")
            seen.add(g["gameId"])
            games.append(
                {
                    "season": season,
                    "game_id": g["gameId"],
                    "game_date": g["gameDateEst"][:10],
                    "team_abbr": team,
                    "opponent_abbr": away if team == home else home,
                    "home_away": "home" if team == home else "away",
                    "scheduled_start_utc": g.get("gameDateTimeUTC"),
                    "final": g["gameStatus"] == 3,
                    "postponed": g.get("postponedStatus", "N") != "N",
                }
            )
    return sorted(games, key=lambda g: (g["game_date"], g["game_id"]))


def latest_report(reports, game, player_id, max_age_hours):
    if not game["scheduled_start_utc"] or game["postponed"]:
        return {
            "status": "Unknown",
            "sources": [],
            "reason": "missing_or_postponed_start",
        }
    cutoff = utc(game["scheduled_start_utc"])
    candidates = []
    matchup = (
        f"{game['opponent_abbr']}@{game['team_abbr']}"
        if game["home_away"] == "home"
        else f"{game['team_abbr']}@{game['opponent_abbr']}"
    )
    for r in reports:
        if (
            r.get("season"),
            r.get("game_date"),
            r.get("team_abbr"),
            r.get("matchup"),
        ) != (game["season"], game["game_date"], game["team_abbr"], matchup):
            continue
        stamp = utc(r["report_timestamp_utc"])
        age = (cutoff - stamp).total_seconds() / 3600
        if 0 < age <= max_age_hours:
            if not r.get("source_url") or not r.get("ingested_at_utc"):
                raise ValueError("Report provenance missing")
            utc(r["ingested_at_utc"])
            candidates.append((stamp, r))
    if not candidates:
        return {"status": "Unknown", "sources": [], "reason": "no_eligible_report"}
    stamp = max(t for t, _ in candidates)
    # Select the team bulletin first: omissions must not revive old listings.
    selected = [
        r for t, r in candidates if t == stamp and r.get("player_id") == player_id
    ]
    if not selected:
        return {
            "status": "Unknown",
            "sources": sorted({r["source_url"] for t, r in candidates if t == stamp}),
            "reported_at": stamp.isoformat(),
            "reason": "not_listed_in_latest_bulletin",
        }
    statuses = {r["injury_status"] for r in selected}
    return {
        "status": next(iter(statuses)) if len(statuses) == 1 else "Conflicting",
        "reported_at": stamp.isoformat(),
        "age_hours": (cutoff - stamp).total_seconds() / 3600,
        "sources": sorted({r["source_url"] for r in selected}),
        "ingested_at": sorted({r["ingested_at_utc"] for r in selected}),
        "reported_reasons": sorted({r.get("reason", "") for r in selected}),
    }


def build_panel(stats, reports, games, memberships, spec, context=()):
    validate_memberships(memberships)
    if spec["player_id"] == spec["teammate_id"] or spec["start"] > spec["end"]:
        raise ValueError("Invalid pair or study dates")
    if not 0 < spec["max_report_age_hours"] <= 48:
        raise ValueError("Report age must be between zero and 48 hours")
    lookup = {}
    for row in stats:
        key = (row["season"], row["game_id"], row["player_id"])
        if key in lookup:
            raise ValueError("Duplicate appearance key")
        lookup[key] = row
    opponents = {}
    names = {}
    for pid in (spec["player_id"], spec["teammate_id"]):
        observed = {
            r["player_name"].strip()
            for r in stats
            if r["season"] == spec["season"]
            and r["player_id"] == pid
            and isinstance(r.get("player_name"), str)
            and r["player_name"].strip()
        }
        names[pid] = next(iter(observed)) if len(observed) == 1 else None
    for row in context:
        key = (row["season"], row["game_id"], row["player_id"])
        if key in opponents:
            raise ValueError("Duplicate context key")
        if (
            row.get("opponent_latest_prior_game_date", "")
            and row["opponent_latest_prior_game_date"] >= row["game_date"]
        ):
            raise ValueError("Future opponent context")
        opponents[key] = row
    selected = [
        g
        for g in games
        if g["season"] == spec["season"]
        and g["team_abbr"] == spec["team_abbr"]
        and spec["start"] <= g["game_date"] <= spec["end"]
    ]
    if not selected or len({g["game_id"] for g in selected}) != len(selected):
        raise ValueError("Empty or duplicate study schedule")
    selected.sort(key=lambda g: (g["game_date"], g["game_id"]))
    panel = []
    for game in selected:
        own, other = [
            lookup.get((game["season"], game["game_id"], pid))
            for pid in (spec["player_id"], spec["teammate_id"])
        ]
        for appearance in (own, other):
            if appearance and (
                appearance["game_date"] != game["game_date"]
                or appearance["season_type"] != "Regular Season"
            ):
                raise ValueError("Appearance/schedule mismatch")
        member = {}
        for pid in (spec["player_id"], spec["teammate_id"]):
            member[pid] = next(
                (
                    r
                    for r in memberships
                    if r["season"] == game["season"]
                    and r["player_id"] == pid
                    and r["valid_from"] <= game["game_date"] < r["valid_to"]
                ),
                None,
            )
        eligibility = "eligible"
        if any(r is None for r in member.values()):
            eligibility = "unknown_membership"
        elif any(r["team_abbr"] != game["team_abbr"] for r in member.values()):
            eligibility = "not_teammates"
        if any(r and r["team_abbr"] != game["team_abbr"] for r in (own, other)):
            eligibility = "conflicting_team_evidence"
        if not game["final"]:
            eligibility = "not_final"
        report = latest_report(
            reports, game, spec["teammate_id"], spec["max_report_age_hours"]
        )
        played = other is not None and other.get("min") is not None and other["min"] > 0
        if eligibility != "eligible":
            group = eligibility
        elif report["status"] == "Conflicting" or (
            played and report["status"] == "Out"
        ):
            group = "conflicting"
        elif played:
            group = "participated"
        elif report["status"] == "Out" and other is None:
            group = "reported_out_no_appearance"
        else:
            group = "unknown"
        own_played = own is not None and own.get("min") is not None and own["min"] > 0
        ctx = opponents.get((game["season"], game["game_id"], spec["player_id"]), {})
        prior_dates = [
            g["game_date"]
            for g in games
            if g["season"] == game["season"]
            and g["team_abbr"] == game["team_abbr"]
            and g["final"]
            and g["game_date"] < game["game_date"]
        ]
        rest = (
            (
                date.fromisoformat(game["game_date"])
                - date.fromisoformat(max(prior_dates))
            ).days
            - 1
            if prior_dates
            else None
        )
        panel.append(
            {
                **game,
                "player_id": spec["player_id"],
                "teammate_id": spec["teammate_id"],
                "focal_player_name": names[spec["player_id"]],
                "teammate_name": names[spec["teammate_id"]],
                "max_report_age_hours": spec["max_report_age_hours"],
                "eligibility": eligibility,
                "exposure": group,
                "focal_participated": own_played,
                "included": own_played and group in GROUPS,
                "membership_evidence": list(member.values()),
                "availability": report,
                "outcomes": own if own_played else None,
                "rest_days": rest,
                "opponent_prior_win_pct": ctx.get("opponent_prior_win_pct"),
                "opponent_prior_efg_allowed_pct": ctx.get(
                    "opponent_prior_efg_allowed_pct"
                ),
            }
        )
    episode = 0
    previous = None
    for row in panel:
        if row["exposure"] != previous:
            episode += 1
        row["episode_id"] = episode
        previous = row["exposure"]
    return panel


def summarize_panel(panel):
    groups = {}
    for group in GROUPS:
        sample = [r for r in panel if r["included"] and r["exposure"] == group]
        outcomes = [r["outcomes"] for r in sample]
        summary = {
            "games": len(sample),
            "episodes": len({r["episode_id"] for r in sample}),
        }
        for key in ("pts", "ast", "min", "fga", "fg3a", "fta", "tov"):
            values = [r[key] for r in outcomes if r.get(key) is not None]
            summary[key] = {
                "mean": mean(values) if values and len(values) == len(sample) else None,
                "valid_games": len(values),
                "sd": stdev(values)
                if len(values) > 1 and len(values) == len(sample)
                else None,
            }
        summary["context_metrics"] = context_metrics(outcomes, [])
        for key in (
            "opponent_prior_win_pct",
            "opponent_prior_efg_allowed_pct",
            "rest_days",
        ):
            values = [r[key] for r in sample if r[key] is not None]
            summary[key] = {
                "mean": mean(values) if values else None,
                "valid_games": len(values),
            }
        summary["home_games"] = sum(r["home_away"] == "home" for r in sample)
        groups[group] = summary
    differences = {}
    for metric in ("pts", "ast", "min"):
        played, out = (groups[g][metric]["mean"] for g in GROUPS)
        sensitivity = []
        for episode in {r["episode_id"] for r in panel if r["included"]}:
            samples = [
                [
                    r["outcomes"].get(metric)
                    for r in panel
                    if r["included"]
                    and r["exposure"] == g
                    and r["episode_id"] != episode
                ]
                for g in GROUPS
            ]
            if all(s and None not in s for s in samples):
                sensitivity.append(mean(samples[1]) - mean(samples[0]))
        differences[metric] = {
            "out_minus_participated": out - played
            if out is not None and played is not None
            else None,
            "leave_one_episode_out_range": [min(sensitivity), max(sensitivity)]
            if sensitivity
            else None,
            "sensitivity_comparisons": len(sensitivity),
        }
    incomplete = sum(r["exposure"] not in GROUPS for r in panel)
    readiness = {
        "unclassified_or_excluded_team_games": incomplete,
        "both_groups_observed": all(groups[g]["games"] > 0 for g in GROUPS),
        "status": "reviewable_descriptive_comparison"
        if not incomplete and all(groups[g]["games"] > 0 for g in GROUPS)
        else "incomplete_descriptive_comparison",
        "causal_ready": False,
    }
    return {
        "claim_level": "descriptive",
        "readiness": readiness,
        "team_games": len(panel),
        "exposure_counts": dict(Counter(r["exposure"] for r in panel)),
        "focal_nonparticipation_games": sum(not r["focal_participated"] for r in panel),
        "included_games": sum(r["included"] for r in panel),
        "groups": groups,
        "differences": differences,
        "uncertainty": "SD describes game variation. Leave-one-episode-out range is sensitivity, not a confidence interval. No causal uncertainty interval is estimated.",
        "limitations": [
            "Retrospective reviewed roster windows, not a complete transaction feed or contemporaneous knowledge.",
            "Latest captured report before scheduled start within the age limit; not guaranteed final pre-tipoff status.",
            "No appearance is not proof of a DNP reason; Out can have a non-injury reason.",
            "Outcomes condition on focal participation; nonparticipation games remain in the panel.",
            "No adjustment for calendar, other absences, opponent, or rest. Co-participation is not shared court time.",
            "Local frozen evidence only; warehouse deployment and public Ask integration are separate.",
        ],
    }
