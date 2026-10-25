"""Source-bound identity preflight and resumable clarification for every Ask route.

Only name spans are rewritten; metrics, role order, filters and dates are retained.
The request-local state carries disclosures, never a cross-request player cache.
"""

import re
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps

from app.agent.player_resolver import normalize_player_text
from app.agent.player_suggestions import (
    _CONTEXT_WORDS,
    _rank_matches,
    resolve_player_name,
)

# Task vocabulary bounds name spans. Unknown prose remains for the route's scope
# validator; this is not a general named-entity model or a permissive query parser.
_SCOPE_WORDS = _CONTEXT_WORDS | frozenset(
    """
what which where when why are am will should let us we our your get find count
many much number appearances appeared appearance log logs totals total rate rates
percent percentage percentile percentiles ranking rankings rank ranked top bottom
league nba baseline peer peers similar similarity most least best worst median
career split splits home away road wins losses win loss won lost against opponent
opponents teammate teammates without out absent absence missed miss sat sitting
sidelined injury injured unavailable availability both together also only include
exclude including excluding while not no yes same different higher lower more less
shooting true effective field goal goals free throw throws three two made makes
attempts attempts attempted turnovers minutes scoring rebounding usage efficiency
per36 ppg rpg apg fg fga fgm ft fta ftm fg3a fg3m ts efg plus minus stat line
breakdown descriptive research study studies adjusted unadjusted estimate impact
effect significant significance statistically statistical reliable reliability
uncertainty confidence interval causal cause pattern difference distribution
fantasy score weighted proxy summarize analyze break down before after as improved improve trade
over like resembles alike nearest trending beside besides other playmaking leads measure
trend trends chart table plot rolling recent previous past next first second third
fourth five ten all each every minimum maximum at least qualified eligible players
january february march april may june july august september october november december
monday tuesday wednesday thursday friday saturday sunday today yesterday tonight
mvp rookie award awards defensive finals winner winners year defensive offense
basketball regular post season postseason round conference eastern western cup
boston celtics brooklyn nets new york knicks philadelphia sixers toronto raptors
chicago bulls cleveland cavaliers detroit pistons indiana pacers milwaukee bucks
atlanta hawks charlotte hornets miami heat orlando magic washington wizards
denver nuggets minnesota timberwolves oklahoma city thunder portland trail blazers
utah jazz golden state warriors los angeles clippers lakers phoenix suns sacramento
kings dallas mavericks houston rockets memphis grizzlies orleans pelicans san antonio
spurs selected clarification player use using compared played playing performing
box scores there any game where scored blocked shots dunks dunk layups layup jumpers
heaves putbacks floaters hooks corner catch pullup pullups
""".split()
)
_WORD = re.compile(r"[^\W\d_]+(?:['’.-][^\W\d_]+)*", re.UNICODE)


class PlayerNameError(Exception):
    def __init__(self, mention, result):
        super().__init__(result.get("message", "Please select the intended player."))
        self.mention, self.result = mention, result


@dataclass
class NameSession:
    choices: dict = field(default_factory=dict)
    notices: list = field(default_factory=list)
    resolutions: list = field(default_factory=list)
    selected_ids: set = field(default_factory=set)


_session: ContextVar[NameSession | None] = ContextVar("ask_name_matching", default=None)


def player_display_name(player):
    team = (
        "/".join(player["observed_teams"])
        if "observed_teams" in player
        else player.get("team_abbr") or player.get("latest_team_abbr")
    )
    return f"{player['player_name']} · {team}" if team else player["player_name"]


def record_resolution(raw, result):
    """Use the same choices and disclosures for compatibility/tool name slots."""
    session = _session.get()
    if not session:
        return result
    choice = session.choices.get(normalize_player_text(raw))
    if choice:
        chosen = next(
            (
                p
                for p in result["matches"]
                if p["player_id"] == choice["player_id"]
                and p["player_name"] == choice["player_name"]
            ),
            None,
        )
        if chosen:
            session.notices.append(
                f"Using {player_display_name(chosen)}, as you selected."
            )
            return {"status": "ok", "matches": [chosen]}
    if result.get("resolution") and result["resolution"] not in session.resolutions:
        session.resolutions.append(result["resolution"])
        session.notices.append(result["notice"])
    return result


