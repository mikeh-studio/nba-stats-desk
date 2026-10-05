#!/usr/bin/env python3
"""Generate new availability questions and independently audit live JSON/SSE answers.

Uses a local immutable source bundle and a loopback app only. No provider calls.
Writes private request/answer artifacts to a new output directory.
"""

import argparse
import json
import math
import random
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from statistics import median
from urllib.parse import urlparse
from urllib.request import Request, urlopen


def oracle(document, request):
    """Independent source join; never import production planning/calculation code."""
    rows = document["stats"]["rows"]
    index = {(r["season"], r["game_id"], r["player_id"]): r for r in rows}
    games = {(g["season"], g["game_id"], g["team_abbr"]): g for g in document["games"]}
    reports = defaultdict(list)
    for r in document["reports"]:
        reports[(r["season"], r["game_date"], r["team_abbr"])].append(r)
    groups = {"both_played": [], "did_not_play": []}
    participation = {
        (p["season"], p["game_id"], p["player_id"]): p
        for p in document.get("participation", [])
    }

    def limited(pid, minutes, current):
        if request.get("include_limited_minutes", False):
            return False
        history = sorted(
            (
                x
                for x in rows
                if x["player_id"] == pid
                and x["season"] == current["season"]
                and x["game_date"] < current["game_date"]
                and (x.get("min") or 0) > 0
                and games[(x["season"], x["game_id"], x["team_abbr"])]["final"]
                and not games[(x["season"], x["game_id"], x["team_abbr"])].get(
                    "postponed"
                )
            ),
            key=lambda x: (x["game_date"], x["game_id"]),
        )[-10:]
        return len(history) >= 5 and minutes < median(
            x["min"] for x in history
        ) * request.get("minutes_threshold", 0.5)

    for r in rows:
        if r["player_id"] != request["player_id"] or r["season"] != request["season"]:
            continue
        if request["phase"] != "Both" and r["season_type"] != request["phase"]:
            continue
        if request["start"] and r["game_date"] < request["start"]:
            continue
        if request["end"] and r["game_date"] > request["end"]:
            continue
        g = games[(r["season"], r["game_id"], r["team_abbr"])]
        if request["home_away"] and g["home_away"] != request["home_away"]:
            continue
        if request["opponent"] and g["opponent_abbr"] != request["opponent"]:
            continue
        if not g["final"] or g.get("postponed") or not r.get("min"):
            continue
        other = index.get((r["season"], r["game_id"], request["teammate_id"]))
        if other and other["team_abbr"] != r["team_abbr"]:
            continue
        matchup = (
            f"{g['opponent_abbr']}@{g['team_abbr']}"
            if g["home_away"] == "home"
            else f"{g['team_abbr']}@{g['opponent_abbr']}"
        )
        tip = datetime.fromisoformat(g["scheduled_start_utc"].replace("Z", "+00:00"))
        eligible = [
            x
            for x in reports[(r["season"], r["game_date"], r["team_abbr"])]
            if x["matchup"] == matchup
            and 0
            < (
                tip
                - datetime.fromisoformat(
                    x["report_timestamp_utc"].replace("Z", "+00:00")
                )
            ).total_seconds()
            <= 172800
        ]
        latest = max(
            (
                datetime.fromisoformat(x["report_timestamp_utc"].replace("Z", "+00:00"))
                for x in eligible
            ),
            default=None,
        )
        statuses = {
            x["injury_status"]
            for x in eligible
            if datetime.fromisoformat(x["report_timestamp_utc"].replace("Z", "+00:00"))
            == latest
            and x["player_id"] == request["teammate_id"]
        }
        final = participation.get((r["season"], r["game_id"], request["teammate_id"]))
        if final and final["team_abbr"] != r["team_abbr"]:
            continue
        minutes = other.get("min") if other else None
        if minutes is None and final:
            minutes = final["minutes"]
        group = None
        if minutes is not None and minutes > 0:
            group = "both_played"
        elif minutes == 0 or (not other and not final and statuses == {"Out"}):
            group = "did_not_play"
        if group and not limited(request["player_id"], r["min"], r):
            if group != "both_played" or not limited(
                request["teammate_id"], minutes, r
            ):
                groups[group].append(r)
    return groups


