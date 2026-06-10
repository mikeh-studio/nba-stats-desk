function escHtml(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

export const TRACKING_KEY = "nba_workbench_tracked_players_v1";
export const TRACKING_VERSION = 1;
export const TRACKING_CAP = 8;

function isObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function normalizePlayerIds(playerIds, cap = TRACKING_CAP) {
  if (!Array.isArray(playerIds)) {
    return [];
  }
  const normalized = [];
  for (const value of playerIds) {
    if (!Number.isInteger(value)) {
      return [];
    }
    if (!normalized.includes(value)) {
      normalized.push(value);
    }
  }
  if (normalized.length > cap) {
    return [];
  }
  return normalized;
}

export function normalizeTrackedPayload(payload, cap = TRACKING_CAP) {
  if (!isObject(payload) || payload.version !== TRACKING_VERSION) {
    return { version: TRACKING_VERSION, player_ids: [] };
  }
  const playerIds = normalizePlayerIds(payload.player_ids, cap);
  if (playerIds.length !== payload.player_ids.length) {
    return { version: TRACKING_VERSION, player_ids: [] };
  }
  return { version: TRACKING_VERSION, player_ids: playerIds };
}

export function parseTrackedPayload(raw, cap = TRACKING_CAP) {
  if (typeof raw !== "string" || !raw.trim()) {
    return { version: TRACKING_VERSION, player_ids: [] };
  }
  try {
    return normalizeTrackedPayload(JSON.parse(raw), cap);
  } catch {
    return { version: TRACKING_VERSION, player_ids: [] };
  }
}

export function serializeTrackedPayload(payload) {
  return JSON.stringify(payload);
}

export function loadTrackedPlayers(storage, cap = TRACKING_CAP) {
  if (!storage || typeof storage.getItem !== "function") {
    return { version: TRACKING_VERSION, player_ids: [] };
  }
  const normalized = parseTrackedPayload(storage.getItem(TRACKING_KEY), cap);
  storage.setItem(TRACKING_KEY, serializeTrackedPayload(normalized));
  return normalized;
}

export function saveTrackedPlayers(storage, payload) {
  if (!storage || typeof storage.setItem !== "function") {
    return;
  }
  storage.setItem(TRACKING_KEY, serializeTrackedPayload(payload));
}

export function addTrackedPlayer(payload, playerId, cap = TRACKING_CAP) {
  if (!Number.isInteger(playerId)) {
    return { payload, error: "invalid_player_id" };
  }
  if (payload.player_ids.includes(playerId)) {
    return { payload, error: null };
  }
  if (payload.player_ids.length >= cap) {
    return { payload, error: "cap_reached" };
  }
  return {
    payload: {
      version: TRACKING_VERSION,
      player_ids: [...payload.player_ids, playerId],
    },
    error: null,
  };
}

export function removeTrackedPlayer(payload, playerId) {
  return {
    version: TRACKING_VERSION,
    player_ids: payload.player_ids.filter((value) => value !== playerId),
  };
}

export function buildCompareHref(playerAId, playerBId, window, focus = "balanced") {
  return `/compare?player_a_id=${playerAId}&player_b_id=${playerBId}&window=${encodeURIComponent(window)}&focus=${encodeURIComponent(focus)}`;
}

function getStorage() {
  try {
    return globalThis.localStorage;
  } catch {
    return null;
  }
}

function formatTrackedSummary(count, cap) {
  return `${count}/${cap} tracked in this browser`;
}

function formatTimeAgo(value) {
  if (!value) {
    return "unavailable";
  }
  const parsed = Date.parse(value);
  if (!Number.isFinite(parsed)) {
    return String(value);
  }
  const seconds = Math.max(0, Math.floor((Date.now() - parsed) / 1000));
  if (seconds < 60) {
    return "just now";
  }
  if (seconds < 3600) {
    const minutes = Math.floor(seconds / 60);
    return `${minutes} minute${minutes === 1 ? "" : "s"} ago`;
  }
  if (seconds < 86400) {
    const hours = Math.floor(seconds / 3600);
    return `${hours} hour${hours === 1 ? "" : "s"} ago`;
  }
  const days = Math.floor(seconds / 86400);
  return `${days} day${days === 1 ? "" : "s"} ago`;
}

function renderHealthStatus(node, payload) {
  const status = payload?.status || "unavailable";
  const lastRefresh = payload?.last_successful_finished_at_utc || "";
  node.classList.toggle("loading", false);
  node.dataset.healthState = status;
  if (lastRefresh) {
    node.title = lastRefresh;
  }
  if (status === "fresh") {
    node.textContent = "Data fresh";
    return;
  }
  if (lastRefresh) {
    node.textContent = `Last refresh ${formatTimeAgo(lastRefresh)}`;
    return;
  }
  node.textContent = "Data status unavailable";
}

let healthStatusStarted = false;

async function setupHealthStatus() {
  if (healthStatusStarted) {
    return;
  }
  healthStatusStarted = true;
  const nodes = Array.from(document.querySelectorAll("[data-health-status]"));
  if (nodes.length === 0) {
    return;
  }
  nodes.forEach((node) => {
    node.classList.add("loading");
  });
  try {
    const response = await fetch("/api/health", { cache: "default" });
    if (!response.ok) {
      throw new Error("health unavailable");
    }
    const payload = await response.json();
    nodes.forEach((node) => renderHealthStatus(node, payload));
  } catch {
    nodes.forEach((node) => {
      node.classList.toggle("loading", false);
      node.dataset.healthState = "unavailable";
      node.textContent = "Data status unavailable";
    });
  }
}

function renderHeadshot(player) {
  const initials = escHtml(player.player_initials || "NBA");
  const imageMarkup = player.headshot_url
    ? `<img src="${escHtml(player.headshot_url)}" alt="" loading="lazy" onerror="this.hidden=true; this.nextElementSibling.hidden=false;" />`
    : "";
  return `
    <div class="player-avatar" aria-hidden="true">
      ${imageMarkup}
      <span class="player-avatar-fallback">${initials}</span>
    </div>
  `;
}

function hydrateTrackedCard(item) {
  const detail = item.item || {};
  const player = detail.player || {};
  const state = detail.availability_state || "unavailable";
  const reason = detail.reason_summary || detail.availability_reason || "Status unavailable";
  return `
    <article class="tracked-card">
      <div class="card-header">
        <div class="identity-row">
          ${renderHeadshot(player)}
          <div>
            <h3><a href="/players/${escHtml(player.player_id)}">${escHtml(player.player_name) || "Unknown player"}</a></h3>
            <p class="meta">${escHtml(player.team_abbr) || "NBA"} · ${escHtml(state)}</p>
          </div>
        </div>
        <button class="track-button secondary" type="button" data-track-button data-player-id="${escHtml(player.player_id)}">
          Remove
        </button>
      </div>
      <p>${escHtml(reason)}</p>
      <div class="chip-row">
        ${player.overall_rank ? `<span class="chip">Rank #${escHtml(player.overall_rank)}</span>` : `<span class="chip">Unranked</span>`}
        ${player.recommendation_score ? `<span class="chip">Score ${escHtml(player.recommendation_score)}</span>` : ""}
        <a class="button-link secondary" href="/compare?player_a_id=${escHtml(player.player_id)}">Compare</a>
      </div>
    </article>
  `;
}

async function fetchTrackedPlayer(playerId) {
  const response = await fetch(`/api/players/${playerId}`);
  if (!response.ok) {
    return {
      item: {
        player: {
          player_id: playerId,
          player_name: `Player ${playerId}`,
          team_abbr: null,
          overall_rank: null,
          recommendation_score: null,
        },
        availability_state: "unavailable",
        availability_reason: "Player not found",
        reason_summary: null,
      },
    };
  }
  return response.json();
}

function syncTrackButtons(payload, cap) {
  const trackedIds = new Set(payload.player_ids);
  document.querySelectorAll("[data-track-button]").forEach((button) => {
    const playerId = Number(button.dataset.playerId);
    const isTracked = trackedIds.has(playerId);
    button.disabled = !isTracked && payload.player_ids.length >= cap;
    button.textContent = isTracked
      ? "Untrack player"
      : payload.player_ids.length >= cap
        ? "Tracked max reached"
        : "Track player";
  });
}

async function renderTrackedRail(payload, cap) {
  const rail = document.querySelector("[data-tracked-rail]");
  if (!rail) {
    return;
  }
  const grid = rail.querySelector("[data-tracked-grid]");
  const empty = rail.querySelector("[data-tracked-empty]");
  const summary = rail.querySelector("[data-tracked-summary]");
  const capMessage = rail.querySelector("[data-tracked-cap-message]");
  if (!grid || !empty || !summary || !capMessage) {
    return;
  }

  summary.textContent = formatTrackedSummary(payload.player_ids.length, cap);
  capMessage.hidden = payload.player_ids.length < cap;

  if (payload.player_ids.length === 0) {
    grid.innerHTML = "";
    empty.hidden = false;
    return;
  }

  empty.hidden = true;
  const responses = await Promise.all(payload.player_ids.map(fetchTrackedPlayer));
  grid.innerHTML = responses.map(hydrateTrackedCard).join("");
  syncTrackButtons(payload, cap);
}

async function toggleTrackedPlayer(playerId) {
  const storage = getStorage();
  if (!storage) {
    return;
  }
  const cap = Number(document.body.dataset.trackingCap || TRACKING_CAP);
  const payload = loadTrackedPlayers(storage, cap);
  const isTracked = payload.player_ids.includes(playerId);
  const nextPayload = isTracked
    ? removeTrackedPlayer(payload, playerId)
    : addTrackedPlayer(payload, playerId, cap).payload;
  saveTrackedPlayers(storage, nextPayload);
  syncTrackButtons(nextPayload, cap);
  await renderTrackedRail(nextPayload, cap);
}

function setupTrackButtons() {
  document.addEventListener("click", async (event) => {
    const target = event.target;
    if (!(target instanceof HTMLElement) || !target.matches("[data-track-button]")) {
      return;
    }
    const playerId = Number(target.dataset.playerId);
    if (!Number.isInteger(playerId)) {
      return;
    }
    event.preventDefault();
    await toggleTrackedPlayer(playerId);
  });
}

function setupCompareSearch() {
  document.querySelectorAll("[data-compare-search-form]").forEach((formNode) => {
    if (!(formNode instanceof HTMLFormElement)) {
      return;
    }
    formNode.addEventListener("submit", async (event) => {
      event.preventDefault();
      const playerAId = Number(formNode.dataset.playerAId);
      const input = formNode.querySelector('input[name="q"]');
      const windowSelect = formNode.querySelector('select[name="window"]');
      const focusSelect = formNode.querySelector('select[name="focus"]');
      const message = formNode.querySelector("[data-compare-search-message]");
      if (
        !Number.isInteger(playerAId) ||
        !(input instanceof HTMLInputElement) ||
        !(windowSelect instanceof HTMLSelectElement) ||
        !(focusSelect instanceof HTMLSelectElement) ||
        !(message instanceof HTMLElement)
      ) {
        return;
      }
      const query = input.value.trim();
      if (!query) {
        return;
      }
      message.textContent = "";
      try {
        const response = await fetch(`/api/players/search?q=${encodeURIComponent(query)}`);
        const data = await response.json();
        if (!response.ok) {
          message.textContent = data.detail || "Search failed";
          return;
        }
        if (!data.items || data.items.length === 0) {
          message.textContent = "No results found";
          return;
        }
        const playerBId = data.items[0].player_id;
        globalThis.location.href = buildCompareHref(
          playerAId,
          playerBId,
          windowSelect.value,
          focusSelect.value
        );
      } catch {
        message.textContent = "Search failed";
      }
    });
  });
}

async function doComparePlayerASearch(query, resultsEl) {
  try {
    const resp = await fetch(`/api/players/search?q=${encodeURIComponent(query)}`);
    if (!resp.ok) return;
    const data = await resp.json();
    if (!data.items || data.items.length === 0) {
      resultsEl.innerHTML = '<div class="viz-search-result"><span class="meta">No results</span></div>';
      resultsEl.hidden = false;
      return;
    }
    resultsEl.innerHTML = data.items
      .map(
        (p) =>
          `<button class="viz-search-result" data-player-id="${escHtml(p.player_id)}">
            ${escHtml(p.player_name)}
            <span class="meta">${escHtml(p.latest_season)}</span>
          </button>`
      )
      .join("");
    resultsEl.hidden = false;
    resultsEl.querySelectorAll(".viz-search-result[data-player-id]").forEach((btn) => {
      btn.addEventListener("click", () => {
        globalThis.location.href = `/compare?player_a_id=${btn.dataset.playerId}`;
      });
    });
  } catch {
    /* swallow */
  }
}

function setupComparePlayerASearch() {
  const input = document.querySelector("[data-compare-player-a-search]");
  const resultsEl = document.getElementById("compare-player-a-results");
  if (!input || !resultsEl) return;

  let searchTimeout = null;

  input.addEventListener("input", () => {
    clearTimeout(searchTimeout);
    const q = input.value.trim();
    if (q.length < 2) { resultsEl.hidden = true; return; }
    searchTimeout = setTimeout(() => doComparePlayerASearch(q, resultsEl), 280);
  });

  input.addEventListener("keydown", (e) => {
    if (e.key === "Escape") resultsEl.hidden = true;
  });

  document.addEventListener("click", (e) => {
    if (!e.target.closest(".compare-pa-search-row")) resultsEl.hidden = true;
  });
}

function renderPlayerSearchResult(player) {
  const rank = player.overall_rank ? `Rank #${escHtml(player.overall_rank)}` : "Qualified";
  const team = player.latest_team_abbr || "NBA";
  const games = player.games_sampled ? `${player.games_sampled} games` : "5+ games";
  return `
    <button class="player-search-result" type="button" data-player-id="${escHtml(player.player_id)}">
      ${renderHeadshot(player)}
      <span>
        <strong>${escHtml(player.player_name)}</strong>
        <span class="meta">${escHtml(team)} · ${escHtml(games)}</span>
      </span>
      <span class="status">${rank}</span>
    </button>
  `;
}

async function runQualifiedPlayerSearch(formNode, query, { navigateFirst = false } = {}) {
  const resultsEl = formNode.querySelector("[data-player-search-results]");
  if (!(resultsEl instanceof HTMLElement)) {
    return;
  }
  try {
    const response = await fetch(`/api/players/search?q=${encodeURIComponent(query)}`);
    const data = await response.json();
    if (!response.ok) {
      resultsEl.innerHTML = `<div class="empty-state"><strong>${escHtml(data.detail || "Search failed")}</strong></div>`;
      resultsEl.hidden = false;
      return;
    }
    const items = Array.isArray(data.items) ? data.items : [];
    if (navigateFirst && items[0]) {
      globalThis.location.href = `/players/${items[0].player_id}`;
      return;
    }
    if (items.length === 0) {
      resultsEl.innerHTML = '<div class="empty-state"><strong>No qualified players found.</strong></div>';
      resultsEl.hidden = false;
      return;
    }
    resultsEl.innerHTML = items.map(renderPlayerSearchResult).join("");
    resultsEl.hidden = false;
    resultsEl.querySelectorAll("[data-player-id]").forEach((button) => {
      button.addEventListener("click", () => {
        globalThis.location.href = `/players/${button.dataset.playerId}`;
      });
    });
  } catch {
    resultsEl.innerHTML = '<div class="empty-state"><strong>Search failed.</strong></div>';
    resultsEl.hidden = false;
  }
}

function setupQualifiedPlayerSearch() {
  document.querySelectorAll("[data-player-search-form]").forEach((formNode) => {
    if (!(formNode instanceof HTMLFormElement)) {
      return;
    }
    const input = formNode.querySelector("[data-player-search-input]");
    const resultsEl = formNode.querySelector("[data-player-search-results]");
    if (!(input instanceof HTMLInputElement) || !(resultsEl instanceof HTMLElement)) {
      return;
    }
    let searchTimeout = null;
    input.addEventListener("input", () => {
      clearTimeout(searchTimeout);
      const query = input.value.trim();
      if (query.length < 2) {
        resultsEl.hidden = true;
        return;
      }
      searchTimeout = setTimeout(() => runQualifiedPlayerSearch(formNode, query), 220);
    });
    input.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        resultsEl.hidden = true;
      }
    });
    formNode.addEventListener("submit", (event) => {
      event.preventDefault();
      const query = input.value.trim();
      if (query) {
        runQualifiedPlayerSearch(formNode, query, { navigateFirst: true });
      }
    });
  });

  document.addEventListener("click", (event) => {
    const target = event.target;
    if (target instanceof Element && target.closest("[data-player-search-form]")) {
      return;
    }
    document.querySelectorAll("[data-player-search-results]").forEach((resultsEl) => {
      if (resultsEl instanceof HTMLElement) {
        resultsEl.hidden = true;
      }
    });
  });
}

