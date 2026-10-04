"""Pair-independent, read-only availability comparisons from immutable evidence."""

from collections import Counter, defaultdict
from functools import lru_cache
from pathlib import Path

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
    groups = {"both_played": [], "reported_out": []}
    sources = {}
    excluded = Counter()
    scope_rows = []
    for row in rows:
        game = games[(row["season"], row["game_id"], row["team_abbr"])]
        if request.get("home_away") and game["home_away"] != request["home_away"]:
            continue
        if request.get("opponent") and game["opponent_abbr"] != request["opponent"]:
            continue
        scope_rows.append(row)
        other = index.get((row["season"], row["game_id"], teammate))
        report = latest_report(
            reports.get((row["season"], row["game_date"], row["team_abbr"]), []),
            game,
            teammate,
            48,
        )
        if not game["final"] or game.get("postponed"):
            excluded["unfinished_game"] += 1
        elif row.get("min") is None or row["min"] <= 0:
            excluded["focal_did_not_play"] += 1
        elif other and other["team_abbr"] != row["team_abbr"]:
            excluded["not_teammates"] += 1
        elif report["status"] == "Conflicting" or (
            other and other.get("min", 0) and report["status"] == "Out"
        ):
            excluded["conflicting_status"] += 1
        elif other and other.get("min") is not None and other["min"] > 0:
            groups["both_played"].append(row)
        elif other is None and report["status"] == "Out":
            # The game-specific team bulletin verifies membership on this date.
            # Do not extend a roster interval through a trade or infer it from absence.
            groups["reported_out"].append(row)
            sources[row["game_id"]] = report["sources"]
        else:
            excluded["unverified_status_or_membership"] += 1
    if not groups["reported_out"]:
        raise SemanticError(
            "unsupported_coverage",
            "No games meet the requested filters with the teammate verified as reported Out and not appearing. No overall statistics were substituted.",
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
        a, b = values["both_played"], values["reported_out"]
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
                game_id=r["game_id"],
                game_date=r["game_date"],
                player_id=focal,
                teammate_id=teammate,
                team_abbr=r["team_abbr"],
                phase=r["season_type"],
                source_urls=sources.get(r["game_id"], []),
            )
            for r in sample
        ]
        for name, sample in groups.items()
    }
    return dict(
        request=request,
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
