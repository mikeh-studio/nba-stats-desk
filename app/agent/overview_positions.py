"""Versioned position evidence. Never infer historical positions from box scores."""

import hashlib
import json
import re

from app.agent.historical_positions import (
    GROUPS,
    LABELS,
    MAPPING_VERSION,
    digest,
    season_valid,
)
from app.agent.metric_distributions import distribution
from app.agent.semantics import aggregate, load_contract

VERSION = "ask_position_peers/3"
POSITION_FIRST = {"pts", "ast", "reb", "fg3m", "stl", "blk"}
PATTERN = r"\b(?:other\s+|among\s+(?:other\s+)?|against\s+(?:other\s+)?)?(guards|wings|big men|bigs)\b"


def requested_groups(question):
    found = {m.group(1).lower() for m in re.finditer(PATTERN, question, re.I)}
    groups = {
        "Guard" if x == "guards" else "Wing" if x == "wings" else "Big" for x in found
    }
    return sorted(groups)


def requested_group(question):
    groups = requested_groups(question)
    return groups[0] if len(groups) == 1 else None


BASELINE_CLAUSE = re.compile(
    r"\b(?:compared?|comparing)\s+(?:with|to|against)\s+"
    r"(?:(?:his|the|their)\s+)?"
    r"(?P<period>(?:prior|previous|last)\s+season|regular[ -]season)\b"
    r"(?:['’]s)?(?:\s+(?P<phase>regular[ -]season|playoffs?|postseason)\b)?",
    re.I,
)


def strip_peer_clause(question):
    return re.sub(
        r"\b(?:compare(?:d)?\s+(?:with|to|against)|v(?:s\.?|ersus)|with|among|against|and)\s+"
        r"(?:other\s+)?(?:guards|wings|big men|bigs)\b",
        "",
        question,
        flags=re.I,
    )


def comparison_intent(question):
    group = requested_group(question)
    baseline = BASELINE_CLAUSE.search(question) if group else None
    return {
        "requested_group": group,
        "requested_groups": requested_groups(question),
        "baseline_period": (
            "same_season"
            if baseline["period"].lower().replace("-", " ") == "regular season"
            else "prior_season"
        )
        if baseline
        else None,
        "baseline_phases": (
            ["Regular Season"]
            if baseline["period"].lower().replace("-", " ") == "regular season"
            or (baseline["phase"] or "").lower().replace("-", " ") == "regular season"
            else ["Playoffs"]
            if baseline["phase"]
            else None
        )
        if baseline
        else None,
        "baseline_requested": bool(
            baseline
            or re.search(
                r"\b(?:prior|previous|his regular|baseline|usual|normal)\b",
                question,
                re.I,
            )
        )
        if group
        else True,
    }


def peer_question(question):
    """Normalize only a position-comparison clause, never another player's name."""
    if not requested_group(question):
        return question
    text = strip_peer_clause(BASELINE_CLAUSE.sub("", question))
    if re.search(r"\b(?:with|and|versus|vs)\b", text, re.I):
        return text
    return re.sub(r"^\s*compare\s+", "Give me an overview of ", text, flags=re.I)


def validated_context(context):
    """Only operator-reviewed, season-effective bundles can enable grouping.

    Bundle integrity is covered by the containing snapshot digest. Approval is
    an operator assertion backed by retained source/audit evidence, not inferred
    from a season label. Unapproved or malformed optional evidence fails closed.
    """
    if (
        not isinstance(context, dict)
        or context.get("mapping_version") != MAPPING_VERSION
        or context.get("sha256") != digest(context)
    ):
        return {}, set()
    approved = context.get("approved_seasons", [])
    if not isinstance(approved, list) or any(not season_valid(s) for s in approved):
        return {}, set()
    records = context.get("records", [])
    if not isinstance(records, list) or len(records) > 5000:
        return {}, set()
    result = {}
    seen = set()
    for r in records:
        if not isinstance(r, dict):
            return {}, set()
        key = (r.get("season"), r.get("player_id"))
        if not isinstance(key[0], str) or type(key[1]) is not int or key in seen:
            return {}, set()
        seen.add(key)
        if (
            r.get("provenance") not in ("season_roster", "dated_team_document")
            or not re.fullmatch(r"[0-9a-f]{64}", str(r.get("source_sha256", "")))
            or not isinstance(r.get("source_locator"), str)
            or not r["source_locator"]
            or not re.fullmatch(r"20\d{2}-\d{2}-\d{2}", str(r.get("source_date", "")))
            or not isinstance(r.get("source_ref"), str)
            or not r["source_ref"].startswith("https://")
        ):
            continue
        position = r.get("position")
        result[key] = GROUPS.get(position) if isinstance(position, str) else None
    return result, set(approved)


def standing(cohort, player_id, direction):
    value = cohort[player_id]["value"]
    values = [s["value"] for s in cohort.values()]
    better = sum(v < value if direction == "lower" else v > value for v in values)
    worse = sum(v > value if direction == "lower" else v < value for v in values)
    return dict(
        rank=1 + better,
        tied=values.count(value) > 1,
        all_equal=len(set(values)) == 1,
        percentile=100 * worse / (len(values) - 1) if len(values) > 1 else None,
    )


