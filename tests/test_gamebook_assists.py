from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import parse_gamebooks as parse
from scripts.gamebook_pair_report import pair_rows, render_report

GAME = {
    "game_id": "0000000001",
    "date": "2025-10-22",
    "season": "2025-26",
    "home": "BBB",
    "away": "AAA",
    "home_name": "Beta Hosts",
    "away_name": "Alpha Visitors",
}
PLAYERS = [
    {"name": "Avery Able", "team": "BBB", "player_id": "10", "played": True},
    {"name": "Blake Baker", "team": "BBB", "player_id": "11", "played": True},
    {"name": "Casey Cole", "team": "AAA", "player_id": "20", "played": True},
]


def fragment(x, y, text):
    return {"x": x, "y": y, "text": text, "size": 10}


def header(period=1):
    return [
        fragment(10, 20, "OFFICIAL PLAY-BY-PLAY"),
        fragment(440, 30, f"{period}st QUARTER"),
        *[
            fragment(x, 60, t)
            for x, t in [
                (10, "Time"),
                (60, "Hosts"),
                (280, "Score"),
                (330, "Lead"),
                (380, "Visitors"),
            ]
        ],
    ]


def pdf_from_pages(pages):
    writer = PdfWriter()
    for fragments in pages:
        page = writer.add_blank_page(width=612, height=792)
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
        )
        commands = []
        for f in fragments:
            t = f["text"].replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            commands.append(f"BT /F1 10 Tf {f['x']} {792 - f['y']} Td ({t}) Tj ET")
        stream = DecodedStreamObject()
        stream.set_data("\n".join(commands).encode())
        page[NameObject("/Contents")] = stream
    result = io.BytesIO()
    writer.write(result)
    return result.getvalue()


def box_page(assist_total=1):
    fragments = [fragment(450, 20, "FINAL BOX")]
    positions = [100, 130, 200, 230, 260, 300, 340]
    fields = ["POS", "MIN", "FG", "3P", "FT", "A", "PTS"]
    for base, label, names, stats in [
        (50, "VISITOR:", [("1", "Casey Cole")], [[0, 0, 0, 0, 0]]),
        (
            200,
            "HOME:",
            [("2", "Avery Able"), ("3", "Blake Baker")],
            [[0, 0, 0, assist_total, 0], [1, 1, 0, 0, 3]],
        ),
    ]:
        fragments.append(fragment(10, base, label))
        fragments.extend(fragment(x, base + 10, t) for x, t in zip(positions, fields))
        for i, ((jersey, name), values) in enumerate(zip(names, stats)):
            y = base + 30 + i * 15
            fragments.extend(
                [
                    fragment(10, y, jersey),
                    fragment(30, y, name),
                    fragment(130, y, "24:00"),
                ]
            )
            fragments.extend(
                fragment(x, y, str(v)) for x, v in zip(positions[2:], values)
            )
        y = base + 30 + len(names) * 15
        fragments.append(fragment(130, y, "240:00"))
        fragments.extend(
            fragment(x, y, str(sum(row[i] for row in stats)))
            for i, x in enumerate(positions[2:])
        )
    return fragments


def document(tmp_path, assist_total=1):
    pages = [box_page(assist_total)]
    for period in range(1, 5):
        rows = header(period) + [
            fragment(10, 90, "12:00"),
            fragment(200, 90, "Start of Period (7:00 PM)"),
        ]
        if period == 1:
            rows += [
                fragment(10, 110, "11:00"),
                fragment(60, 110, "B.Baker 25' 3PT Jump Shot (A.Able)"),
                fragment(280, 110, "3-0"),
            ]
        rows += [
            fragment(200, 150, f"End of {period}st QUARTER (3-0)"),
            fragment(200, 165, "End of Period (8:00 PM)"),
        ]
        pages.append(rows)
    body = pdf_from_pages(pages)
    path = tmp_path / "game.pdf"
    path.write_bytes(body)
    reference = {
        "season": "2025-26",
        "date": "2025-10-22",
        "home_team_abbreviation": "BBB",
        "away_team_abbreviation": "AAA",
        "stats": {
            "Home": {
                "FullGame": [
                    {"Name": p["name"], "EntityId": p["player_id"]}
                    for p in PLAYERS
                    if p["team"] == "BBB"
                ]
            },
            "Away": {"FullGame": [{"Name": "Casey Cole", "EntityId": "20"}]},
        },
    }
    identity = tmp_path / "identity.json"
    identity.write_text(json.dumps(reference))
    attempt = {
        "game": GAME,
        "sha256": parse.sha(body),
        "url": "https://example.test/game.pdf",
    }
    return path, attempt, identity


def test_full_pdf_reconciles_and_replay_is_deterministic(tmp_path):
    args = document(tmp_path)
    first = parse.parse_document(*args)
    assert first["status"] == "reconciled", first["errors"]
    assert len(first["assists"]) == 1
    event = first["assists"][0]
    assert (event["assister_id"], event["scorer_id"], event["points"]) == (
        "10",
        "11",
        3,
    )
    assert event["page"] == 2 and event["clock"] == "11:00"
    assert parse.canonical(first) == parse.canonical(parse.parse_document(*args))