def metric_value(rows, key, aggregation):
    ratio = {
        "fg_pct": (["fgm", "fga"], lambda s: s["fgm"], lambda s: s["fga"], 100),
        "ts_pct": (
            ["pts", "fga", "fta"],
            lambda s: s["pts"],
            lambda s: 2 * (s["fga"] + 0.44 * s["fta"]),
            100,
        ),
        "pts_per36": (["pts", "min"], lambda s: 36 * s["pts"], lambda s: s["min"], 1),
    }
    if key in ratio:
        components, num, den, scale = ratio[key]
        valid = [r for r in rows if all(r.get(c) is not None for c in components)]
        sums = {c: sum(r[c] for r in valid) for c in components}
        value = num(sums) / den(sums) if valid and den(sums) else None
    else:
        valid = [r for r in rows if r.get(key) is not None]
        value = (
            sum(r[key] for r in valid) / (len(valid) if aggregation == "average" else 1)
            if valid
            else None
        )
        scale = 1
    return value, len(valid), scale


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--url", default="http://127.0.0.1:8017")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case-indices", type=int, nargs="+")
    args = parser.parse_args()
    if urlparse(args.url).hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("Loopback only")
    args.output.mkdir(parents=True, exist_ok=False)
    doc = json.loads(args.evidence.read_text())
    names = {r["player_id"]: r["player_name"] for r in doc["stats"]["rows"]}
    # Discover pairs from co-appearances, then sample them reproducibly. No registry.
    teams = defaultdict(Counter)
    for r in doc["stats"]["rows"]:
        teams[r["team_abbr"]][r["player_id"]] += 1
    pairs = []
    for counts in teams.values():
        top = [pid for pid, _ in counts.most_common(3)]
        pairs.extend((a, b) for a in top for b in top if a != b)
    random.Random(37).shuffle(pairs)
    pairs = pairs[:8]
    # Include the user-reported failure and its reverse as explicit regressions.
    pairs = [(201939, 203110), (203110, 201939), (2544, 1629029), *pairs]
    templates = [
        (
            "Tell me how {a} played while {b} was out",
            list(
                ("pts", "reb", "ast", "stl", "blk", "tov", "fg3m", "min", "plus_minus")
            ),
            "average",
            None,
        ),
        (
            "Show {a} assists when {b} was absent in the regular season",
            ["ast"],
            "average",
            None,
        ),
        ("Compare {a} field goal percentage without {b}", ["fg_pct"], "average", None),
        (
            "How did {a} perform without {b} at home using points per 36 minutes",
            ["pts_per36"],
            "average",
            "home",
        ),
    ]
    cases = []
    for a, b in pairs:
        for template, metrics, aggregation, venue in templates:
            q = template.format(a=names[a], b=names[b]).replace("using ", "")
            expected = dict(
                player_id=a,
                teammate_id=b,
                season="2025-26",
                phase="Regular Season" if "regular season" in q else "Both",
                start=None,
                end=None,
                metrics=metrics,
                aggregation=aggregation,
                home_away=venue,
                opponent=None,
            )
            cases.append((q, expected))
            if "regular season" in q:
                expected["explicit_phase"] = True
    failures = []
    answered = 0
    withheld = 0
    selected_cases = [
        (i, case)
        for i, case in enumerate(cases)
        if args.case_indices is None or i in args.case_indices
    ]
    for i, (question, expected) in selected_cases:
        if i:
            time.sleep(5.2)  # Respect the normal local 12/minute limit.
        body = json.dumps(dict(question=question, provider="openai")).encode()
        with urlopen(
            Request(
                args.url + "/api/agent/ask", body, {"Content-Type": "application/json"}
            ),
            timeout=60,
        ) as response:
            payload = json.load(response)
        (args.output / f"{i:03}.json").write_text(
            json.dumps(
                dict(question=question, expected=expected, payload=payload), indent=2
            )
        )
        try:
            groups = oracle(doc, expected)
            result = payload["availability_evidence"]
            assert result["policy_version"] == "availability/2"
            expected = {
                "include_limited_minutes": False,
                "minutes_threshold": 0.5,
                **expected,
            }
            assert result["request"] == expected, "Requested scope changed"
            assert (
                sum(map(len, result["groups"].values())) + len(result["excluded_games"])
                == result["scope_appearances"]
            )
            if not groups["did_not_play"]:
                assert not payload["charts"]
                assert all(m["difference"] is None for m in result["metrics"])
                withheld += 1
            for group, sample in groups.items():
                assert {x["game_id"] for x in result["groups"][group]} == {
                    r["game_id"] for r in sample
                }, "Game attribution mismatch"
                for game in result["groups"][group]:
                    assert (
                        game["player_id"] == expected["player_id"]
                        and game["teammate_id"] == expected["teammate_id"]
                    )
                    if group == "did_not_play":
                        assert game["source_urls"] or game["teammate_minutes"] == 0
            for m in result["metrics"]:
                for group, sample in groups.items():
                    value, n, scale = metric_value(
                        sample, m["metric"], expected["aggregation"]
                    )
                    actual = m["groups"][group]
                    assert actual["valid_games"] == n and actual[
                        "observed_games"
                    ] == len(sample)
                    assert (
                        actual["value"] is None
                        if value is None
                        else math.isclose(actual["value"], value, abs_tol=1e-9)
                    )
            assert (
                names[expected["player_id"]] in payload["answer"]
                and names[expected["teammate_id"]] in payload["answer"]
            )
            assert (
                payload["player_profile"]["player"]["player_id"]
                == expected["player_id"]
            )
            for row, metric in zip(
                payload["tables"][0]["rows"], result["metrics"], strict=True
            ):
                for col, group in [(1, "both_played"), (2, "did_not_play")]:
                    value, _, scale = metric_value(
                        groups[group], metric["metric"], expected["aggregation"]
                    )
                    assert row[col] == (
                        "unavailable" if value is None else f"{value * scale:.1f}"
                    ), "Rendered table mismatch"
            for metric in result["metrics"][:3] if groups["did_not_play"] else []:
                value, _, scale = metric_value(
                    groups["did_not_play"], metric["metric"], expected["aggregation"]
                )
                assert (
                    "unavailable" if value is None else f"{value * scale:.1f}"
                ) in payload["answer"], "Narrative statistic missing"
            if payload["charts"]:
                metric = next(
                    m
                    for m in result["metrics"]
                    if all(
                        g["value"] is not None and not g["missing_component_games"]
                        for g in m["groups"].values()
                    )
                )
                points = payload["charts"][0]["series"][0]["points"]
                for point, group in zip(
                    points, ["both_played", "did_not_play"], strict=True
                ):
                    value, _, scale = metric_value(
                        groups[group], metric["metric"], expected["aggregation"]
                    )
                    assert math.isclose(
                        point["y"], round(value * scale, 1), abs_tol=1e-9
                    ), "Chart attribution mismatch"
            if groups["did_not_play"]:
                answered += 1
        except (AssertionError, KeyError) as exc:
            failures.append(dict(case=i, error=str(exc)))
    report = dict(
        total=len(selected_cases),
        answered=answered,
        correctly_withheld=withheld,
        failures=failures,
        source_hash=doc["sha256"],
        kind="live endpoint plus independent local source arithmetic",
        model_calls=0,
    )
    (args.output / "summary.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
