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
    selectionRequestId: 0,
    renderFrame: 0,
    modalOpen: false,
    lastFocusedElement: null,
  };

  const $ = (selector) => document.querySelector(selector);
  const TABLE_METRICS = ["min", "pts", "reb", "ast", "fg3m", "fg_pct", "ft_pct", "stl", "blk"];
  const MODAL_METRICS = ["pts", "reb", "ast", "fg3m", "fg_pct", "ft_pct"];

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

  function isPercentMetric(metricOrKey) {
    if (typeof metricOrKey === "string") {
      return metricOrKey === "fg_pct" || metricOrKey === "ft_pct";
    }
    return metricOrKey?.format === "percent" || isPercentMetric(metricOrKey?.key);
  }

  function fmtMetric(metricOrKey, value) {
    const parsed = asNumber(value);
    if (parsed === null) return "-";
    if (isPercentMetric(metricOrKey)) return `${parsed.toFixed(1)}%`;
    return fmt(parsed);
  }

  function fmtSignedMetric(metricOrKey, value) {
    const parsed = asNumber(value);
    if (parsed === null) return "-";
    const prefix = parsed > 0 ? "+" : "";
    const suffix = isPercentMetric(metricOrKey) ? "pp" : "";
    return `${prefix}${parsed.toFixed(1)}${suffix}`;
  }

  function metricByKey(item, key) {
    return (item.metrics || []).find((metric) => metric.key === key) || {};
  }

  function sortValue(player, key) {
    if (key === "score") {
      return asNumber(player.performance_score) ?? Number.NEGATIVE_INFINITY;
    }
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
    const labels = { min: "MIN", pts: "PTS", reb: "REB", ast: "AST", stl: "STL", blk: "BLK", fg3m: "3PM", fg_pct: "FG%", ft_pct: "FT%" };
    return labels[key] || String(key || "").toUpperCase();
  }

  async function fetchJson(url) {
    const response = await fetch(url, { cache: "no-store" });
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

  function setTableBusy(isBusy) {
    $("#performance-table-wrap").setAttribute("aria-busy", isBusy ? "true" : "false");
  }

  function findPlayerButtonByKey(key) {
    return (
      Array.from(document.querySelectorAll(".performance-player-button")).find(
        (button) => `${button.dataset.playerId}:${button.dataset.gameId}` === key
      ) || null
    );
  }

  function clearSelectedPlayer({ restoreFocus = false, render = true } = {}) {
    const focusKey = state.selectedKey;
    const fallbackFocus = state.lastFocusedElement;
    state.selectedKey = "";
    closePlayerModal({ restoreFocus: false });
    if (render) renderPlayers();
    if (restoreFocus) {
      const focusTarget = focusKey ? findPlayerButtonByKey(focusKey) : fallbackFocus;
      if (focusTarget instanceof HTMLElement) {
        focusTarget.focus();
      }
    }
    state.lastFocusedElement = null;
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
      <td data-metric-key="${esc(metric.key || "")}">
        <span class="metric-value">${esc(fmtMetric(metric, metric.value))}</span>
        <span class="performance-delta ${deltaClass}">${esc(fmtSignedMetric(metric, metric.delta))}</span>
      </td>
    `;
  }

  function playerAvatar(player) {
    if (player.headshot_url) {
      return `
        <div class="player-avatar" aria-hidden="true">
          <img src="${esc(player.headshot_url)}" alt="" loading="lazy" onerror="this.hidden=true; this.nextElementSibling.hidden=false;" />
          <span class="player-avatar-fallback" hidden>${esc(player.player_initials || "NBA")}</span>
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
      indicator.textContent =
        key === state.sortKey ? (state.sortDir === "asc" ? "^" : "v") : "";
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
              <button class="performance-player-button" type="button" data-player-id="${esc(player.player_id)}" data-game-id="${esc(player.game_id)}" aria-pressed="${state.selectedKey === key ? "true" : "false"}">
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
            ${TABLE_METRICS.map((metricKey) => metricCell(metricByKey(player, metricKey))).join("")}
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

  function playerKey(player) {
    return `${player.player_id}:${player.game_id}`;
  }

  function renderModalMetric(label, value, meta = "") {
    return `
      <div class="metric">
        <span class="metric-label">${esc(label)}</span>
        <span class="metric-value">${esc(value)}</span>
        ${meta ? `<span class="meta">${esc(meta)}</span>` : ""}
      </div>
    `;
  }

  function closePlayerModal({ restoreFocus = true } = {}) {
    const modal = $("[data-performance-modal]");
    if (!modal || modal.hidden) return;
    modal.hidden = true;
    state.modalOpen = false;
    if (
      restoreFocus &&
      state.lastFocusedElement instanceof HTMLElement &&
      document.contains(state.lastFocusedElement)
    ) {
      state.lastFocusedElement.focus();
    }
    state.lastFocusedElement = null;
  }

  function openPlayerModal(player) {
    const modal = $("[data-performance-modal]");
    const panel = $(".performance-modal-panel");
    if (!modal || !panel) return;
    state.selectedKey = playerKey(player);
    renderPlayers();

    const status = player.performance_status || "near";
    const score = fmtSigned(player.performance_score);
    const minutes = asNumber(player.minutes) === null ? "Minutes N/A" : `${fmt(player.minutes)} MIN`;
    const avatar = $("#performance-modal-avatar");
    avatar.innerHTML = player.headshot_url
      ? `<img src="${esc(player.headshot_url)}" alt="" loading="lazy" onerror="this.hidden=true; this.nextElementSibling.hidden=false;" /><span class="player-avatar-fallback" hidden>${esc(player.player_initials || "NBA")}</span>`
      : `<span class="player-avatar-fallback">${esc(player.player_initials || "NBA")}</span>`;

    $("#performance-modal-title").textContent = player.player_name || "Unknown player";
    $("#performance-modal-meta").textContent = `${player.team_abbr || "NBA"} - ${player.matchup || "Game"} - ${player.game_date || ""}`;
    const statusEl = $("#performance-modal-status");
    statusEl.className = `performance-status ${status}`;
    statusEl.textContent = statusLabel(status);

    const summary = [
      renderModalMetric("P-Rating", score, `${player.above_count ?? 0} above - ${player.below_count ?? 0} below`),
      renderModalMetric("Minutes", minutes, "Selected game"),
      ...MODAL_METRICS.map((metricKey) => {
        const metric = metricByKey(player, metricKey);
        return renderModalMetric(
          metric.label || statLabel(metricKey),
          fmtMetric(metric, metric.value),
          `${fmtSignedMetric(metric, metric.delta)} vs avg ${fmtMetric(metric, metric.season_average)}`
        );
      }),
    ];
    $("#performance-modal-summary").innerHTML = summary.join("");

    const detailLink = $("#performance-modal-detail-link");
    const playerId = Number(player.player_id);
    detailLink.href = Number.isFinite(playerId) && playerId > 0 ? `/players/${playerId}` : "/performance";
    detailLink.setAttribute("aria-label", `View details for ${player.player_name || "selected player"}`);

    state.lastFocusedElement = findPlayerButtonByKey(state.selectedKey);
    state.modalOpen = true;
    modal.hidden = false;
    panel.focus();
  }

  function openPlayerModalByKey(playerId, gameId) {
    const key = `${playerId}:${gameId}`;
    const player = state.players.find((item) => playerKey(item) === key);
    if (!player) return;
    openPlayerModal(player);
  }

  async function loadPlayers(requestId = state.selectionRequestId) {
    if (!state.selectedDate) {
      state.players = [];
      clearSelectedPlayer({ restoreFocus: false, render: false });
      renderPlayers();
      return;
    }
    $("#performance-player-filter").disabled = true;
    setEmpty("Loading player rows...", true);
    clearSelectedPlayer({ restoreFocus: false, render: false });
    const params = new URLSearchParams({ game_date: state.selectedDate });
    if (state.selectedGameId) params.set("game_id", state.selectedGameId);
    try {
      const data = await fetchJson(`/api/performance/players?${params.toString()}`);
      if (requestId !== state.selectionRequestId) return;
      state.players = data.items || [];
      $("#performance-player-filter").disabled = false;
      setTableBusy(false);
      renderPlayers();
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
    clearSelectedPlayer({ restoreFocus: false, render: false });
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
      openPlayerModalByKey(button.dataset.playerId, button.dataset.gameId);
    });

    document.querySelectorAll("[data-performance-modal-close]").forEach((button) => {
      button.addEventListener("click", () => clearSelectedPlayer());
    });

    document.addEventListener("keydown", (event) => {
      if (!state.modalOpen) return;
      if (event.key === "Escape") {
        clearSelectedPlayer();
        return;
      }
      if (event.key === "Tab") trapModalFocus(event);
    });
  }

  function trapModalFocus(event) {
    const panel = $(".performance-modal-panel");
    if (!panel) return;
    const focusable = Array.from(
      panel.querySelectorAll(
        'a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])'
      )
    ).filter((node) => node.offsetParent !== null);
    if (focusable.length === 0) {
      event.preventDefault();
      panel.focus();
      return;
    }
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    const active = document.activeElement;
    if (event.shiftKey) {
      if (active === first || active === panel || !panel.contains(active)) {
        event.preventDefault();
        last.focus();
      }
    } else if (active === last) {
      event.preventDefault();
      first.focus();
    }
  }

  async function init() {
    initEvents();
    await loadInitial();
  }

  if (typeof window === "undefined" || window.__NBA_PERFORMANCE_TEST_HOOKS__) {
    globalThis.__performanceTest = {
      state,
      comparePlayers,
      sortValue,
    };
  }

  if (typeof document !== "undefined") {
    document.addEventListener("DOMContentLoaded", init);
  }
})();
