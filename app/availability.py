"""Pair-independent, read-only availability comparisons from immutable evidence."""

from collections import Counter, defaultdict
from functools import lru_cache
from math import isfinite
from pathlib import Path
from statistics import median

from app.agent.semantic_source import snapshot_evidence
from app.agent.semantics import SemanticError, aggregate, load_contract
from app.agent.teammate_readiness import latest_report
from app.research_snapshots import read_snapshot


@lru_cache(maxsize=2)
def _load(path, modified, size):
    document = read_snapshot(Path(path))
    return prepare_availability(document)


def prepare_availability(document):
    """Validate source joins before publication as well as on serving reads."""
    if document.get("artifact_type") != "availability_evidence/v1":
        raise ValueError("Unsupported availability evidence")
    evidence = snapshot_evidence(document["stats"])
    games = {}
    for game in document["games"]:
        key = (game["season"], game["game_id"], game["team_abbr"])
        if key in games:
            raise ValueError("Duplicate team schedule")
        games[key] = game
    reports = defaultdict(list)
    for report in document["reports"]:
        if report["team_abbr"] not in report["matchup"].split("@"):
            raise ValueError(
                "Injury report team does not belong to its matchup; rebuild evidence"
            )
        reports[(report["season"], report["game_date"], report["team_abbr"])].append(
            report
        )
    stats = {(r["season"], r["game_id"], r["player_id"]): r for r in evidence.rows}
    for row in evidence.rows:
        game = games.get((row["season"], row["game_id"], row["team_abbr"]))
        if (
            not game
            or row["game_date"] != game["game_date"]
            or row["season_type"] != game["season_type"]
        ):
            raise ValueError("Stats and schedule disagree")
    seen = set()
    for record in document.get("participation", []):
        key = (record["season"], record["game_id"], record["player_id"])
        game = games.get((record["season"], record["game_id"], record["team_abbr"]))
        if key in seen or not game or not game["final"] or not record.get("source_url"):
            raise ValueError("Invalid participation evidence")
        if (
            type(record["minutes"]) not in (int, float)
            or not isfinite(record["minutes"])
            or record["minutes"] < 0
            or type(record["player_id"]) is not int
            or record["player_id"] <= 0
        ):
            raise ValueError("Participation requires nonnegative final minutes")
        existing = stats.get(key)
        if existing and (
            existing["team_abbr"] != record["team_abbr"]
            or (
                existing.get("min") is not None
                and (existing["min"] > 0) != (record["minutes"] > 0)
            )
        ):
            raise ValueError("Participation and statistics disagree")
        seen.add(key)
    return document, evidence, games, reports, stats


def load_availability(path):
    if not path:
        raise SemanticError(
            "availability_missing",
            "Verified game and availability evidence is not connected. I cannot apply the teammate condition, so no overall statistics were substituted.",
        )
    file = Path(path)
    stat = file.stat()
    return _load(str(file.resolve()), stat.st_mtime_ns, stat.st_size)


