"""Render a private, offline pair report from validated gamebook extraction."""

from __future__ import annotations

import csv
import html
from collections import defaultdict


def esc(value):
    return html.escape(str(value), quote=True)


def pair_rows(snapshot):
    pairs = defaultdict(list)
    for game in snapshot["games"]:
        if game["status"] == "reconciled":
            for event in game["assists"]:
                pairs[(event["team"], event["assister_id"], event["scorer_id"])].append(
                    event
                )
    return sorted(
        pairs.items(),
        key=lambda item: (
            item[0][0] != "NYK",
            item[0][0],
            -len(item[1]),
            item[1][0]["assister"],
            item[1][0]["scorer"],
        ),
    )


def render_report(snapshot, output):
    pairs = pair_rows(snapshot)
    games = snapshot["games"]
    accepted = [g for g in games if g["status"] == "reconciled"]
    events = [e for g in accepted for e in g["assists"]]
    nyk = [e for e in events if e["team"] == "NYK"]
    coverage = []
    md = [
        "# Assist pair report",
        "",
        f"{len(accepted)} of {len(games)} games reconciled against their own final box scores.",
        "",
        "Scope: both teams in the selected games. Opponents have different coverage; this is not a league ranking.",
        "Validation: same-document reconciliation of every playing player’s assists, field goals, threes, free throws and points, plus running scores. No independent event labels or warehouse comparison.",
        "",
        "| Date | Game | Status | Extracted / box assists |",
        "| --- | --- | --- | --- |",
    ]
    for g in games:
        game = g["game"]
        counts = []
        for team in [game["away"], game["home"]]:
            count = sum(e["team"] == team for e in g["assists"])
            expected = g["box_totals"].get(team, {}).get("A", "unknown")
            counts.append(f"{team}: {count} / {expected}")
        label = f"{game['away']} at {game['home']}"
        coverage.append(
            f"<tr><td>{esc(game['date'])}</td><td>{esc(label)}</td><td>{esc(g['status'])}</td><td>{esc('; '.join(counts))}</td><td><a href='{esc(game['game_id'])}.json'>Audit JSON</a></td></tr>"
        )
        md.append(f"| {game['date']} | {label} | {g['status']} | {'; '.join(counts)} |")
    summaries, details = [], []
    md += [
        "",
        "## Directed pairs",
        "",
        "| Team | Passer → scorer | Assists | Assisted FG points | Plays |",
        "| --- | --- | ---: | ---: | --- |",
    ]
    export = []
    for (team, passer, scorer), plays in pairs:
        first = plays[0]
        pid = f"pair-{team}-{passer}-{scorer}"
        label = f"{first['assister']} → {first['scorer']}"
        points = sum(e["points"] for e in plays)
        summaries.append(
            f"<tr><td>{esc(team)}</td><td>{esc(label)}</td><td>{len(plays)}</td><td>{points}</td><td><a href='#{pid}'>View {len(plays)} plays</a></td></tr>"
        )
        md.append(
            f"| {team} | {label} | {len(plays)} | {points} | [View plays](index.html#{pid}) |"
        )
        playrows = []
        for e in plays:
            local = f"documents/{e['document_sha256']}.pdf#page={e['page']}"
            remote = f"{e['source_url']}#page={e['page']}"
            playrows.append(
                f"<tr id='play-{e['event_id']}'><td>{esc(e['game_date'])}</td><td>Q{e['period']} · {esc(e['clock'])}</td><td>{e['points']}</td><td>{esc(e['raw_text'])}</td><td><a href='{esc(local)}' target='_blank' rel='noopener'>Saved PDF · p{e['page']}</a><br><a href='{esc(remote)}' target='_blank' rel='noopener'>NBA source</a></td></tr>"
            )
            export.append(
                {
                    k: e[k]
                    for k in [
                        "event_id",
                        "game_id",
                        "game_date",
                        "team",
                        "period",
                        "clock",
                        "assister_id",
                        "assister",
                        "scorer_id",
                        "scorer",
                        "points",
                        "page",
                        "raw_text",
                        "document_sha256",
                        "revision",
                        "source_url",
                    ]
                }
            )
        details.append(
            f"<details id='{pid}'><summary>{esc(team)} · {esc(label)} <span>{len(plays)} assists · {points} assisted FG points</span></summary><div class='scroll'><table><thead><tr><th>Date</th><th>Play</th><th>Points</th><th>Source text</th><th>Evidence</th></tr></thead><tbody>{''.join(playrows)}</tbody></table></div></details>"
        )
    validation = []
    for g in games:
        for p in g["player_validation"]:
            validation.append(
                f"<tr><td>{esc(g['game']['date'])}</td><td>{esc(p['team'])}</td><td>{esc(p['name'])}</td><td>{p['extracted']['A']} / {p['stats']['A']}</td><td>{'Match' if p['matches'] else 'Mismatch'}</td></tr>"
            )
    failures = "".join(
        f"<pre>{esc(g['game']['game_id'])}: {esc(g['errors'])}</pre>"
        for g in games
        if g["errors"]
    )
    content = f"""<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Five game assist pair report</title>
<style>
:root{{color-scheme:light}}*{{box-sizing:border-box}}body{{margin:0;background:#f4f6f8;color:#172332;font:15px/1.55 system-ui,sans-serif}}main{{max-width:1180px;margin:auto;padding:36px 24px 80px}}h1{{font-size:36px;line-height:1.15;margin:8px 0 14px}}h2{{margin-top:34px}}a{{color:#1259a5}}.eyebrow{{font-weight:700;letter-spacing:.1em;font-size:12px;color:#536479}}.note{{background:#e7eef6;border-left:4px solid #326997;padding:16px 20px}}.cards{{display:flex;gap:16px;flex-wrap:wrap;margin:24px 0}}.card{{background:white;border:1px solid #dbe1e8;border-radius:10px;padding:18px 24px;min-width:180px}}.card strong{{display:block;font-size:32px}}.scroll{{overflow:auto}}table{{border-collapse:collapse;width:100%;background:white}}th,td{{padding:11px 14px;text-align:left;border-bottom:1px solid #e1e6ec;vertical-align:top}}th{{background:#edf1f5;font-size:12px;text-transform:uppercase;letter-spacing:.03em}}details{{margin:12px 0;background:white;border:1px solid #dbe1e8;border-radius:8px;scroll-margin:20px}}summary{{cursor:pointer;padding:16px;font-weight:650}}summary span{{font-weight:400;color:#526478;margin-left:12px}}:target{{outline:3px solid #e7a539;background:#fff8df}}.subtitle{{max-width:850px;color:#526478}}pre{{white-space:pre-wrap}}footer{{margin-top:32px;color:#526478;font-size:13px}}
</style><main><div class="eyebrow">NBA DATASET PILOT · 2025–26 REGULAR SEASON</div><h1>Who assisted whom?</h1><p class="subtitle">October 22–31, 2025 · Both teams in five Knicks games. Counts cover assisted made baskets, not all passes or potential assists.</p>
<div class="cards"><div class="card"><strong>{len(accepted)} / {len(games)}</strong>Games reconciled</div><div class="card"><strong>{len(nyk)}</strong>Knicks assists</div><div class="card"><strong>{len(events)}</strong>Assists across both teams</div><div class="card"><strong>{len(pairs)}</strong>Directed player pairs</div></div>
<p class="note"><b>What is validated:</b> extracted player assists, field goals, threes, free throws and points match the final box score in the same PDF; running scores also reconcile. This is not an independent accuracy test. Opponents appear in only one selected game each. No league-wide ranking or passing-efficiency claim is supported.</p>
<p><a href="assists.csv">Download assist events CSV</a> · <a href="snapshot.json">Full evidence JSON</a> · <a href="report.md">Markdown report</a></p>
<h2>Game coverage</h2><div class="scroll"><table><thead><tr><th>Date</th><th>Game</th><th>Status</th><th>Extracted / box assists</th><th>Evidence</th></tr></thead><tbody>{"".join(coverage)}</tbody></table></div>{failures}
<h2>Passer → scorer</h2><p>Knicks pairs first, then opponents. Assisted FG points are the value of the linked made shots, excluding free throws.</p><div class="scroll"><table><thead><tr><th>Team</th><th>Pair</th><th>Assists</th><th>Assisted FG points</th><th>Plays</th></tr></thead><tbody>{"".join(summaries)}</tbody></table></div>
<h2>Play evidence</h2><p>Expand a pair to see every counted play. Saved PDF links open the exact captured document at the cited page; the clock locates the row.</p>{"".join(details)}
<details><summary>Player reconciliation · {len(validation)} appearances</summary><div class="scroll"><table><thead><tr><th>Date</th><th>Team</th><th>Player</th><th>Assists extracted / box</th><th>All five checks</th></tr></thead><tbody>{"".join(validation)}</tbody></table></div></details>
<footer>Offline prototype · canonical player IDs resolved against frozen provider identity snapshots and game-specific box-score names. Documents and evidence stay private. Extraction revisions and source hashes are in the JSON. Independently labeled play accuracy and live warehouse reconciliation remain untested.</footer></main>
<script>function reveal(){{const el=document.getElementById(location.hash.slice(1));if(!el)return;const detail=el.closest('details');if(detail)detail.open=true;el.scrollIntoView();}}addEventListener('hashchange',reveal);addEventListener('load',reveal);</script></html>"""
    (output / "index.html").write_text(content, encoding="utf-8")
    (output / "report.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    with (output / "assists.csv").open("w", newline="", encoding="utf-8") as stream:
        fields = (
            list(export[0])
            if export
            else ["event_id", "game_id", "assister_id", "scorer_id"]
        )
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(export)
