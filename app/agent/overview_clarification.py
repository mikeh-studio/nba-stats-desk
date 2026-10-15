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
    """Return question, error, and whether ordinary follow-up context is needed."""
    kind = pending.query_plan["overview_clarification"]
    text = re.sub(
        r"^\s*(?:(?:please|ok(?:ay)?|so|then|now)\b[,\s]*|"
        r"(?:can|could|would)\s+you\s+|I\s+want\s+(?:to\s+)?)+",
        "",
        question,
        flags=re.I,
    )
    if kind == "groups":
        choice = re.fullmatch(
            r"\s*(?:use\s+|only\s+|(?:how|what)\s+about\s+)?"
            r"(guards?|wings?(?:/forwards?)?|forwards?|bigs?(?:/centers?)?|centers?|big men)[.!?]?\s*",
            text,
            re.I,
        )
        if choice:
            word = choice[1].lower()
            group = (
                "guards"
                if word.startswith("guard")
                else "wings"
                if word.startswith(("wing", "forward"))
                else "bigs"
            )
            item = r"(?:other\s+)?(?:guards|wings|big men|bigs)\b"
            replacement = re.sub(
                rf"\b{item}(?:\s*(?:,\s*(?:(?:and|or)\s+)?|(?:and|or|/)\s*){item})*",
                group,
                pending.question,
                flags=re.I,
            )
            if len(requested_groups(replacement)) == 1:
                return replacement, None, False
    # A baseline-only fragment is not a new current-period question, including
    # when an earlier answer supplies an identity through conversation context.
    baseline_reply = kind == "window_baseline" and re.match(
        r"^\s*(?:(?:what|how)\s+about\b|compare\s+(?:with|to|against)\b|use\b|from\b)",
        text,
        re.I,
    )
    if not baseline_reply:
        # Restatements keep normal identity/date resolution, never raw history
        # concatenation. Explicit new dates supersede the prior answer's dates.
        if re.match(
            r"^\s*(?:give|show|how|what|who|which|compare|tell|analy[sz]e|summari[sz]e|overview|profile|stats|statistics)\b",
            text,
            re.I,
        ):
            return text, None, True
        entity = re.match(
            r"^(.+?)\s+(?:among|against|v(?:s\.?|ersus))\s+(?:other\s+)?"
            r"(?:guards|wings|bigs|big men)\b",
            text,
            re.I,
        )
        if entity and len(entity[1].split()) >= 2:
            return "Give an overview of " + text, None, True
    message = (
        WINDOW_MESSAGE
        if kind == "window_baseline"
        else "Please restate the question with one player and one comparison group, "
        "or ask for a two-player scorecard without a group comparison."
    )
    return pending.question, OverviewClarification(kind, message), False