def _spans(question, players):
    """Find exact names and bounded plausible name phrases, retaining offsets."""
    exact = []
    tokens = list(_WORD.finditer(question))
    # Try complete spelling candidates before a short exact alias can consume
    # the first/last token ("LeBron Jams" must not become "LeBron" + "Jams").
    for i in range(len(tokens)):
        for width in (2, 3, 4):
            group = tokens[i : i + width]
            if len(group) != width or any(
                normalize_player_text(m.group()) in _SCOPE_WORDS for m in group
            ):
                continue
            if any(
                question[a.end() : b.start()].strip() for a, b in zip(group, group[1:])
            ):
                continue
            raw = re.sub(
                r"['’]s$", "", question[group[0].start() : group[-1].end()], flags=re.I
            )
            result = resolve_player_name(raw, players)
            # Don't expand a shorter matching name across an unrelated token.
            _, ranked = _rank_matches(raw, players)
            if (
                result["matches"]
                and ranked
                and all(
                    matched == normalize_player_text(raw) for _, _, _, matched in ranked
                )
            ):
                exact.append((group[0].start(), group[0].start() + len(raw)))
    for player in players:
        for name in (player["player_name"], *player.get("aliases", [])):
            if not name:
                continue
            for m in re.finditer(
                r"(?<!\w)" + re.escape(name) + r"(?!\w)", question, re.I
            ):
                exact.append((m.start(), m.end()))
    exact = sorted(set(exact), key=lambda s: (s[0], -(s[1] - s[0])))
    # Don't accept a familiar short alias while discarding an unfamiliar
    # surname: "Jalen Zqxv" is not simply "Jalen".
    for left, right in zip(tokens, tokens[1:]):
        if question[left.end() : right.start()].strip():
            continue
        if any(normalize_player_text(m.group()) in _SCOPE_WORDS for m in (left, right)):
            continue
        pair = (left.start(), right.end())
        if any(
            s in {(left.start(), left.end()), (right.start(), right.end())}
            for s in exact
        ) and not any(a <= pair[0] and b >= pair[1] for a, b in exact):
            exact.append(pair)
    exact = [
        s
        for s in exact
        if not any(a <= s[0] and b >= s[1] and (a, b) != s for a, b in exact)
    ]
    groups, group = [], []
    for m in tokens:
        text = re.sub(r"['’]s$", "", m.group(), flags=re.I)
        key = normalize_player_text(text)
        protected = any(a < m.end() and b > m.start() for a, b in exact)
        boundary = key in _SCOPE_WORDS or (text.isupper() and len(text) <= 4)
        if protected or boundary:
            if group:
                groups.append(group)
                group = []
            continue
        if group and question[group[-1].end() : m.start()].strip():
            groups.append(group)
            group = []
        group.append(m)
    if group:
        groups.append(group)
    spans = list(exact)
    for group in groups:
        a, b = group[0].start(), group[-1].end()
        raw = re.sub(r"['’]s$", "", question[a:b], flags=re.I)
        b = a + len(raw)
        result = resolve_player_name(raw, players)
        # A full unknown personal name must fail closed, including lowercase
        # names in explicit player slots. Other unfamiliar prose is left intact.
        slot = (
            a == 0
            and bool(
                re.match(
                    r"\s+(?:points?|rebounds?|assists?|stats?|performance|play|played|games?|shooting|overview)\b",
                    question[b:],
                    re.I,
                )
            )
        ) or bool(
            re.search(
                r"(?:\b(?:did|does|has|have|is|was|for|without|about|player|to|with|of|show|compare|research|analyze|review|summarize)|^how)\s*$",
                question[:a],
                re.I,
            )
        )
        if result["matches"] or (slot and 2 <= len(group) <= 4):
            spans.append((a, b))
    return sorted(set(spans))