function setupTabs() {
  document.querySelectorAll("[data-tab-group]").forEach((group) => {
    const buttons = Array.from(group.querySelectorAll("[data-tab-target]"));
    const panels = Array.from(group.querySelectorAll("[data-tab-panel]"));
    const activate = (targetName) => {
      buttons.forEach((button) => {
        const isActive = button.dataset.tabTarget === targetName;
        button.classList.toggle("is-active", isActive);
        button.setAttribute("aria-selected", isActive ? "true" : "false");
      });
      panels.forEach((panel) => {
        const isActive = panel.dataset.tabPanel === targetName;
        panel.classList.toggle("is-active", isActive);
        panel.hidden = !isActive;
      });
    };
    buttons.forEach((button) => {
      button.addEventListener("click", () => activate(button.dataset.tabTarget));
    });
  });
}

const TREND_STATS = {
  pts: { label: "PTS" },
  reb: { label: "REB" },
  ast: { label: "AST" },
  stl: { label: "STL" },
  blk: { label: "BLK" },
  tov: { label: "TOV" },
};

function formatChartNumber(value) {
  if (!Number.isFinite(value)) {
    return "";
  }
  return Number.isInteger(value) ? String(value) : value.toFixed(1);
}

function buildTrendTooltip(point, stat, baselineValue, width) {
  const tooltipWidth = 154;
  const tooltipHeight = baselineValue === null ? 42 : 56;
  const x = Math.min(width - tooltipWidth - 8, Math.max(8, point.x - tooltipWidth / 2));
  const y = point.y - tooltipHeight - 12 < 8 ? point.y + 14 : point.y - tooltipHeight - 12;
  const baselineText =
    baselineValue === null
      ? ""
      : `<text class="trend-tooltip-text" x="${x + 10}" y="${y + 47}">Lg avg ${formatChartNumber(baselineValue)}</text>`;
  return `
    <g class="trend-tooltip">
      <rect class="trend-tooltip-box" x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${tooltipWidth}" height="${tooltipHeight}" rx="8" />
      <text class="trend-tooltip-text" x="${x + 10}" y="${y + 18}">${escHtml(point.date)}</text>
      <text class="trend-tooltip-text" x="${x + 10}" y="${y + 33}">${escHtml(stat.label)} ${formatChartNumber(point.value)}</text>
      ${baselineText}
    </g>
  `;
}

