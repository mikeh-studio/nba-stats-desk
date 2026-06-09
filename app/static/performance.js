(function () {
  "use strict";

  const state = {
    dates: [],
    games: [],
    players: [],
    selectedDate: "",
    selectedGameId: "",
    selectedKey: "",
    statusFilter: "all",
    nameFilter: "",
    sortKey: "score",
    sortDir: "desc",
    trendStat: "pts",
    currentDetail: null,
    selectionRequestId: 0,
    detailRequestId: 0,
    renderFrame: 0,
    detailCache: new Map(),
  };

  const $ = (selector) => document.querySelector(selector);

  function esc(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function asNumber(value) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }

  function fmt(value, digits = 1) {
    const parsed = asNumber(value);
    if (parsed === null) return "-";
    if (Number.isInteger(parsed)) return String(parsed);
    return parsed.toFixed(digits);
  }

  function fmtSigned(value) {
    const parsed = asNumber(value);
    if (parsed === null) return "-";
    const prefix = parsed > 0 ? "+" : "";
    return `${prefix}${parsed.toFixed(1)}`;
  }

  function metricByKey(item, key) {
    return (item.metrics || []).find((metric) => metric.key === key) || {};
  }

  function sortValue(player, key) {
    if (key === "score") return Math.abs(asNumber(player.performance_score) || 0);
    const metric = metricByKey(player, key);
    return asNumber(metric.value) ?? Number.NEGATIVE_INFINITY;
  }

  function sortTieValue(player, key) {
    if (key === "score") return asNumber(player.performance_score) || 0;
    return asNumber(metricByKey(player, key).delta) || 0;
  }

  function comparePlayers(a, b) {
    const direction = state.sortDir === "asc" ? 1 : -1;
    const primary = sortValue(a, state.sortKey) - sortValue(b, state.sortKey);
    if (primary !== 0) return primary * direction;
    const tie = sortTieValue(a, state.sortKey) - sortTieValue(b, state.sortKey);
    if (tie !== 0) return tie * direction;
    return String(a.player_name || "").localeCompare(String(b.player_name || ""));
  }

  function statusLabel(status) {
    if (status === "above") return "Above";
    if (status === "below") return "Below";
    if (status === "near") return "Near";
    return "N/A";
  }

  function statLabel(key) {
    const labels = { pts: "PTS", reb: "REB", ast: "AST", stl: "STL", blk: "BLK" };
    return labels[key] || String(key || "").toUpperCase();
  }

  async function fetchJson(url) {
    const response = await fetch(url, { cache: "default" });
    if (!response.ok) {
      throw new Error(`Request failed: ${response.status}`);
    }
    return response.json();
  }

  function setEmpty(message, isBusy = false) {
    $("#performance-empty").hidden = false;
    $("#performance-empty").querySelector("p").textContent = message;
    $("#performance-table-wrap").hidden = true;
    $("#performance-table-wrap").setAttribute("aria-busy", isBusy ? "true" : "false");
  }

  function setDetailEmpty(message) {
    $("#performance-detail-empty").hidden = false;
    $("#performance-detail-empty").querySelector("p").textContent = message;
    $("#performance-detail").hidden = true;
    $("#performance-detail-title").textContent = "Player View";
  }

  function setTableBusy(isBusy) {
    $("#performance-table-wrap").setAttribute("aria-busy", isBusy ? "true" : "false");
  }

  function populateDates() {
    const select = $("#performance-date-select");
    if (state.dates.length === 0) {
      select.innerHTML = '<option value="">No recent dates</option>';
      select.disabled = true;
      return;
    }
    select.disabled = false;
    select.innerHTML = state.dates
      .map((date) => `<option value="${esc(date.value)}">${esc(date.label)}</option>`)
      .join("");
    if (
      !state.selectedDate ||
      !state.dates.some((date) => date.value === state.selectedDate)
    ) {
      state.selectedDate = state.dates[0].value;
    }
    select.value = state.selectedDate;
  }

  function populateGames() {
    const select = $("#performance-game-select");
    select.disabled = !state.selectedDate;
    const options = [
      '<option value="">All games</option>',
      ...state.games.map((game) => {
        const score =
          game.away_team_pts !== null && game.home_team_pts !== null
            ? ` ${game.away_team_pts}-${game.home_team_pts}`
            : "";
        return `<option value="${esc(game.game_id)}">${esc(game.matchup || game.teams || game.game_id)}${esc(score)}</option>`;
      }),
    ];
    select.innerHTML = options.join("");
    select.value = state.selectedGameId || "";
  }

  function filteredPlayers() {
    const query = state.nameFilter.trim().toLowerCase();
    return state.players.filter((player) => {
      if (
        state.statusFilter !== "all" &&
        player.performance_status !== state.statusFilter
      ) {
        return false;
      }
      if (!query) return true;
      return String(player.player_name || "").toLowerCase().includes(query);
    }).sort(comparePlayers);
  }

  function metricCell(metric) {
    const delta = asNumber(metric.delta);
    const deltaClass = delta > 0 ? "good" : delta < 0 ? "bad" : "";
    return `
      <td>
        <span class="metric-value">${esc(fmt(metric.value))}</span>
        <span class="performance-delta ${deltaClass}">${esc(fmtSigned(metric.delta))}</span>
      </td>
    `;
  }

  function playerAvatar(player) {
    if (player.headshot_url) {
      return `
        <div class="player-avatar" aria-hidden="true">
          <img src="${esc(player.headshot_url)}" alt="" loading="lazy" onerror="this.hidden=true; this.nextElementSibling.hidden=false;" />
          <span class="player-avatar-fallback">${esc(player.player_initials || "NBA")}</span>
        </div>
      `;
    }
    return `
      <div class="player-avatar" aria-hidden="true">
        <span class="player-avatar-fallback">${esc(player.player_initials || "NBA")}</span>
      </div>
    `;
  }

  function renderSortIndicators() {
    document.querySelectorAll("[data-sort-indicator]").forEach((indicator) => {
      const key = indicator.dataset.sortIndicator;
      indicator.textContent = key === state.sortKey ? (state.sortDir === "asc" ? "^" : "v") : "";
    });
  }

  function renderPlayers() {
    renderSortIndicators();
    const players = filteredPlayers();
    const meta = $("#performance-list-meta");
    const above = state.players.filter((item) => item.performance_status === "above").length;
    const below = state.players.filter((item) => item.performance_status === "below").length;
    meta.textContent = state.selectedDate
      ? `${state.players.length} players loaded - ${above} above - ${below} below`
      : "Select a date to load players.";

    if (players.length === 0) {
      setEmpty(state.players.length === 0 ? "No player rows found for this selection." : "No players match the current filters.");
      return;
    }

    $("#performance-empty").hidden = true;
    $("#performance-table-wrap").hidden = false;
    const tbody = $("#performance-player-rows");
    tbody.innerHTML = players
      .map((player) => {
        const key = `${player.player_id}:${player.game_id}`;
        const status = player.performance_status || "near";
        return `
          <tr class="${state.selectedKey === key ? "is-focus" : ""}">
            <td>
              <button class="performance-player-button" type="button" data-player-id="${esc(player.player_id)}" data-game-id="${esc(player.game_id)}">
                ${playerAvatar(player)}
                <span class="performance-player-text">
                  <span class="performance-player-name">${esc(player.player_name)}</span>
                  <span class="meta">${esc(player.team_abbr || "NBA")} - ${esc(player.minutes)} MIN</span>
                </span>
              </button>
            </td>
            <td>
              ${esc(player.matchup || "")}
              <span class="performance-delta">${esc(player.game_date || "")}</span>
            </td>
            <td>
              <span class="performance-status ${esc(status)}">${esc(statusLabel(status))}</span>
              <span class="performance-delta">${esc(fmtSigned(player.performance_score))}</span>
            </td>
            ${metricCell(metricByKey(player, "pts"))}
            ${metricCell(metricByKey(player, "reb"))}
            ${metricCell(metricByKey(player, "ast"))}
            ${metricCell(metricByKey(player, "stl"))}
            ${metricCell(metricByKey(player, "blk"))}
          </tr>
        `;
      })
      .join("");
  }

  function scheduleRenderPlayers() {
    if (state.renderFrame) {
      cancelAnimationFrame(state.renderFrame);
    }
    state.renderFrame = requestAnimationFrame(() => {
      state.renderFrame = 0;
      renderPlayers();
    });
  }

  function clamp(value, min, max) {
    return Math.max(min, Math.min(max, value));
  }

  function positionFor(value, low, high) {
    const parsed = asNumber(value);
    if (parsed === null || high <= low) return 0;
    return clamp(((parsed - low) / (high - low)) * 100, 0, 100);
  }

  function renderRange(metric) {
    const range = metric.range || {};
    const values = [
      range.p10,
      range.p25,
      range.median,
      range.p75,
      range.p90,
      metric.value,
      metric.season_average,
    ]
      .map(asNumber)
      .filter((value) => value !== null);
    let low = asNumber(range.p10);
    let high = asNumber(range.p90);
    if (low === null && values.length) low = Math.min(...values);
    if (high === null && values.length) high = Math.max(...values);
    if (low === null || high === null) {
      low = 0;
      high = 1;
    }
    if (low === high) {
      low = Math.max(0, low - 1);
      high += 1;
    }

    const typicalLeft = positionFor(range.p25, low, high);
    const typicalRight = positionFor(range.p75, low, high);
    const marker = positionFor(metric.value, low, high);
    const average = positionFor(metric.season_average, low, high);
    const percentile = asNumber(metric.percentile);

    return `
      <div class="performance-range-row">
        <div class="performance-range-top">
          <strong>${esc(metric.label)}</strong>
          <span class="metric-value">${esc(fmt(metric.value))}</span>
        </div>
        <div class="performance-range-track" aria-label="${esc(metric.label)} percentile range">
          <span class="performance-range-typical" style="left:${typicalLeft.toFixed(1)}%;width:${Math.max(2, typicalRight - typicalLeft).toFixed(1)}%;"></span>
          <span class="performance-range-average" style="left:${average.toFixed(1)}%;"></span>
          <span class="performance-range-marker" style="left:${marker.toFixed(1)}%;"></span>
        </div>
        <div class="performance-range-labels">
          <span class="meta">Typical ${esc(fmt(range.p25))} - ${esc(fmt(range.p75))}</span>
          <span class="meta">${percentile === null ? "N/A" : `${percentile.toFixed(1)}th`} percentile - Avg ${esc(fmt(metric.season_average))}</span>
        </div>
      </div>
    `;
  }

  function trendBaseline(item, key) {
    const trend = item.trend_30d || {};
    const stat = (trend.stats || []).find((entry) => entry.key === key);
    return asNumber(stat?.season_average) ?? asNumber(metricByKey(item, key).season_average);
  }

  function renderTrendControls() {
    $("#performance-trend-tabs").querySelectorAll("[data-trend-stat]").forEach((button) => {
      const isActive = button.dataset.trendStat === state.trendStat;
      button.classList.toggle("is-active", isActive);
    });
  }

  function renderTrendChart(item) {
    renderTrendControls();
    const chartEl = $("#performance-trend-chart");
    const trend = item.trend_30d || {};
    const points = (trend.points || [])
      .map((point) => ({
        ...point,
        value: asNumber(point[state.trendStat]),
      }))
      .filter((point) => point.value !== null);
    const baseline = trendBaseline(item, state.trendStat);
    const label = statLabel(state.trendStat);
    chartEl.dataset.pointCount = String(points.length);
    chartEl.dataset.stat = state.trendStat;
    chartEl.dataset.playerId = String(item.player_id || "");
    chartEl.dataset.gameId = String(item.game_id || "");

    if (points.length === 0) {
      $("#performance-trend-meta").textContent = `No ${label} trend games found in the last 30 days.`;
      chartEl.innerHTML = '<div class="empty-state"><p>No 30-day trend rows for this player.</p></div>';
      return;
    }
    $("#performance-trend-meta").textContent = `${label} trend across ${points.length} ${points.length === 1 ? "game" : "games"} with season average baseline.`;

    const width = 560;
    const height = 220;
    const pad = { top: 20, right: 20, bottom: 34, left: 38 };
    const innerWidth = width - pad.left - pad.right;
    const innerHeight = height - pad.top - pad.bottom;
    const values = points.map((point) => point.value);
    if (baseline !== null) values.push(baseline);
    let minValue = Math.min(...values, 0);
    let maxValue = Math.max(...values, 1);
    if (minValue === maxValue) {
      minValue = Math.max(0, minValue - 1);
      maxValue += 1;
    }
    const range = maxValue - minValue;

    function xAt(index) {
      if (points.length === 1) return pad.left + innerWidth / 2;
      return pad.left + (index / (points.length - 1)) * innerWidth;
    }

    function yAt(value) {
      return pad.top + innerHeight - ((value - minValue) / range) * innerHeight;
    }

    const linePoints = points
      .map((point, index) => `${xAt(index).toFixed(1)},${yAt(point.value).toFixed(1)}`)
      .join(" ");
    const baselineY = baseline === null ? null : yAt(baseline);
    const dots = points
      .map((point, index) => {
        const x = xAt(index).toFixed(1);
        const y = yAt(point.value).toFixed(1);
        return `
          <circle class="performance-trend-dot" cx="${x}" cy="${y}" r="4">
            <title>${esc(point.game_date)} ${esc(point.matchup || "")} - ${esc(label)} ${esc(fmt(point.value))}</title>
          </circle>
        `;
      })
      .join("");
    const firstLabel = points[0]?.game_date || "";
    const lastLabel = points[points.length - 1]?.game_date || "";
    const pointItems = points
      .map((point) => `
        <li class="performance-trend-point-item" data-trend-date="${esc(point.game_date || "")}" data-trend-value="${esc(fmt(point.value))}">
          <span>${esc(point.game_date || "")}</span>
          <strong>${esc(fmt(point.value))}</strong>
        </li>
      `)
      .join("");
    chartEl.innerHTML = `
      <svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${esc(label)} last 30 days trend">
        <line x1="${pad.left}" x2="${width - pad.right}" y1="${pad.top + innerHeight}" y2="${pad.top + innerHeight}" stroke="rgba(255,255,255,0.16)" />
        <line x1="${pad.left}" x2="${pad.left}" y1="${pad.top}" y2="${pad.top + innerHeight}" stroke="rgba(255,255,255,0.16)" />
        ${baselineY === null ? "" : `<line class="performance-trend-baseline" x1="${pad.left}" x2="${width - pad.right}" y1="${baselineY.toFixed(1)}" y2="${baselineY.toFixed(1)}" />`}
        <polyline class="performance-trend-line" points="${linePoints}" />
        ${dots}
        <text class="performance-trend-axis" x="${pad.left}" y="${height - 14}">${esc(firstLabel)}</text>
        <text class="performance-trend-axis" text-anchor="end" x="${width - pad.right}" y="${height - 14}">${esc(lastLabel)}</text>
        <text class="performance-trend-label" x="${pad.left}" y="15">${esc(label)} ${esc(fmt(maxValue))}</text>
        ${baselineY === null ? "" : `<text class="performance-trend-label" text-anchor="end" x="${width - pad.right}" y="${Math.max(14, baselineY - 6).toFixed(1)}">Avg ${esc(fmt(baseline))}</text>`}
      </svg>
      <div class="performance-trend-data">
        <div class="performance-trend-data-title">${esc(label)} by game</div>
        <ul>${pointItems}</ul>
      </div>
    `;
  }

  function renderDetail(item) {
    const key = `${item.player_id}:${item.game_id}`;
    state.selectedKey = key;
    state.currentDetail = item;
    renderPlayers();

    $("#performance-detail-empty").hidden = true;
    $("#performance-detail").hidden = false;
    $("#performance-detail-title").textContent = "Player View";
    $("#performance-detail-meta").textContent = `${statusLabel(item.performance_status)} - ${fmtSigned(item.performance_score)} score - ${item.games_sampled || 0} season games`;
    $("#performance-detail-name").textContent = item.player_name || "Unknown player";
    $("#performance-detail-game").textContent = `${item.team_abbr || "NBA"} - ${item.matchup || ""} - ${item.game_date || ""}`;

    const avatar = $("#performance-detail-avatar");
    avatar.innerHTML = item.headshot_url
      ? `<img src="${esc(item.headshot_url)}" alt="" loading="lazy" onerror="this.hidden=true; this.nextElementSibling.hidden=false;" /><span class="player-avatar-fallback">${esc(item.player_initials || "NBA")}</span>`
      : `<span class="player-avatar-fallback">${esc(item.player_initials || "NBA")}</span>`;

    $("#performance-detail-summary").innerHTML = (item.metrics || [])
      .map((metric) => {
        const delta = asNumber(metric.delta);
        const deltaClass = delta > 0 ? "good" : delta < 0 ? "bad" : "";
        return `
          <div class="metric">
            <span class="metric-label">${esc(metric.label)}</span>
            <span class="metric-value">${esc(fmt(metric.value))}</span>
            <span class="meta ${deltaClass}">${esc(fmtSigned(metric.delta))} vs avg ${esc(fmt(metric.season_average))}</span>
          </div>
        `;
      })
      .join("");

    $("#performance-range-list").innerHTML = (item.metrics || [])
      .map(renderRange)
      .join("");

    renderTrendChart(item);
  }

  function renderDetailPreview(item) {
    renderDetail(item);
    $("#performance-detail-meta").textContent = `${statusLabel(item.performance_status)} - ${fmtSigned(item.performance_score)} score - ${item.games_sampled || 0} season games - loading detail`;
    $("#performance-trend-meta").textContent = "Loading 30-day trend and percentile ranges.";
    $("#performance-trend-chart").innerHTML = '<div class="empty-state"><p>Loading trend rows...</p></div>';
  }

  function showFirstPlayerDetail() {
    if (state.players.length === 0) return;
    const first = state.players[0];
    renderDetailPreview(first);
    loadPlayerDetail(first.player_id, first.game_id, { keepPreview: true });
  }

  async function loadPlayerDetail(playerId, gameId, options = {}) {
    if (!playerId || !gameId) return;
    const key = `${playerId}:${gameId}`;
    const cached = state.detailCache.get(key);
    if (cached) {
      renderDetail(cached);
      return;
    }
    const requestId = ++state.detailRequestId;
    if (!options.keepPreview) {
      setDetailEmpty("Loading percentile ranges...");
    }
    try {
      const data = await fetchJson(
        `/api/performance/players/${encodeURIComponent(playerId)}?game_id=${encodeURIComponent(gameId)}`
      );
      if (requestId !== state.detailRequestId) return;
      state.detailCache.set(key, data.item);
      renderDetail(data.item);
    } catch {
      if (requestId !== state.detailRequestId) return;
      if (options.keepPreview) {
        $("#performance-trend-meta").textContent = "Detailed percentile ranges could not load.";
        $("#performance-trend-chart").innerHTML = '<div class="empty-state"><p>Trend rows are unavailable.</p></div>';
      } else {
        setDetailEmpty("Could not load this player performance row.");
      }
    }
  }

  async function loadPlayers(requestId = state.selectionRequestId) {
    if (!state.selectedDate) {
      state.players = [];
      renderPlayers();
      return;
    }
    $("#performance-player-filter").disabled = true;
    setEmpty("Loading player rows...", true);
    state.currentDetail = null;
    state.detailRequestId += 1;
    setDetailEmpty("Pick a row from the player list.");
    const params = new URLSearchParams({ game_date: state.selectedDate });
    if (state.selectedGameId) params.set("game_id", state.selectedGameId);
    try {
      const data = await fetchJson(`/api/performance/players?${params.toString()}`);
      if (requestId !== state.selectionRequestId) return;
      state.players = data.items || [];
      $("#performance-player-filter").disabled = false;
      state.selectedKey = "";
      setTableBusy(false);
      renderPlayers();
      if (state.players.length > 0) {
        showFirstPlayerDetail();
      }
    } catch {
      if (requestId !== state.selectionRequestId) return;
      state.players = [];
      setTableBusy(false);
      setEmpty("Could not load player rows.");
    }
  }

  async function loadGames(requestId = state.selectionRequestId) {
    state.games = [];
    populateGames();
    if (!state.selectedDate) return;
    try {
      const data = await fetchJson(
        `/api/performance/games?game_date=${encodeURIComponent(state.selectedDate)}`
      );
      if (requestId !== state.selectionRequestId) return;
      state.games = data.items || [];
      populateGames();
    } catch {
      if (requestId !== state.selectionRequestId) return;
      state.games = [];
      populateGames();
    }
  }

  async function loadDate(dateValue) {
    await loadInitial(dateValue);
  }

  async function loadInitial(dateValue = "") {
    const requestId = ++state.selectionRequestId;
    state.selectedDate = dateValue;
    state.selectedGameId = "";
    $("#performance-player-filter").disabled = true;
    setEmpty("Loading player rows...", true);
    setDetailEmpty("Pick a row from the player list.");
    const params = new URLSearchParams();
    if (dateValue) params.set("game_date", dateValue);
    try {
      const suffix = params.toString() ? `?${params.toString()}` : "";
      const data = await fetchJson(`/api/performance/initial${suffix}`);
      if (requestId !== state.selectionRequestId) return;
      state.dates = data.dates || [];
      state.selectedDate = data.selected_date || "";
      state.selectedGameId = data.selected_game_id || "";
      state.games = data.games || [];
      state.players = data.players || [];
      populateDates();
      populateGames();
      $("#performance-player-filter").disabled = state.players.length === 0;
      setTableBusy(false);
      if (state.selectedDate) {
        renderPlayers();
        showFirstPlayerDetail();
      } else {
        setEmpty("No recent game dates are available.");
      }
    } catch {
      if (requestId !== state.selectionRequestId) return;
      $("#performance-date-select").innerHTML = '<option value="">Unavailable</option>';
      state.games = [];
      state.players = [];
      setTableBusy(false);
      setEmpty("Could not load recent game dates.");
    }
  }

  function initEvents() {
    $("#performance-date-select").addEventListener("change", (event) => {
      loadDate(event.target.value);
    });

    $("#performance-game-select").addEventListener("change", (event) => {
      state.selectedGameId = event.target.value;
      loadPlayers(++state.selectionRequestId);
    });

    $("#performance-player-filter").addEventListener("input", (event) => {
      state.nameFilter = event.target.value;
      scheduleRenderPlayers();
    });

    $("#performance-status-tabs").querySelectorAll("[data-status]").forEach((button) => {
      button.addEventListener("click", () => {
        $("#performance-status-tabs")
          .querySelectorAll("[data-status]")
          .forEach((item) => item.classList.remove("is-active"));
        button.classList.add("is-active");
        state.statusFilter = button.dataset.status || "all";
        scheduleRenderPlayers();
      });
    });

    document.querySelectorAll("[data-sort-key]").forEach((button) => {
      button.addEventListener("click", () => {
        const nextKey = button.dataset.sortKey || "score";
        if (state.sortKey === nextKey) {
          state.sortDir = state.sortDir === "desc" ? "asc" : "desc";
        } else {
          state.sortKey = nextKey;
          state.sortDir = "desc";
        }
        scheduleRenderPlayers();
      });
    });

    $("#performance-player-rows").addEventListener("click", (event) => {
      const target = event.target;
      if (!(target instanceof Element)) return;
      const button = target.closest(".performance-player-button");
      if (!button) return;
      loadPlayerDetail(button.dataset.playerId, button.dataset.gameId);
    });

    $("#performance-trend-tabs").querySelectorAll("[data-trend-stat]").forEach((button) => {
      button.addEventListener("click", () => {
        state.trendStat = button.dataset.trendStat || "pts";
        if (state.currentDetail) renderTrendChart(state.currentDetail);
      });
    });
  }

  async function init() {
    initEvents();
    await loadInitial();
  }

  document.addEventListener("DOMContentLoaded", init);
})();
