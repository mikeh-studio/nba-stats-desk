"""Explicit sourced participation facts; absent records never prove absence."""

import json
import re
from pathlib import Path

from app.agent.payload import answer_payload
from app.agent.semantic_serving import requested_seasons
from app.agent.semantics import SemanticError


def load_participation_catalog():
    data = json.loads(
        Path(__file__).with_name("participation_catalog.json").read_text()
    )
    if data.get("version") != 1:
        raise ValueError("Unsupported participation catalog")
    seen = set()
    for r in data["records"]:
        key = (r["season"], r["event"], r["player_id"])
        if (
            key in seen
            or r["event"] != "NBA Finals"
            or r["participated"] is not False
            or type(r["player_id"]) is not int
            or not r["aliases"]
            or not r["source_url"].startswith("https://www.nba.com/")
        ):
            raise ValueError("Invalid participation record")
        seen.add(key)
    return data["records"]


def answer_known_nonparticipation(question, season, conversation_id=None):
    # Only unambiguous NBA Finals requests, never conference finals, comparisons,
    # hypothetical questions or a request about other seasons/players.
    text = question.lower()
    if re.search(
        r"\b(?:conference|east|west|if|would|could|versus|vs|compare|before|after)\b",
        text,
    ):
        return None
    if not re.search(r"\b(?:nba )?finals?\b", text):
        return None
    try:
        seasons = requested_seasons(question, season)
    except SemanticError:
        return None
    if len(seasons) != 1:
        return None
    for record in load_participation_catalog():
        if record["season"] != seasons[0]:
            continue
        aliases = "|".join(re.escape(a.lower()) for a in record["aliases"])
        text, count = re.subn(r"\b(?:" + aliases + r")\b", " ", question.lower())
        if count != 1:
            continue
        text = re.sub(r"20\d{2}[-–/]\d{2,4}", " ", text)
        text = re.sub(r"[’']s\b", " ", text)
        text = re.sub(
            r"\b(?:nba|finals?|in|the|this|season|did|does|play|participate|reach|make|it|to|how|many|what|were|was|his|points?|pts|rebounds?|assists?|stats|statistics|per|game|score|scored|average)\b",
            " ",
            text,
        )
        if text.strip(" ?.!,"):
            continue
        return answer_payload(
            f"{record['player_name']} did not participate in the {record['season']} NBA Finals. {record['reason']} "
            "Finals statistics are not applicable, rather than missing or zero. "
            f"[Verified participation source]({record['source_url']}).",
            status="not_applicable",
            conversation_id=conversation_id,
            participation_evidence={
                "contract_version": "event_participation/1",
                **record,
            },
            agent_plan={
                "route": "event_participation",
                "confidence": 1.0,
                "needs_clarification": False,
            },
            assumptions=[
                "This is a sourced participation fact, not an inference from missing box scores. Other players and seasons require their own verified records."
            ],
        )
    return None