function buildTrendSvg(games, statKey, baselines = {}) {
  const stat = TREND_STATS[statKey] || TREND_STATS.pts;
  const baselineValue = Number(baselines[statKey]?.value);
  const hasBaseline = Number.isFinite(baselineValue);
  const values = games
    .map((game, index) => ({
      index,
      date: game.game_date,
      value: Number(game[statKey]),
    }))
    .filter((point) => Number.isFinite(point.value));
  if (values.length === 0) {
    return '<div class="empty-state"><strong>No trend data available.</strong></div>';
  }

  const width = 720;
  const height = 250;
  const pad = { top: 26, right: 28, bottom: 46, left: 46 };
  const innerWidth = width - pad.left - pad.right;
  const innerHeight = height - pad.top - pad.bottom;
  const maxValue = Math.max(
    ...values.map((point) => point.value),
    hasBaseline ? baselineValue : 0,
    1
  );
  const yMax = Math.max(1, Math.ceil(maxValue * 1.15));
  const bottom = pad.top + innerHeight;
  const points = values.map((point, index) => {
    const x =
      values.length === 1
        ? pad.left + innerWidth / 2
        : pad.left + (index / (values.length - 1)) * innerWidth;
    const y = bottom - (point.value / yMax) * innerHeight;
    return { ...point, x, y };
  });
  const linePoints = points.map((point) => `${point.x.toFixed(1)},${point.y.toFixed(1)}`).join(" ");
  const areaPoints = `${pad.left},${bottom} ${linePoints} ${points[points.length - 1].x.toFixed(1)},${bottom}`;
  const baselineY = hasBaseline
    ? bottom - (baselineValue / yMax) * innerHeight
    : null;
  const baselineLabelY = hasBaseline ? Math.max(14, baselineY - 7) : null;
  const baselineMarkup = hasBaseline
    ? `
      <line class="trend-baseline" x1="${pad.left}" y1="${baselineY.toFixed(1)}" x2="${width - pad.right}" y2="${baselineY.toFixed(1)}" />
      <text class="trend-baseline-label" x="${width - pad.right}" y="${baselineLabelY.toFixed(1)}" text-anchor="end">League avg ${formatChartNumber(baselineValue)}</text>
    `
    : "";
  const grid = [0, 0.25, 0.5, 0.75, 1]
    .map((ratio) => {
      const y = bottom - ratio * innerHeight;
      const label = Math.round(yMax * ratio);
      return `
        <line x1="${pad.left}" y1="${y.toFixed(1)}" x2="${width - pad.right}" y2="${y.toFixed(1)}" stroke="rgba(255,255,255,0.08)" />
        <text class="trend-axis" x="12" y="${(y + 4).toFixed(1)}">${label}</text>
      `;
    })
    .join("");
  const dots = points
    .map(
      (point) => `
        <g class="trend-point" tabindex="0" aria-label="${escHtml(point.date)} ${escHtml(stat.label)} ${formatChartNumber(point.value)}">
          <circle class="trend-dot" cx="${point.x.toFixed(1)}" cy="${point.y.toFixed(1)}" r="5" />
          ${buildTrendTooltip(point, stat, hasBaseline ? baselineValue : null, width)}
        </g>
      `
    )
    .join("");
  const firstDate = values[0]?.date || "";
  const lastDate = values[values.length - 1]?.date || "";

  return `
    <svg role="img" aria-label="${escHtml(stat.label)} game trend" viewBox="0 0 ${width} ${height}">
      ${grid}
      <polygon class="trend-area" points="${areaPoints}" />
      ${baselineMarkup}
      <polyline class="trend-line" points="${linePoints}" />
      ${dots}
      <text class="trend-label" x="${pad.left}" y="${height - 16}">${escHtml(firstDate)}</text>
      <text class="trend-label" text-anchor="end" x="${width - pad.right}" y="${height - 16}">${escHtml(lastDate)}</text>
      <text class="trend-label" text-anchor="middle" x="${width / 2}" y="18">${escHtml(stat.label)}</text>
    </svg>
  `;
}

