"""Typed Overview clarifications: replies must not revive rejected constraints."""

import re

from app.agent.overview_positions import requested_groups
from app.agent.semantics import SemanticError

WINDOW_MESSAGE = (
    "Ask about the current window alone, or compare a full season with the prior "
    "season. Separate current and baseline date ranges are not supported by this "
    "Overview. Please restate the question with one of those choices."
)


class OverviewClarification(SemanticError):
    def __init__(self, kind, message):
        super().__init__("clarification_required", message)
        self.kind = kind


def resume_overview(question, pending):
    """Return a replacement question or a typed clarification, without concatenation."""
    kind = pending.query_plan["overview_clarification"]
    # A full restatement supersedes all rejected constraints and identities.
    if re.match(
        r"^\s*(?:give|show|how|what|who|which|compare|tell|analy[sz]e|summari[sz]e)\b",
        question,
        re.I,
    ):
        return question, None
    if kind == "groups":
        choice = re.fullmatch(
            r"\s*(?:use\s+|only\s+)?(guards?|wings?(?:/forwards?)?|bigs?(?:/centers?)?|big men)[.!]?\s*",
            question,
            re.I,
        )
        if choice:
            word = choice[1].lower()
            group = (
                "guards"
                if word.startswith("guard")
                else "wings"
                if word.startswith("wing")
                else "bigs"
            )
            item = r"(?:other\s+)?(?:guards|wings|big men|bigs)\b"
            replacement = re.sub(
                rf"\b{item}(?:\s*(?:,|and|or|/)\s*{item})*",
                group,
                pending.question,
                flags=re.I,
            )
            if len(requested_groups(replacement)) == 1:
                return replacement, None
    message = (
        WINDOW_MESSAGE
        if kind == "window_baseline"
        else "Please restate the question with one player and one comparison group, "
        "or ask for a two-player scorecard without a group comparison."
    )
    return pending.question, OverviewClarification(kind, message)
