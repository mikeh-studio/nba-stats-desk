import {displayDate} from "./semantic_presentation.js";

const escape = value => String(value ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
const publicationId = value => typeof value === "string" && /^pub_[0-9a-f]{16}$/.test(value);

function publicUrl(value) {
  try {
    const url = new URL(value);
    return url.protocol === "https:" ? url.href : "";
  } catch {
    return "";
  }
}

const text = value => typeof value === "string" ? value.trim() : "";
function cutoffLabel(cutoff) {
  if (typeof cutoff === "string") return cutoff ? `Data through ${displayDate(cutoff)}` : "";
  if (!cutoff || typeof cutoff !== "object") return "";
  const phases = Object.entries(cutoff).filter(([, value]) => text(value));
  const dates = [...new Set(phases.map(([, date]) => date))];
  return dates.length === 1 ? cutoffLabel(dates[0])
    : phases.map(([phase, date]) => `${phase}: ${displayDate(date)}`).join(" · ");
}

export function renderAskSources(payload, extra = "") {
  const coverage = payload.source_coverage;
  const pin = [payload.publication_id, coverage?.publication_id].find(publicationId);
  const evidence = payload.semantic_evidence;
  const records = [];
  const used = new Set();

  function add(label, scope, url = "", identity = "") {
    if (!label) return;
    const key = identity || url || [label, scope].join("|");
    if (used.has(key)) return;
    used.add(key);
    records.push({label, scope, url});
  }

  const sections = [
    evidence, evidence?.current, evidence?.baseline, evidence?.baseline_provenance,
    ...(Array.isArray(payload.appearance_evidence) ? payload.appearance_evidence : []),
    payload.reference_evidence, payload.research,
  ];
  if (pin) {
    const game = evidence?.game;
    const player = game ? evidence.player : payload.player_profile?.player;
    const coreHref = (path, query = {}) =>
      `/publication-core/api/${path}?${new URLSearchParams({publication_id: pin, ...query})}`;
    add("Verified publication", [cutoffLabel(coverage?.last), pin].filter(Boolean).join(" · "), "", pin);
    if (game && /^\d{10}$/.test(game.game_id) && Number.isFinite(game.home_score) && Number.isFinite(game.away_score))
      add("Accepted game results", [`Game ${game.game_id}`, pin].join(" · "));
    if (game && /^\d{4}-\d{2}-\d{2}$/.test(game.game_date))
      add("Published matchups", displayDate(game.game_date), coreHref("performance/games", {game_date: game.game_date}));
    if (game && /^\d{10}$/.test(game.game_id))
      add(evidence.rosters?.status === "ok" ? "Published player box scores for both teams" : "Published player box-score rows", "", coreHref("performance/players", {game_id: game.game_id}));
    if (Number.isSafeInteger(player?.player_id) && player.player_id > 0)
      add("Published player game log", text(player.player_name), coreHref(`players/${player.player_id}/game-log`));
  }

  for (const section of sections) {
    const source = section?.provenance || section;
    const snapshot = text(source?.snapshot_id);
    const label = text(source?.source) || (publicationId(snapshot) ? "Verified publication" : "");
    const scope = [cutoffLabel(source?.data_through), snapshot].filter(Boolean).join(" · ");
    add(label, scope, "", snapshot);
  }
  const award = payload.award_evidence;
  const awardUrl = publicUrl(award?.source_url);
  if (awardUrl) add("NBA award record", text(award.season), awardUrl);
  const studyId = text(payload.study_id);
  if (studyId) add("Published teammate study", [studyId, text(payload.study_claims?.evidence_id)].filter(Boolean).join(" · "));

  const availability = payload.availability_evidence;
  if (availability) {
    const groups = Object.values(availability.groups || {}).flat();
    const games = groups.concat(availability.excluded_games || []);
    for (const game of games) {
      for (const raw of game?.source_urls || []) {
        const url = publicUrl(raw);
        if (url) add("NBA availability evidence", displayDate(game.game_date), url);
      }
    }
  }

  if (!records.length && !extra) return "";
  const list = records.length
    ? `<ul>${records.map(record => `<li>${record.url ? `<a href="${escape(record.url)}">${escape(record.label)}</a>` : escape(record.label)}${record.scope ? ` <span class="meta">· ${escape(record.scope)}</span>` : ""}</li>`).join("")}</ul>` : "";
  return records.length > 4
    ? `<details class="ask-answer-sources"><summary>Sources (${records.length})</summary>${list}${extra}</details>`
    : `<section class="ask-answer-sources" aria-label="Sources"><h3>Sources</h3>${list}${extra}</section>`;
}