function setupPlayerTrendCharts() {
  document.querySelectorAll("[data-player-trend-chart]").forEach((chart) => {
    if (!(chart instanceof HTMLElement)) {
      return;
    }
    const dataNode = chart.querySelector("[data-trend-data]");
    const baselineNode = chart.querySelector("[data-baseline-data]");
    let games = [];
    let baselines = {};
    try {
      games = JSON.parse(dataNode?.textContent || "[]");
    } catch {
      games = [];
    }
    try {
      baselines = JSON.parse(baselineNode?.textContent || "{}");
    } catch {
      baselines = {};
    }
    const group = chart.closest("[data-tab-group]") || document;
    const controls = group.querySelector("[data-player-trend-controls]");
    const render = (statKey) => {
      chart.innerHTML = buildTrendSvg(games, statKey, baselines);
    };
    if (controls) {
      controls.querySelectorAll("[data-stat-key]").forEach((button) => {
        button.addEventListener("click", () => {
          controls.querySelectorAll("[data-stat-key]").forEach((item) => {
            item.classList.toggle("is-active", item === button);
          });
          render(button.dataset.statKey || "pts");
        });
      });
    }
    render("pts");
  });
}

export function initWorkbench() {
  setupHealthStatus();
  setupTrackButtons();
  setupCompareSearch();
  setupComparePlayerASearch();
  setupQualifiedPlayerSearch();
  setupTabs();
  setupPlayerTrendCharts();
  const storage = getStorage();
  if (!storage) {
    return;
  }
  const cap = Number(document.body.dataset.trackingCap || TRACKING_CAP);
  const payload = loadTrackedPlayers(storage, cap);
  syncTrackButtons(payload, cap);
  renderTrackedRail(payload, cap);
}

if (typeof document !== "undefined") {
  setupHealthStatus();
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initWorkbench);
  } else {
    initWorkbench();
  }
}