def prepare_names(question, players, *, strict=True, skip_not_found=False):
    """Resolve name spans against this route's roster, before planning/execution.

    strict=False is only for canonicalizing original wording in policy metadata,
    after the effective (possibly contextual) execution question was validated.
    skip_not_found=True lets host preflight inspect later candidate-bearing spans;
    ordinary identity validation still rejects unknown names by default.
    """
    session = _session.get()
    # Legacy selected-player paths pass the choice through their own roster
    # validation. The common request wrapper handles new clarifications.
    replacements = []
    from app.agent.player_resolver import load_player_aliases

    aliases = load_player_aliases()
    roster = [
        dict(
            p,
            aliases=sorted(
                set(p.get("aliases", []))
                | {
                    a
                    for a, target in aliases.items()
                    if normalize_player_text(target)
                    == normalize_player_text(p["player_name"])
                }
            ),
        )
        for p in players
    ]
    for start, end in _spans(question, roster):
        raw = question[start:end]
        key = normalize_player_text(raw)
        result = resolve_player_name(raw, roster)
        choice = session.choices.get(key) if session else None
        if choice:
            chosen = next(
                (
                    p
                    for p in result["matches"]
                    if p["player_id"] == choice["player_id"]
                    and p["player_name"] == choice["player_name"]
                ),
                None,
            )
            if not chosen:
                raise PlayerNameError(
                    raw,
                    {
                        "status": "not_found",
                        "matches": [],
                        "message": "The selected player is not available in this request's data scope.",
                    },
                )
            result = {"status": "ok", "matches": [chosen]}
            # Duplicate canonical names remain distinct until explicitly selected.
            roster = [
                p
                for p in roster
                if p["player_id"] == chosen["player_id"]
                or normalize_player_text(p["player_name"])
                != normalize_player_text(chosen["player_name"])
            ]
            note = f"Using {player_display_name(chosen)}, as you selected for ‘{raw}’."
            if chosen["player_id"] not in session.selected_ids:
                session.notices.append(note)
                session.selected_ids.add(chosen["player_id"])
        if result["status"] != "ok":
            if not strict or (skip_not_found and result["status"] == "not_found"):
                continue
            raise PlayerNameError(raw, result)
        chosen = result["matches"][0]
        replacements.append((start, end, chosen["player_name"]))
        if (
            session
            and result.get("resolution")
            and result["resolution"] not in session.resolutions
        ):
            session.resolutions.append(result["resolution"])
            session.notices.append(result["notice"])
    for start, end, name in reversed(replacements):
        question = question[:start] + name + question[end:]
    return question, roster


def match_name_question(question, players):
    """Apply fallback matching to extracted names, not surrounding prose."""
    from app.agent.player_suggestions import match_player_name

    original = question.split("\nSelected player:", 1)[0].split("\nClarification:", 1)[
        0
    ]
    spans = _spans(original, players)
    if len(spans) == 1:
        a, b = spans[0]
        return resolve_player_name(original[a:b], players)
    if spans:
        results = [resolve_player_name(original[a:b], players) for a, b in spans]
        missing = next((r for r in results if r["status"] == "not_found"), None)
        if missing:
            return missing
        matches = {p["player_id"]: p for r in results for p in r["matches"]}
        return {
            "status": "clarification_required",
            "matches": list(matches.values())[:5],
            "message": "More than one player was named. Please clarify the intended player.",
        }
    return match_player_name(original, players)


def apply_name_notices(payload):
    """Disclose identity changes before renderer-owned claim binding."""
    session = _session.get()
    if not session:
        return payload
    applied = payload.setdefault("name_resolution_notices", [])
    notes = [n for n in dict.fromkeys(session.notices) if n not in applied]
    if notes:
        prefix = " ".join(notes)
        payload["answer"] = prefix + "\n\n" + payload["answer"]
        payload.setdefault("assumptions", []).extend(notes)
        applied.extend(notes)
        if payload.get("appearance_evidence"):
            payload.setdefault("claim_evidence", {})["name_resolution_notice"] = (
                " ".join(applied)
            )
    payload.setdefault("name_resolutions", []).extend(
        r for r in session.resolutions if r not in payload.get("name_resolutions", [])
    )
    return payload