def compare_availability(loaded, request):
    document, evidence, games, reports, index = loaded
    focal, teammate = request["player_id"], request["teammate_id"]
    if focal == teammate:
        raise SemanticError("invalid_scope", "Choose two different players.")
    phases = (
        ["Regular Season", "Playoffs"]
        if request["phase"] == "Both"
        else [request["phase"]]
    )
    covered_phases = [
        p
        for p in phases
        if (request["season"], p) in evidence.covered_scopes
        and (request["season"], p) in evidence.data_through
    ]
    missing_phases = [p for p in phases if p not in covered_phases]
    if not covered_phases or (missing_phases and request.get("explicit_phase")):
        raise SemanticError(
            "unsupported_coverage",
            "The source does not cover every explicitly requested season phase. No other phase was substituted.",
        )
    rows = [
        r
        for r in evidence.rows
        if r["season"] == request["season"]
        and r["player_id"] == focal
        and (request["phase"] == "Both" or r["season_type"] == request["phase"])
        and (not request["start"] or r["game_date"] >= request["start"])
        and (not request["end"] or r["game_date"] <= request["end"])
    ]
    if not rows:
        raise SemanticError(
            "unsupported_coverage",
            "No focal-player appearances are available for that exact season and date range.",
        )
    request = {"include_limited_minutes": False, "minutes_threshold": 0.5, **request}
    threshold = request["minutes_threshold"]
    if type(threshold) not in (int, float) or not 0 < threshold <= 1:
        raise SemanticError(
            "invalid_scope", "Minutes threshold must be above 0% and at most 100%."
        )
    if type(request["include_limited_minutes"]) is not bool:
        raise SemanticError(
            "invalid_scope", "Include limited minutes must be true or false."
        )
    participation = {
        (r["season"], r["game_id"], r["player_id"]): r
        for r in document.get("participation", [])
    }
    histories = {}
    for pid in (focal, teammate):
        histories[pid] = sorted(
            [
                r
                for r in evidence.rows
                if r["season"] == request["season"]
                and r["player_id"] == pid
                and r.get("min") is not None
                and r["min"] > 0
                and games[(r["season"], r["game_id"], r["team_abbr"])]["final"]
                and not games[(r["season"], r["game_id"], r["team_abbr"])].get(
                    "postponed"
                )
            ],
            key=lambda r: (r["game_date"], r["game_id"]),
        )

    def minutes_check(pid, minutes, date):
        prior = [r for r in histories[pid] if r["game_date"] < date][-10:]
        baseline = median(r["min"] for r in prior) if len(prior) >= 5 else None
        return dict(
            player_id=pid,
            minutes=minutes,
            baseline_minutes=baseline,
            baseline_game_ids=[r["game_id"] for r in prior],
            cutoff_minutes=baseline * threshold if baseline is not None else None,
            status="insufficient_baseline"
            if baseline is None
            else "limited_minutes"
            if minutes < baseline * threshold
            else "usual_minutes",
        )

    groups = {"both_played": [], "did_not_play": []}
    sources, details = {}, {}
    excluded = Counter()
    excluded_games = []
    scope_rows = []
    for row in rows:
        game = games[(row["season"], row["game_id"], row["team_abbr"])]
        if request.get("home_away") and game["home_away"] != request["home_away"]:
            continue
        if request.get("opponent") and game["opponent_abbr"] != request["opponent"]:
            continue
        scope_rows.append(row)
        key = (row["season"], row["game_id"], teammate)
        other = index.get(key)
        final = participation.get(key)
        other_minutes = other.get("min") if other else None
        if other_minutes is None and final:
            other_minutes = final["minutes"]
        other_team = (
            other["team_abbr"] if other else final["team_abbr"] if final else None
        )
        report = latest_report(
            reports.get((row["season"], row["game_date"], row["team_abbr"]), []),
            game,
            teammate,
            48,
        )
        detail = dict(
            game_id=row["game_id"],
            game_date=row["game_date"],
            opponent=game["opponent_abbr"],
            focal_minutes=row.get("min"),
            teammate_minutes=other_minutes,
            report_status=report["status"],
            participation="unknown",
            minutes_checks=[],
            warnings=[],
        )
        reason, group = None, None
        if not game["final"] or game.get("postponed"):
            reason = "unfinished_game"
        elif row.get("min") is None:
            reason = "unknown_focal_minutes"
        elif row["min"] <= 0:
            reason = "focal_did_not_play"
        elif other_team and other_team != row["team_abbr"]:
            reason = "not_teammates"
        elif other_minutes is not None and other_minutes > 0:
            group = "both_played"
            if report["status"] in ("Out", "Conflicting"):
                detail["warnings"].append(
                    "Recorded minutes override the pregame report"
                )
        elif other_minutes == 0 and other_team == row["team_abbr"]:
            group = "did_not_play"
        elif other is None and final is None and report["status"] == "Out":
            group = "did_not_play"
        else:
            reason = "unverified_status_or_membership"
        if group:
            detail["participation"] = group
            basis = (
                "stats_positive_minutes"
                if other and other.get("min") is not None and other["min"] > 0
                else "stats_zero_minutes"
                if other and other.get("min") == 0
                else "final_boxscore"
                if final
                else "out_report"
            )
            detail["classification_basis"] = basis
            detail["classification_evidence"] = (
                dict(
                    snapshot_id=evidence.snapshot_id,
                    season=row["season"],
                    game_id=row["game_id"],
                    player_id=teammate,
                )
                if basis.startswith("stats_")
                else {}
            )
            sources[row["game_id"]] = (
                [final["source_url"]]
                if basis == "final_boxscore"
                else report["sources"]
                if basis == "out_report"
                else []
            )
            detail["minutes_checks"] = [
                minutes_check(focal, row["min"], row["game_date"])
            ]
            if group == "both_played":
                detail["minutes_checks"].append(
                    minutes_check(teammate, other_minutes, row["game_date"])
                )
            if not request["include_limited_minutes"] and any(
                c["status"] == "limited_minutes" for c in detail["minutes_checks"]
            ):
                reason = "limited_minutes"
        detail["source_urls"] = sources.get(row["game_id"], report["sources"])
        if reason:
            excluded[reason] += 1
            excluded_games.append({**detail, "reason": reason})
        else:
            groups[group].append(row)
            details[row["game_id"]] = detail
    if not scope_rows:
        raise SemanticError(
            "unsupported_coverage", "No appearances match these filters."
        )
    contract = load_contract()
    metrics = []
    for key in request["metrics"]:
        metric = contract.metric(key)
        aggregation = "ratio" if metric.denominator else request["aggregation"]
        values = {
            name: aggregate(sample, metric, aggregation)
            for name, sample in groups.items()
        }
        a, b = values["both_played"], values["did_not_play"]
        difference = None
        if all(
            v["value"] is not None and not v["missing_component_games"]
            for v in values.values()
        ):
            difference = (b["value"] - a["value"]) * (
                100 if metric.unit == "ratio" else 1
            )
        metrics.append(
            dict(
                metric=key,
                label=metric.label,
                unit="%" if metric.unit == "ratio" else metric.unit,
                aggregation=aggregation,
                groups=values,
                difference=difference,
                difference_unit="percentage points"
                if metric.unit == "ratio"
                else metric.unit,
            )
        )
    attribution = {
        name: [
            dict(
                player_id=focal,
                teammate_id=teammate,
                team_abbr=r["team_abbr"],
                phase=r["season_type"],
                **details[r["game_id"]],
            )
            for r in sample
        ]
        for name, sample in groups.items()
    }
    return dict(
        policy_version="availability/2",
        covered_phases=covered_phases,
        missing_phases=missing_phases,
        request=request,
        excluded_games=excluded_games,
        policy=dict(
            baseline_appearances=10,
            minimum_baseline_appearances=5,
            minutes_threshold=threshold,
            include_limited_minutes=request["include_limited_minutes"],
        ),
        metrics=metrics,
        groups=attribution,
        excluded=dict(excluded),
        scope_appearances=len(scope_rows),
        snapshot_id=document["sha256"],
        stats_snapshot_id=evidence.snapshot_id,
        source_through=max(r["game_date"] for r in scope_rows),
        observed_start=min(r["game_date"] for r in scope_rows),
        rows=[r for sample in groups.values() for r in sample],
    )
