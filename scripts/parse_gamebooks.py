#!/usr/bin/env python3
"""Offline assist extraction and same-document reconciliation for NBA gamebooks."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pypdf
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.fetch_gamebooks import load_manifest  # noqa: E402
from scripts.gamebook_pair_report import render_report  # noqa: E402

VERSION = "gamebook_assists/1"
CLOCK = re.compile(r"\d{0,2}:\d{2}(?:\.\d+)?$")
METRICS = ("FG", "3P", "FT", "A", "PTS")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def key(text):
    text = unicodedata.normalize("NFKD", text)
    return re.sub(r"[^a-z0-9]", "", text.lower())


def page_fragments(page):
    """PDF text origins in points, with a top-left page origin."""
    fragments = []
    if page.rotation or list(page.mediabox)[:2] != [0, 0]:
        raise ValueError("Unsupported rotated or translated page")

    def visit(text, cm, tm, font, size):
        if text.strip():
            if cm[1] or cm[2] or tm[1] or tm[2]:
                raise ValueError("Unsupported rotated text")
            x = tm[4] * cm[0] + tm[5] * cm[2] + cm[4]
            y = float(page.mediabox.height) - (tm[4] * cm[1] + tm[5] * cm[3] + cm[5])
            fragments.append(
                {
                    "x": round(x, 2),
                    "y": round(y, 2),
                    "size": round(abs(cm[3] * size), 2),
                    "text": " ".join(text.split()),
                }
            )

    page.extract_text(visitor_text=visit)
    return sorted(fragments, key=lambda f: (f["y"], f["x"]))


def lines(fragments):
    groups = []
    for fragment in fragments:
        if not groups or abs(groups[-1][0]["y"] - fragment["y"]) > 0.6:
            groups.append([])
        groups[-1].append(fragment)
    return [sorted(group, key=lambda f: f["x"]) for group in groups]


def aliases(name):
    parts = name.split()
    surname = "".join(parts[1:])
    return {key(parts[0][0] + surname), key(parts[0] + surname)}


def box_score(fragments, game):
    players, totals, headers = [], {}, None
    team = None
    for row in lines(fragments):
        texts = [f["text"] for f in row]
        if texts[0] in ("VISITOR:", "HOME:"):
            team = game["away" if texts[0] == "VISITOR:" else "home"]
            headers = None
        elif "MIN" in texts and "A" in texts and "PTS" in texts:
            headers = {f["text"]: f["x"] for f in row}
            if not all(m in headers for m in METRICS):
                raise ValueError("Unsupported box-score columns")
        elif headers and team:
            cells = {}
            for fragment in row:
                if fragment["x"] >= headers["MIN"] - 10:
                    column = min(headers, key=lambda h: abs(headers[h] - fragment["x"]))
                    if column in cells:
                        raise ValueError("Multiple box-score cells in one column")
                    cells[column] = fragment["text"]
            is_player = row[0]["x"] < headers["POS"] and texts[0].isdigit()
            if is_player:
                name = " ".join(
                    f["text"] for f in row[1:] if f["x"] < headers["POS"] - 3
                )
                played = bool(CLOCK.fullmatch(cells.get("MIN", "")))
                if not played and not any(
                    t.startswith(("DNP", "DND", "NWT")) for t in texts
                ):
                    raise ValueError(f"Unrecognized box-score player row: {name}")
                stats = {m: int(cells[m]) if played else None for m in METRICS}
                players.append(
                    {
                        "name": name,
                        "team": team,
                        "jersey": texts[0],
                        "played": played,
                        "stats": stats,
                        "box_page": 1,
                        "box_y": row[0]["y"],
                    }
                )
            elif re.fullmatch(r"\d{3}:\d{2}", cells.get("MIN", "")):
                totals[team] = {m: int(cells[m]) for m in METRICS}
                headers = None
    if set(totals) != {game["away"], game["home"]}:
        raise ValueError("Missing final box-score team totals")
    for team, total in totals.items():
        for metric in METRICS:
            if (
                sum(
                    p["stats"][metric]
                    for p in players
                    if p["team"] == team and p["played"]
                )
                != total[metric]
            ):
                raise ValueError(f"Box-score rows do not sum to team {team} {metric}")
    return players, totals


def resolve_identities(players, reference, game):
    if reference["season"] != game["season"] or reference["date"][:10] != game["date"]:
        raise ValueError("Identity reference date/season mismatch")
    for side in ("home", "away"):
        if reference[f"{side}_team_abbreviation"] != game[side]:
            raise ValueError("Identity reference team mismatch")
        by_name = defaultdict(set)
        for item in reference["stats"][side.title()]["FullGame"]:
            if str(item["EntityId"]) != "0":
                by_name[key(item["Name"])].add(str(item["EntityId"]))
        for player in players:
            if player["team"] != game[side]:
                continue
            ids = by_name[key(player["name"])]
            player["player_id"] = next(iter(ids)) if len(ids) == 1 else None
            if player["played"] and not player["player_id"]:
                raise ValueError(f"Unresolved identity: {player['name']}")
    ids = [p["player_id"] for p in players if p["played"]]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate active player identity")


def pbp_rows(fragments, page_number, width, game):
    joined = " ".join(f["text"] for f in fragments)
    if "OFFICIAL PLAY-BY-PLAY" not in joined:
        # Summary spillover pages contain no clocks. Never silently skip an event page.
        if any(CLOCK.fullmatch(f["text"]) and f["x"] < 40 for f in fragments):
            raise ValueError(f"Unclassified clock-bearing page {page_number}")
        return []
    period_match = re.search(r"(\d+)(?:st|nd|rd|th) (QUARTER|OVERTIME)", joined)
    if not period_match:
        raise ValueError(f"Missing period on PBP page {page_number}")
    period = int(period_match[1]) + (4 if period_match[2] == "OVERTIME" else 0)
    header = next(
        (
            r
            for r in lines(fragments)
            if "Time" in [f["text"] for f in r] and "Score" in [f["text"] for f in r]
        ),
        None,
    )
    if not header or len(header) != 5:
        raise ValueError(f"Unsupported PBP columns on page {page_number}")
    clock_col, left, score_col, lead_col, right = header
    side_teams = {}
    for side, label in [("left", left), ("right", right)]:
        matches = [
            game[s]
            for s in ("home", "away")
            if key(game[s + "_name"]).endswith(key(label["text"]))
        ]
        if len(matches) != 1:
            raise ValueError("Ambiguous PBP team header")
        side_teams[side] = matches[0]
    if len(set(side_teams.values())) != 2:
        raise ValueError("Repeated PBP team column")
    clocks = [
        f
        for f in fragments
        if CLOCK.fullmatch(f["text"])
        and abs(f["x"] - clock_col["x"]) < 2
        and f["y"] > clock_col["y"]
    ]
    endings = [f for f in fragments if f["text"].startswith("End of Period")]
    end_headers = [f for f in fragments if re.match(r"End of \d+", f["text"])]
    if not clocks and not endings:
        raise ValueError("Empty PBP page")
    if not clocks and any(
        clock_col["y"] < f["y"] < min(e["y"] for e in end_headers) for f in fragments
    ):
        raise ValueError("Unclassified content before period ending")
    result = []
    for i, clock in enumerate(clocks):
        top = (clocks[i - 1]["y"] + clock["y"]) / 2 if i else clock_col["y"] + 2
        bottom = (
            (clock["y"] + clocks[i + 1]["y"]) / 2
            if i + 1 < len(clocks)
            else clock["y"] + 10
        )
        parts = [
            f
            for f in fragments
            if top <= f["y"] < bottom
            and f is not clock
            and not f["text"].startswith("Copyright (c)")
        ]
        scores = [
            f["text"]
            for f in parts
            if abs(f["x"] - score_col["x"]) < 3 and re.fullmatch(r"\d+-\d+", f["text"])
        ]
        row = {
            "page": page_number,
            "row": i,
            "period": period,
            "clock": clock["text"],
            "score": scores[0] if len(scores) == 1 else None,
            "score_teams": [side_teams["left"], side_teams["right"]],
            "raw_text": " ".join(f["text"] for f in parts),
            "actions": [],
        }
        for side, start, end in [
            ("left", left["x"] - 2, score_col["x"] - 4),
            ("right", right["x"] - 2, width),
        ]:
            action = [f for f in parts if start <= f["x"] < end]
            if action:
                row["actions"].append(
                    {
                        "team": side_teams[side],
                        "text": " ".join(f["text"] for f in action),
                        "bbox": [
                            round(start, 2),
                            round(min(f["y"] - f["size"] for f in action) - 1, 2),
                            round(end, 2),
                            round(max(f["y"] for f in action) + 3, 2),
                        ],
                    }
                )
        result.append(row)
    for ending in endings:
        end_score = next(
            (re.search(r"\((\d+-\d+)\)", f["text"]) for f in end_headers), None
        )
        result.append(
            {
                "page": page_number,
                "row": len(result),
                "period": period,
                "clock": None,
                "score": end_score[1] if end_score else None,
                "score_teams": [side_teams["left"], side_teams["right"]],
                "raw_text": ending["text"],
                "actions": [],
            }
        )
    return result


def find_player(token, team, players):
    matches = [
        p for p in players if p["team"] == team and key(token) in aliases(p["name"])
    ]
    if len(matches) != 1 or not matches[0]["played"] or not matches[0]["player_id"]:
        raise ValueError(f"Unresolved PBP identity: {team} {token}")
    return matches[0]


def parse_action(action, players):
    text = action["text"]
    if text.startswith("MISS "):
        return None
    # Only made-field-goal syntax can produce an assist; foul officials are not assisters.
    shot = re.fullmatch(r"(\S+) (.*?(?:Shot|Layup|Dunk|Hook))(?: \(([^()]+)\))?", text)
    free_throw = re.fullmatch(r"(\S+) .*Free Throw \d+ of \d+", text)
    if not shot and not free_throw:
        if not re.search(
            r"REBOUND|STEAL|BLOCK|TURNOVER|FOUL|Foul|Offensive|TIMEOUT|SUB|"
            r"Violation|VIOLATION|Possession:|Start of Period|End of Period|"
            r"JUMP BALL|Jump Ball|Instant Replay|Challenge|Ejection|EJECTION|"
            r"Technical|Delay Of Game",
            text,
        ):
            raise ValueError(f"Unclassified action: {text}")
        return None
    match = shot or free_throw
    scorer = find_player(match[1], action["team"], players)
    assister = (
        find_player(shot[3], action["team"], players) if shot and shot[3] else None
    )
    if assister and assister["player_id"] == scorer["player_id"]:
        raise ValueError("Self assist")
    points = 1 if free_throw else 3 if "3PT" in shot[2] else 2
    return {
        "scorer_id": scorer["player_id"],
        "scorer": scorer["name"],
        "assister_id": assister["player_id"] if assister else None,
        "assister": assister["name"] if assister else None,
        "team": action["team"],
        "points": points,
        "kind": "free_throw" if free_throw else "field_goal",
        "raw_text": text,
        "bbox": action["bbox"],
    }


def parse_document(path, attempt, identity_path):
    game = attempt["game"]
    body, identity_bytes = path.read_bytes(), identity_path.read_bytes()
    if sha(body) != attempt["sha256"]:
        raise ValueError("PDF hash differs from retrieval record")
    run = {
        "parser_version": VERSION,
        "parser_sha256": sha(Path(__file__).read_bytes()),
        "pypdf_version": pypdf.__version__,
        "document_sha256": sha(body),
        "identity_sha256": sha(identity_bytes),
        "schema_version": "assist_events/1",
    }
    run["revision"] = sha(canonical(run).encode())
    reader = PdfReader(path)
    pages = [page_fragments(page) for page in reader.pages]
    if "FINAL BOX" not in " ".join(f["text"] for f in pages[0]):
        raise ValueError("Missing final box score")
    players, totals = box_score(pages[0], game)
    resolve_identities(players, json.loads(identity_bytes), game)
    rows = []
    # Pages before the first PBP header are period box scores, not action pages.
    first_pbp = next(
        i
        for i, p in enumerate(pages)
        if any("OFFICIAL PLAY-BY-PLAY" in f["text"] for f in p)
    )
    for i in range(first_pbp, len(pages)):
        rows.extend(
            pbp_rows(pages[i], i + 1, float(reader.pages[i].mediabox.width), game)
        )
    errors, scoring = [], []
    counters = defaultdict(Counter)
    scores = {game["away"]: 0, game["home"]: 0}
    for row in rows:
        for action in row["actions"]:
            try:
                event = parse_action(action, players)
            except ValueError as exc:
                errors.append(
                    {"page": row["page"], "clock": row["clock"], "error": str(exc)}
                )
                continue
            if not event:
                continue
            event.update({k: row[k] for k in ("page", "row", "period", "clock")})
            event.update(
                game_id=game["game_id"],
                game_date=game["date"],
                season=game["season"],
                phase="Regular Season",
                revision=run["revision"],
                document_sha256=sha(body),
                source_url=attempt["url"],
            )
            event["event_id"] = sha(
                f"{run['revision']}:{row['page']}:{row['row']}:{event['team']}".encode()
            )
            scoring.append(event)
            stats = counters[event["scorer_id"]]
            stats["PTS"] += event["points"]
            stats["FT" if event["kind"] == "free_throw" else "FG"] += 1
            if event["points"] == 3:
                stats["3P"] += 1
            if event["assister_id"]:
                counters[event["assister_id"]]["A"] += 1
            scores[event["team"]] += event["points"]
            if not row["score"]:
                errors.append(
                    {
                        "page": row["page"],
                        "clock": row["clock"],
                        "error": "Scoring event without score",
                    }
                )
        if row["score"]:
            expected = dict(zip(row["score_teams"], map(int, row["score"].split("-"))))
            if expected != scores:
                errors.append(
                    {
                        "page": row["page"],
                        "clock": row["clock"],
                        "error": "Running score mismatch",
                        "expected": expected,
                        "extracted": dict(scores),
                    }
                )
    periods = sorted({r["period"] for r in rows})
    if periods != list(range(1, max(periods, default=0) + 1)) or len(periods) < 4:
        errors.append({"error": "Missing periods"})
    for period in periods:
        period_rows = [r for r in rows if r["period"] == period]
        for marker in ("Start of Period", "End of Period"):
            if sum(marker in r["raw_text"] for r in period_rows) != 1:
                errors.append(
                    {"error": f"Missing/duplicate {marker}", "period": period}
                )
    validation = []
    for player in players:
        if not player["played"]:
            continue
        actual = {metric: counters[player["player_id"]][metric] for metric in METRICS}
        matched = actual == player["stats"]
        validation.append({**player, "extracted": actual, "matches": matched})
        if not matched:
            errors.append(
                {
                    "error": "Player box-score mismatch",
                    "player": player["name"],
                    "expected": player["stats"],
                    "extracted": actual,
                }
            )
    assists = [e for e in scoring if e["assister_id"]]
    return {
        "game": game,
        "extraction": run,
        "status": "reconciled" if not errors else "rejected",
        "errors": errors,
        "box_totals": totals,
        "player_validation": validation,
        "rows": rows,
        "scoring_events": scoring,
        "assists": assists,
        "pdf": str(path),
        "source_url": attempt["url"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--identity-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output or ROOT / "reports/gamebook-pilot" / (
        "pairs-"
        + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "-"
        + uuid4().hex[:8]
    )
    if (
        not output.resolve().is_relative_to((ROOT / "reports").resolve())
        or output.exists()
    ):
        parser.error("output must be a new directory inside reports/")
    report = json.loads((args.capture / "report.json").read_text())
    games = load_manifest(args.capture / "manifest.json")
    if sha((args.capture / "manifest.json").read_bytes()) != report["manifest_sha256"]:
        parser.error("capture manifest hash mismatch")
    if [a["game"] for a in report["attempts"]] != games:
        parser.error("capture attempts do not match frozen manifest")
    output.mkdir(parents=True)
    (output / "documents").mkdir()
    results = []
    started = time.monotonic()
    for attempt in report["attempts"]:
        game = attempt["game"]
        try:
            if attempt["status"] != "available":
                raise ValueError("Gamebook not available")
            path = args.capture / game["game_id"] / attempt["artifact"]
            if not path.resolve().is_relative_to(args.capture.resolve()):
                raise ValueError("Invalid artifact path")
            identity_path = args.identity_dir / f"{game['game_id']}-stats.json"
            result = parse_document(path, attempt, identity_path)
            shutil.copyfile(path, output / "documents" / f"{attempt['sha256']}.pdf")
        except (ValueError, OSError, KeyError, StopIteration) as exc:
            result = {
                "game": game,
                "status": "rejected",
                "errors": [{"error": str(exc)}],
                "assists": [],
                "scoring_events": [],
                "player_validation": [],
                "box_totals": {},
            }
        results.append(result)
        print(
            game["game_id"],
            result["status"],
            len(result["assists"]),
            "assists",
            len(result["errors"]),
            "errors",
            flush=True,
        )
        (output / f"{game['game_id']}.json").write_text(
            json.dumps(result, indent=2, ensure_ascii=False) + "\n"
        )
    snapshot = {
        "schema_version": "assist_pair_report/1",
        "games": results,
        "validation_scope": "same-PDF scoring/assist reconciliation; not independently labeled accuracy",
        "warehouse_validation": "not_run",
    }
    (output / "snapshot.json").write_text(canonical(snapshot) + "\n")
    (output / "run.json").write_text(
        json.dumps(
            {
                "created_at": datetime.now(timezone.utc).isoformat(),
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "snapshot_sha256": sha(canonical(snapshot).encode()),
            },
            indent=2,
        )
        + "\n"
    )
    render_report(snapshot, output)
    print("Report:", output / "index.html")
    return 0 if all(r["status"] == "reconciled" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
