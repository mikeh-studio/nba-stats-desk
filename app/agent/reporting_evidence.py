"""Offline-first reporting evidence for governed player overviews.

No network or model calls here. Corpus records are curated paraphrases with
provenance; they are never represented as publisher quotations.
"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from datetime import date
from typing import Any
from urllib.parse import urlsplit

from app.agent.performance_overview import build_overview
from app.agent.player_context import summarize_context, teammate_comparison
from app.agent.semantic_serving import source_players
from app.agent.semantics import Evidence

CONTRACT = "nba_reporting_evidence/0.2"


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def validate_corpus(documents: list[dict]) -> None:
    ids: set[str] = set()
    for doc in documents:
        key = doc["id"]
        if not re.fullmatch(r"R[0-9]+", key) or key in ids:
            raise ValueError("Reporting IDs must be unique R-prefixed integers")
        ids.add(key)
        url = urlsplit(doc["url"])
        if url.scheme != "https" or not url.hostname or url.username or url.password:
            raise ValueError("Sources require an HTTPS URL without credentials")
        for field in ("title", "publisher", "author", "summary", "retrieved_at"):
            if not isinstance(doc[field], str) or not doc[field].strip():
                raise ValueError(f"Missing source {field}")
        if len(doc["summary"].split()) > 100:
            raise ValueError("Curated summaries must be at most 100 words")
        if doc["content_kind"] != "curated_paraphrase":
            raise ValueError("Milestone corpus accepts attributed paraphrases only")
        if doc["status"] not in {"available", "withdrawn"}:
            raise ValueError("Invalid document status")
        if not doc["player_ids"] or any(type(p) is not int for p in doc["player_ids"]):
            raise ValueError("Explicit numeric player IDs required")
        start, end, version = (
            date.fromisoformat(doc[k])
            for k in ("event_start", "event_end", "version_available_date")
        )
        if start > end or end > version:
            raise ValueError("Invalid event dates or future event in summary")
        if doc["content_sha256"] != digest(doc["summary"]):
            raise ValueError("Reporting summary checksum mismatch")


def retrieve_reporting(documents: list[dict], case: dict, *, limit: int = 4) -> dict:
    """Filter before lexical ranking; date precision is whole calendar days.

    version_available_date is the latest displayed article update, not a
    guessed first-publication time. This is a conservative publication-date
    screen, not proof of the exact article version available in the past.
    """
    validate_corpus(documents)
    if not 1 <= limit <= 10:
        raise ValueError("Reporting limit must be 1..10")
    if case["reporting_mode"] not in {"retrospective", "published_by_cutoff"}:
        raise ValueError("Unknown reporting mode")
    start = min(case["start"], case["previous_start"])
    end = case["end"]
    terms = set(re.findall(r"[a-z]{3,}", case["question"].lower()))
    candidates, excluded = [], []
    seen: set[str] = set()
    for doc in documents:
        reason = None
        if case["player_id"] not in doc["player_ids"]:
            reason = "different_player"
        elif doc["status"] != "available":
            reason = "withdrawn"
        elif doc["event_end"] < start or doc["event_start"] > end:
            reason = "outside_event_window"
        elif (
            case["reporting_mode"] == "published_by_cutoff"
            and doc["version_available_date"] > case["reporting_cutoff"]
        ):
            reason = "published_after_cutoff"
        elif doc["content_sha256"] in seen:
            reason = "duplicate_content"
        if reason:
            excluded.append({"id": doc["id"], "reason": reason})
            continue
        seen.add(doc["content_sha256"])
        words = set(
            re.findall(r"[a-z]{3,}", (doc["title"] + " " + doc["summary"]).lower())
        )
        candidates.append((len(terms & words), doc))
    candidates.sort(key=lambda item: (-item[0], item[1]["id"]))
    return {
        "status": "found" if candidates else "no_relevant_reporting",
        "passages": [deepcopy(doc) for _, doc in candidates[:limit]],
        "excluded": excluded,
        "eligible_count": len(candidates),
    }


def prepare_case(
    evidence: Evidence, documents: list[dict], case: dict, context=None
) -> dict:
    """Resolve a case to the same deterministic overview used by Ask."""
    dates = {
        key: date.fromisoformat(case[key])
        for key in ("start", "end", "previous_start", "previous_end")
    }
    if (
        not dates["previous_start"]
        <= dates["previous_end"]
        < dates["start"]
        <= dates["end"]
    ):
        raise ValueError("Comparison periods must be ordered and non-overlapping")
    date.fromisoformat(case["reporting_cutoff"])
    if case["reporting_cutoff"] < case["end"]:
        raise ValueError("Reporting cutoff cannot precede the statistics window")
    players = source_players(evidence)
    matches = [p for p in players if p["player_id"] == case["player_id"]]
    if len(matches) != 1 or matches[0]["player_name"] != case["player_name"]:
        raise ValueError("Evaluation identity does not match the frozen warehouse")
    scope = {**dates, "seasons": case["seasons"], "phases": [case["phase"]]}
    if any((s, case["phase"]) not in evidence.covered_scopes for s in case["seasons"]):
        raise ValueError("Requested season/phase absent from snapshot")
    overview = build_overview(
        case["question"], evidence, players, scope, selected=matches[0]
    )
    if overview["status"] not in {"ok", "no_observations"}:
        raise ValueError(f"Overview unavailable: {overview['status']}")
    own = [r for r in evidence.rows if r["player_id"] == case["player_id"]]
    reporting = retrieve_reporting(documents, case)
    metrics = [
        {
            "id": "S_" + m["key"],
            "label": m["label"],
            "current": m["value"],
            "previous": m["previous"]["value"] if m["previous"] else None,
            "change": m["change"],
            "valid_games": m["valid_games"],
            "missing_games": m["missing_component_games"],
            "previous_missing_games": m["previous"]["missing_component_games"]
            if m["previous"]
            else None,
            "unit": m.get("unit", "per game"),
            "change_unit": m.get("change_unit", "per game"),
            **(
                {
                    "components": {
                        "current": m["components"],
                        "previous": m["previous"]["components"],
                    }
                }
                if m.get("unit") == "percent"
                else {}
            ),
        }
        for m in overview["semantic_evidence"]["metrics"]
    ]

    def period_rows(start, end):
        return [
            r
            for r in own
            if start <= r["game_date"] <= end
            and r["season_type"] == case["phase"]
            and r["season"] in case["seasons"]
        ]

    current = period_rows(case["start"], case["end"])
    previous = period_rows(case["previous_start"], case["previous_end"])
    bundle = {
        "contract": CONTRACT,
        "case_id": case["id"],
        "question": case["question"],
        "player": matches[0]["player_name"],
        "player_id": case["player_id"],
        "scope": {
            k: case[k]
            for k in (
                "start",
                "end",
                "previous_start",
                "previous_end",
                "phase",
                "reporting_mode",
                "reporting_cutoff",
            )
        },
        "statistics": {
            "snapshot_id": evidence.snapshot_id,
            "metrics": metrics,
            "appearances": {
                "id": "S_games",
                "current": len(current),
                "previous": len(previous),
            },
            "teams": {
                "id": "S_teams",
                "current": sorted({r["team_abbr"] for r in current}),
                "previous": sorted({r["team_abbr"] for r in previous}),
            },
        },
        "reporting": {k: reporting[k] for k in ("status", "passages")},
        "limitations": [
            "Stats are retrospective corrected warehouse facts, not historically known values.",
            "Per-game rates use recorded appearances; missing appearances are not zero-stat games.",
            "Reporting is a small curated corpus; absence of evidence does not establish absence of events.",
            "Paraphrases are curator-written, not quotations; displayed update dates conservatively screen publication timing.",
            "Before/after changes do not prove causation. Do not infer medical causes or prognoses.",
        ],
    }
    if context is not None:
        bundle["statistics"]["opponent"] = summarize_context(context["rows"], case)
        if case.get("teammate_id"):
            bundle["statistics"]["teammate"] = teammate_comparison(
                context["rows"], context["reports"], case, case["teammate_id"]
            )
    return {
        "case": case,
        "bundle": bundle,
        "bundle_sha256": digest(bundle),
        "overview": overview,
        "retrieval_audit": reporting,
    }


def validate_response(bundle: dict, response: dict) -> list[str]:
    """Structural citation checks, not a semantic entailment judge."""
    errors = []
    if response.get("case_id") != bundle["case_id"]:
        errors.append("case_id_mismatch")
    statistics = {m["id"] for m in bundle["statistics"]["metrics"]} | {
        "S_games",
        "S_teams",
    }
    statistics |= {
        bundle["statistics"][key]["id"]
        for key in ("opponent", "teammate")
        if key in bundle["statistics"]
    }
    reports = {p["id"] for p in bundle["reporting"]["passages"]}
    for index, claim in enumerate(response.get("claims", [])):
        kind = claim.get("kind")
        ids = claim.get("evidence_ids", [])
        if not claim.get("text", "").strip():
            errors.append(f"claim_{index}:empty_text")
        if kind not in {
            "statistic",
            "reported_context",
            "interpretation",
            "limitation",
        }:
            errors.append(f"claim_{index}:invalid_kind")
        if any(key not in statistics | reports for key in ids):
            errors.append(f"claim_{index}:unknown_evidence")
        if kind == "statistic" and (
            not ids or any(key not in statistics for key in ids)
        ):
            errors.append(f"claim_{index}:statistic_requires_metrics")
        if kind == "reported_context" and (
            not ids or any(key not in reports for key in ids)
        ):
            errors.append(f"claim_{index}:context_requires_reporting")
        if kind == "interpretation" and not ids:
            errors.append(f"claim_{index}:interpretation_requires_evidence")
    if not response.get("claims"):
        errors.append("empty_claims")
    return errors


def attach_reporting(overview: dict, bundle: dict, response: dict) -> dict:
    """Compose an Ask-shaped payload without changing governed tables or charts."""
    errors = validate_response(bundle, response)
    result = deepcopy(overview)
    result["reporting_evidence"] = {
        "contract": CONTRACT,
        "status": "invalid_response" if errors else bundle["reporting"]["status"],
        "validation_errors": errors,
        "passages": deepcopy(bundle["reporting"]["passages"]),
        "claims": [] if errors else deepcopy(response["claims"]),
    }
    if errors:
        return result
    sources = {p["id"]: p for p in bundle["reporting"]["passages"]}
    paragraphs = []
    for claim in response["claims"]:
        citations = " ".join(
            f"[{key}]({sources[key]['url']})" if key in sources else f"[{key}]"
            for key in claim["evidence_ids"]
        )
        paragraphs.append(f"{claim['text']} {citations}".strip())
    result["answer"] = "\n\n".join(paragraphs)
    return result
