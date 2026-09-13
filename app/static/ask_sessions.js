// Open tabs are navigation, not storage. Closing/overflow never removes a chat.
export const TAB_LIMIT = 5;

export function normalizeTabs(raw = {}) {
  const open = [
    ...new Set(
      (Array.isArray(raw.open) ? raw.open : []).filter(
        (id) => typeof id === "string" && id.length && id.length < 200,
      ),
    ),
  ].slice(-TAB_LIMIT);
  return {
    open,
    active: open.includes(raw.active) ? raw.active : null,
    drafts: raw.drafts && typeof raw.drafts === "object" ? raw.drafts : {},
  };
}

export function openTab(state, id) {
  const next = normalizeTabs(state);
  if (!next.open.includes(id)) next.open.push(id);
  // Keep existing positions when switching; only a newly opened tab overflows.
  next.open = next.open.slice(-TAB_LIMIT);
  next.active = id;
  return next;
}

export function closeTab(state, id) {
  const next = normalizeTabs(state);
  const index = next.open.indexOf(id);
  next.open = next.open.filter((item) => item !== id);
  if (next.active === id)
    next.active = next.open[Math.max(0, index - 1)] || null;
  return next;
}

export function matchesSession(conversation, query) {
  const terms = String(query || "")
    .toLocaleLowerCase()
    .trim()
    .split(/\s+/);
  const text = [
    conversation.title,
    ...(conversation.turns || []).flatMap((turn) => [
      turn.question,
      turn.payload?.player_profile?.player?.player_name,
      ...(Array.isArray(turn.payload?.player_profiles)
        ? turn.payload.player_profiles.map(
            (profile) => profile?.player?.player_name,
          )
        : []),
    ]),
  ]
    .join(" ")
    .toLocaleLowerCase();
  return terms.every((term) => text.includes(term));
}
