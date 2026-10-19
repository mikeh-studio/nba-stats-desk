// Immutable current-page configuration; saved response metadata has no authority.
const pageMode = globalThis.document?.querySelector(
  'meta[name="ask-claim-enforcement"]',
)?.content;
export const claimMode = () => (pageMode === "enforce" ? "enforce" : "log");
export const withholdKnownInvalid = (payload) =>
  claimMode() === "enforce" && payload?.claim_validation?.status === "invalid";
export function withheldClaimPayload(payload) {
  const safe = {
    status: "error",
    reason_code: "claim_binding_mismatch",
    answer:
      "This answer could not be verified. Diagnostic code: claim_binding_mismatch.",
    tables: [],
    charts: [],
    assumptions: [],
  };
  for (const key of [
    "conversation_id",
    "ask_route",
    "query_plan",
    "agent_plan",
  ]) {
    try {
      const encoded = JSON.stringify(payload?.[key]);
      if (encoded !== undefined && encoded.length <= 8192)
        safe[key] = JSON.parse(encoded);
    } catch {
      /* Unsafe stored context is omitted. */
    }
  }
  return safe;
}
const routes = new Set([
  "unsupported",
  "clarify",
  "event_participation",
  "appearances",
  "availability",
  "research",
  "research_breakdown",
  "teammate_study",
  "teammate_study_v1",
  "similarity",
  "league_baseline",
  "award",
  "player_split",
  "player_comparison",
  "player_overview",
  "governed_metrics",
  "legacy:ranking",
  "legacy:player_trend",
  "legacy:game_log",
  "legacy:percentile",
  "legacy:similarity",
  "legacy:compare",
  "legacy:overview",
  "legacy:clarify",
]);
const surfaces = new Set([
  "overview",
  "comparison",
  "comparison-chart",
  "drawer",
  "unknown",
]);
const reasons = new Set([
  "none",
  "source_changed",
  "destination_mismatch",
  "binding_structure",
  "other",
]);
const states = new WeakMap();
let queue = [],
  scheduled = false,
  inFlight = false,
  dropped = 0;
const drop = (n) => {
  dropped = Math.min(1000000, dropped + n);
};
export const claimObservationDrops = () => dropped;
export function flushClaimObservations() {
  scheduled = false;
  if (inFlight || !queue.length) return;
  const batch = queue.splice(0, 16);
  if (typeof fetch !== "function") {
    drop(batch.length);
    return;
  }
  inFlight = true;
  let timer;
  Promise.resolve()
    .then(async () => {
      const controller = new AbortController();
      timer = setTimeout(() => controller.abort(), 2000);
      const response = await fetch("/api/agent/claim-observations", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify(batch),
        keepalive: true,
        signal: controller.signal,
      });
      if (!response.ok) drop(batch.length);
    })
    .catch(() => drop(batch.length))
    .finally(() => {
      clearTimeout(timer);
      inFlight = false;
      if (queue.length && !scheduled) {
        scheduled = true;
        setTimeout(flushClaimObservations, 0);
      }
    });
}
export function observeClaims(root, payload, session, report, reason) {
  try {
    observe(root, payload, session, report, reason);
  } catch {
    drop(1);
  }
}
function observe(root, payload, session, report, reason) {
  const surface = surfaces.has(session?.component)
    ? session.component
    : "unknown";
  // Selection/hover is part of the same view; changing drawer metric/view is new.
  const key =
    surface +
    ":" +
    (session?.options?.key || "") +
    ":" +
    (session?.options?.view || "");
  let state = states.get(root);
  if (!state || state.payload !== payload || state.key !== key) {
    state = { payload, key, assessed: false, invalid: false };
    states.set(root, state);
  }
  const first = !state.assessed,
    invalid = report.status === "invalid";
  if (!first && (!invalid || state.invalid)) return;
  state.assessed = true;
  state.invalid ||= invalid;
  const event = {
    version: "ask_claim_rollout/1",
    origin: "browser",
    kind: first ? "assessment" : "first_invalid",
    route: routes.has(payload?.ask_route?.route)
      ? payload.ask_route.route
      : "unknown",
    surface,
    status: invalid
      ? "invalid"
      : report.status === "complete"
        ? "complete"
        : "incomplete",
    reason: invalid
      ? reasons.has(reason) && reason !== "none"
        ? reason
        : "other"
      : "none",
    contract: "ask_ui_claim_bindings/1",
    mode: claimMode(),
    assessed: first ? 1 : 0,
    invalid: invalid ? 1 : 0,
  };
  if (queue.length < 16) queue.push(event);
  else dropped = Math.min(1000000, dropped + 1);
  if (!scheduled) {
    scheduled = true;
    setTimeout(flushClaimObservations, 0);
  }
}