def test_wrong_box_totals_reject_game_without_adjusting_events(tmp_path):
    result = parse.parse_document(*document(tmp_path, assist_total=2))
    assert result["status"] == "rejected"
    assert len(result["assists"]) == 1
    assert any(e["error"] == "Player box-score mismatch" for e in result["errors"])
    assert pair_rows({"games": [result]}) == []


def test_tampered_document_rejected(tmp_path):
    path, attempt, identity = document(tmp_path)
    path.write_bytes(path.read_bytes() + b"changed")
    with pytest.raises(ValueError, match="hash differs"):
        parse.parse_document(path, attempt, identity)


def test_wrapped_assist_and_fractional_clock_stay_together():
    f = header() + [
        fragment(10, 100, ":38.4"),
        fragment(380, 96, "C.Cole 25' 3PT Jump Shot"),
        fragment(380, 104, "(D.Delta)"),
        fragment(280, 100, "0-3"),
        fragment(10, 120, ":35.0"),
        fragment(60, 120, "TIMEOUT (1)"),
    ]
    row = parse.pbp_rows(sorted(f, key=lambda x: (x["y"], x["x"])), 9, 612, GAME)[0]
    assert row["clock"] == ":38.4"
    assert row["actions"][0]["text"] == "C.Cole 25' 3PT Jump Shot (D.Delta)"
    assert row["actions"][0]["team"] == "AAA"
    assert row["actions"][0]["bbox"][1] < 96 and row["actions"][0]["bbox"][3] > 104


def test_summary_only_period_end_is_preserved():
    f = header() + [
        fragment(200, 90, "End of 1st QUARTER (3-0)"),
        fragment(200, 100, "End of Period (8:00 PM)"),
    ]
    rows = parse.pbp_rows(f, 3, 612, GAME)
    assert len(rows) == 1 and rows[0]["clock"] is None and rows[0]["score"] == "3-0"


def test_unknown_clock_page_cannot_be_skipped():
    with pytest.raises(ValueError, match="Unclassified clock-bearing"):
        parse.pbp_rows([fragment(10, 100, "11:10")], 3, 612, GAME)


@pytest.mark.parametrize(
    "text",
    ["B.Baker Dunk (A.Able)", "B.Baker 5' Hook (A.Able)", "B.Baker Layup (A.Able)"],
)
def test_short_shots_and_hook_have_assists(text):
    e = parse.parse_action({"text": text, "team": "BBB", "bbox": [0, 0, 1, 1]}, PLAYERS)
    assert e["assister_id"] == "10" and e["points"] == 2


@pytest.mark.parametrize(
    "text", ["B.Baker S.FOUL (P1, T1) (A.Able)", "MISS B.Baker Layup (A.Able)"]
)
def test_foul_officials_and_misses_never_produce_assists(text):
    assert (
        parse.parse_action({"text": text, "team": "BBB", "bbox": [0, 0, 1, 1]}, PLAYERS)
        is None
    )


def test_ambiguous_unknown_and_self_assists_fail_closed():
    with pytest.raises(ValueError, match="Unresolved"):
        parse.find_player(
            "A.Able",
            "BBB",
            PLAYERS
            + [{"name": "Alex Able", "team": "BBB", "player_id": "12", "played": True}],
        )
    with pytest.raises(ValueError, match="Unresolved"):
        parse.parse_action(
            {"text": "B.Baker Dunk (Z.Unknown)", "team": "BBB", "bbox": []}, PLAYERS
        )
    with pytest.raises(ValueError, match="Self assist"):
        parse.parse_action(
            {"text": "B.Baker Dunk (B.Baker)", "team": "BBB", "bbox": []}, PLAYERS
        )
    with pytest.raises(ValueError, match="Unclassified action"):
        parse.parse_action(
            {"text": "NEW UNKNOWN ACTION", "team": "BBB", "bbox": []}, PLAYERS
        )


def test_reference_wrong_scope_rejected():
    with pytest.raises(ValueError, match="date/season"):
        parse.resolve_identities([], {"season": "2024-25", "date": "2024-10-22"}, GAME)


def test_report_has_links_for_each_play_and_escapes_source_text(tmp_path):
    result = parse.parse_document(*document(tmp_path))
    result["assists"][0]["raw_text"] = "<img src=x onerror=alert(1)>"
    render_report({"games": [result]}, tmp_path)
    content = (tmp_path / "index.html").read_text()
    assert "<img src=x" not in content and "&lt;img src=x" in content
    assert f"documents/{result['extraction']['document_sha256']}.pdf#page=2" in content
    assert "play-" + result["assists"][0]["event_id"] in content
    assert "pair-BBB-10-11" in content