def add_position_comparisons(metrics, grouped, player_id, scope, context, question):
    positions, approved = validated_context(context)
    seasons = {r["season"] for rows in grouped.values() for r in rows}
    season = next(iter(seasons)) if len(seasons) == 1 else None
    own_group = positions.get((season, player_id))
    requested = requested_group(question)
    contract = load_contract()
    for record in metrics:
        key = record["key"]
        if key not in POSITION_FIRST:
            continue
        metric = contract.metric(key)
        minimum = record["percentile_policy"]["min_games"]
        summaries = {
            pid: aggregate(rows, metric, "average") for pid, rows in grouped.items()
        }
        eligible = {
            pid: s
            for pid, s in summaries.items()
            if s["value"] is not None
            and not s["missing_component_games"]
            and s["valid_games"] >= minimum
        }
        mapped = {pid: positions.get((season, pid)) for pid in eligible}
        coverage = (
            sum(g is not None for g in mapped.values()) / len(eligible)
            if eligible
            else 0
        )
        target = requested or (
            own_group[0] if own_group and len(own_group) == 1 else None
        )
        cohort = {
            pid: s
            for pid, s in eligible.items()
            if mapped[pid] and target in mapped[pid]
        }
        reason = (
            "ineligible_sample"
            if player_id not in eligible
            else "multiple_seasons"
            if len(seasons) != 1
            else "missing_historical_provenance"
            if season not in approved
            else "ambiguous_position"
            if own_group is None or (not requested and len(own_group) != 1)
            else "group_mismatch"
            if requested and requested not in own_group
            else "insufficient_coverage"
            if coverage < 0.95
            else "too_few_peers"
            if len(cohort) < 10
            else None
        )
        selected = cohort if reason is None else eligible
        kind = "position" if reason is None else "league"
        # Preserve an unambiguous league-wide leader under a fixed rule, unless
        # the user explicitly requested their position group.
        if (
            reason is None
            and requested is None
            and record.get("percentile") == 100
            and len(eligible) >= 20
        ):
            selected, kind = eligible, "league"
        if requested and reason is not None:
            # A league cohort cannot answer an explicitly requested position.
            selected, kind = {}, "unavailable"
        n = len(selected)
        stats = (
            standing(selected, player_id, metric.direction)
            if player_id in selected
            else {}
        )
        mode = "percentile" if n >= 20 else "ordinal" if n >= 10 else "withheld"
        # Do not create praise/adverse claims when every eligible value is equal.
        if not stats or stats.get("all_equal"):
            mode = "withheld"
        record["peer_comparison"] = dict(
            version=VERSION,
            mapping_version=MAPPING_VERSION,
            kind=kind,
            group=target if kind in ("position", "unavailable") else None,
            requested_group=requested,
            season=season,
            start=str(scope["start"]),
            end=str(scope["end"]),
            phases=scope["phases"],
            cohort_size=n,
            membership_sha256=hashlib.sha256(
                json.dumps(sorted(selected)).encode()
            ).hexdigest(),
            mode=mode,
            coverage=coverage,
            eligible_players=len(eligible),
            classified_players=sum(g is not None for g in mapped.values()),
            membership_policy="listed_positions_overlap",
            evidence_id=context.get("sha256") if isinstance(context, dict) else None,
            fallback_reason=reason,
            qualification=dict(min_games=minimum, complete_components=True),
            **stats,
            per36=rate_reference(selected, grouped, player_id, key),
            distribution=distribution(
                metric, selected, grouped.get(player_id, []), player_id
            ),
        )


def ordinal(n):
    return str(n) + (
        "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    )


def comparison_text(metric, phase, label):
    p = metric.get("peer_comparison")
    if not p or p["mode"] == "withheld":
        return ""
    n = p["cohort_size"]
    group = LABELS.get(p["group"], "players league-wide")
    scope = f"{n} qualifying {phase} {group}"
    if p["mode"] == "ordinal":
        return f"ranked {'tied ' if p['tied'] else ''}{ordinal(p['rank'])} of {scope} in {label} per game"
    if p["percentile"] == 100:
        return f"led {scope} in {label} per game"
    return f"exceeded {min(p['percentile'], 99.9):.1f}% of other qualifying {phase} {group} in {label} per game"


def rate_reference(selected, grouped, player_id, key):
    """Exposure context over exactly the selected cohort; never expand membership."""
    if player_id not in selected:
        return None
    rates = {}
    for pid in selected:
        rows = grouped[pid]
        if any(r.get("min") is None for r in rows):
            return None
        minutes = sum(r["min"] for r in rows)
        if minutes <= 0:
            return None
        rates[pid] = 36 * sum(r[key] for r in rows) / minutes
    from app.agent.metric_distributions import describe

    return {
        "value": rates[player_id],
        "cohort_size": len(rates),
        "summary": describe(list(rates.values())),
    }