def identity_payload(error, question, conversation_id, model_calls=None):
    from app.agent.payload import answer_payload

    session = _session.get()
    result = error.result
    options = [
        dict(
            player_id=p["player_id"],
            player_name=p["player_name"],
            team_abbr=(
                "/".join(p["observed_teams"])
                if "observed_teams" in p
                else p.get("team_abbr")
            ),
            label=p["player_name"],
        )
        for p in result["matches"]
    ]
    plan = {
        "kind": "identity",
        "name_matching": True,
        "mention": error.mention,
        "choices": session.choices if session else {},
    }
    if model_calls is not None:
        plan["model_calls"] = model_calls
    return answer_payload(
        str(error),
        status=result["status"],
        conversation_id=conversation_id,
        clarification_options=options,
        identity=result,
        semantic_plan=plan,
        clarification_question=question,
    )


def name_matching_entrypoint(answer):
    """Give direct semantic callers the same resumable identity boundary."""

    @wraps(answer)
    def wrapped(agent, question, **kwargs):
        if _session.get() is not None:
            return answer(agent, question, **kwargs)
        return run_name_matching(
            agent,
            question,
            kwargs.get("selected_player"),
            kwargs.get("conversation_id"),
            lambda effective, selected: answer(
                agent, effective, **{**kwargs, "selected_player": selected}
            ),
            trace=kwargs.get("trace"),
        )

    return wrapped


def run_name_matching(
    agent, question, selected_player, conversation_id, callback, *, trace=None
):
    """Keep clarification replies on the original route and original scope."""
    from app.agent.scope_policy import name_reply, starts_new_question

    store = (
        getattr(agent, "conversation_store", getattr(agent, "store", None))
        if conversation_id
        else None
    )
    pending = store.get_pending_clarification(conversation_id) if store else None
    session = NameSession()
    from app.agent.routes import supersedes_pending

    if (
        pending
        and (pending.query_plan or {}).get("name_matching")
        and not selected_player
        and supersedes_pending(question, pending)
        and not any(
            str(p.get("team_abbr") or "").casefold() == question.strip().casefold()
            for p in pending.candidates
        )
        and resolve_player_name(question, pending.candidates)["status"] != "ok"
    ):
        store.clear_pending_clarification(conversation_id)
        pending = None
    if pending and (pending.query_plan or {}).get("name_matching"):
        if selected_player or (
            name_reply(question) and not starts_new_question(question)
        ):
            candidates = list(pending.candidates)
            reply = re.sub(
                r"^(?:(?:show|give|tell) me|i (?:mean|meant)|use|pick|select)\s+",
                "",
                question.strip(),
                flags=re.I,
            ).strip(" .!?")
            match = resolve_player_name(reply, candidates)
            teams = [
                p
                for p in candidates
                if str(p.get("team_abbr") or "").casefold() == reply.casefold()
            ]
            if match["status"] != "ok" and len(teams) == 1:
                match = {"status": "ok", "matches": teams}
            choice = selected_player or (
                match["matches"][0] if match["status"] == "ok" else None
            )
            session.choices = dict(pending.query_plan.get("choices", {}))
            if choice and any(
                p["player_id"] == choice.get("player_id")
                and p["player_name"] == choice.get("player_name")
                for p in candidates
            ):
                session.choices[
                    normalize_player_text(pending.query_plan["mention"])
                ] = choice
                # Allow a second preflight after the original spelling is replaced.
                session.choices[normalize_player_text(choice["player_name"])] = choice
            question = pending.question
            selected_player = None
        store.clear_pending_clarification(conversation_id)
    token = _session.set(session)
    try:
        try:
            payload = callback(question, selected_player)
        except PlayerNameError as exc:
            result = exc.result
            payload = identity_payload(
                exc, question, conversation_id, trace.model_calls if trace else None
            )
            if trace:
                trace.outcome = (
                    "clarified"
                    if result["status"] == "clarification_required"
                    else result["status"]
                )
            if store and result["status"] == "clarification_required":
                store.set_pending_clarification(
                    conversation_id,
                    question=question,
                    query_plan={
                        "name_matching": True,
                        "mention": exc.mention,
                        "choices": session.choices,
                    },
                    candidates=payload["clarification_options"],
                )
        apply_name_notices(payload)
        return payload
    finally:
        _session.reset(token)
