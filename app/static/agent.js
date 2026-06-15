function escHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function formatNumber(value) {
  if (!Number.isFinite(value)) {
    return "";
  }
  return Number.isInteger(value) ? String(value) : value.toFixed(1);
}

function asArray(value) {
  return Array.isArray(value) ? value : [];
}

function renderTable(table) {
  const columns = asArray(table.columns);
  const rows = asArray(table.rows);
  return `
    <section class="stack">
      <h3>${escHtml(table.title || "Table")}</h3>
      <div class="table-scroll">
        <table class="data-table compact">
          <thead>
            <tr>${columns.map((column) => `<th>${escHtml(column.label || column.key)}</th>`).join("")}</tr>
          </thead>
          <tbody>
            ${rows
              .map(
                (row) =>
                  `<tr>${asArray(row)
                    .map((value) => `<td>${escHtml(value)}</td>`)
                    .join("")}</tr>`
              )
              .join("")}
          </tbody>
        </table>
      </div>
    </section>
  `;
}

function tooltipMarkup({ x, y, label, meta }, width) {
  const tooltipWidth = 160;
  const tooltipHeight = meta ? 56 : 40;
  const tx = Math.min(width - tooltipWidth - 8, Math.max(8, x - tooltipWidth / 2));
  const ty = y - tooltipHeight - 12 < 8 ? y + 14 : y - tooltipHeight - 12;
  return `
    <g class="agent-tooltip">
      <rect class="agent-tooltip-box" x="${tx.toFixed(1)}" y="${ty.toFixed(1)}" width="${tooltipWidth}" height="${tooltipHeight}" rx="8" />
      <text class="agent-tooltip-text" x="${tx + 10}" y="${ty + 18}">${escHtml(label)}</text>
      ${meta ? `<text class="agent-tooltip-text" x="${tx + 10}" y="${ty + 36}">${escHtml(meta)}</text>` : ""}
    </g>
  `;
}

function renderLineChart(chart) {
  const width = 720;
  const height = 250;
  const pad = { top: 26, right: 26, bottom: 46, left: 46 };
  const series = asArray(chart.series).filter((item) => asArray(item.points).length > 0);
  const firstSeries = series[0];
  if (!firstSeries) {
    return '<div class="empty-state"><strong>No chart data.</strong></div>';
  }
  const pointsRaw = asArray(firstSeries.points)
    .map((point, index) => ({
      index,
      xLabel: String(point.x ?? ""),
      yValue: Number(point.y),
      meta: String(point.meta ?? ""),
    }))
    .filter((point) => Number.isFinite(point.yValue));
  if (pointsRaw.length === 0) {
    return '<div class="empty-state"><strong>No chart data.</strong></div>';
  }

  const innerWidth = width - pad.left - pad.right;
  const innerHeight = height - pad.top - pad.bottom;
  const bottom = pad.top + innerHeight;
  const maxValue = Math.max(...pointsRaw.map((point) => point.yValue), 1);
  const yMax = Math.max(1, Math.ceil(maxValue * 1.15));
  const points = pointsRaw.map((point, index) => {
    const x =
      pointsRaw.length === 1
        ? pad.left + innerWidth / 2
        : pad.left + (index / (pointsRaw.length - 1)) * innerWidth;
    const y = bottom - (point.yValue / yMax) * innerHeight;
    return { ...point, x, y };
  });
  const grid = [0, 0.25, 0.5, 0.75, 1]
    .map((ratio) => {
      const y = bottom - ratio * innerHeight;
      return `
        <line x1="${pad.left}" y1="${y.toFixed(1)}" x2="${width - pad.right}" y2="${y.toFixed(1)}" stroke="rgba(255,255,255,0.08)" />
        <text class="agent-axis" x="12" y="${(y + 4).toFixed(1)}">${Math.round(yMax * ratio)}</text>
      `;
    })
    .join("");
  const line = points.map((point) => `${point.x.toFixed(1)},${point.y.toFixed(1)}`).join(" ");
  const dots = points
    .map((point) => {
      const label = `${firstSeries.label || firstSeries.key}: ${formatNumber(point.yValue)}`;
      return `
        <g class="agent-point" tabindex="0" aria-label="${escHtml(point.xLabel)} ${escHtml(label)}">
          <circle class="agent-dot" cx="${point.x.toFixed(1)}" cy="${point.y.toFixed(1)}" r="5" />
          ${tooltipMarkup({ x: point.x, y: point.y, label, meta: point.meta || point.xLabel }, width)}
        </g>
      `;
    })
    .join("");
  return `
    <svg role="img" aria-label="${escHtml(chart.title || "Line chart")}" viewBox="0 0 ${width} ${height}">
      ${grid}
      <polyline class="agent-line" points="${line}" />
      ${dots}
      <text class="agent-axis" x="${pad.left}" y="${height - 16}">${escHtml(pointsRaw[0].xLabel)}</text>
      <text class="agent-axis" text-anchor="end" x="${width - pad.right}" y="${height - 16}">${escHtml(pointsRaw[pointsRaw.length - 1].xLabel)}</text>
    </svg>
  `;
}

function renderBarChart(chart) {
  const width = 720;
  const height = 260;
  const pad = { top: 34, right: 24, bottom: 48, left: 46 };
  const series = asArray(chart.series)[0];
  const points = asArray(series?.points)
    .map((point) => ({
      xLabel: String(point.x ?? ""),
      yValue: Number(point.y),
      meta: String(point.meta ?? ""),
    }))
    .filter((point) => Number.isFinite(point.yValue));
  if (points.length === 0) {
    return '<div class="empty-state"><strong>No chart data.</strong></div>';
  }
  const innerWidth = width - pad.left - pad.right;
  const innerHeight = height - pad.top - pad.bottom;
  const bottom = pad.top + innerHeight;
  // Scale to the data (with headroom) so short bars stay readable instead of
  // being squashed against a fixed 0-100 axis.
  const maxValue = Math.max(...points.map((point) => point.yValue), 1);
  const yMax = maxValue * 1.15;
  const barGap = 12;
  const barWidth = Math.max(20, (innerWidth - barGap * (points.length - 1)) / points.length);
  const bars = points
    .map((point, index) => {
      const barHeight = point.yValue > 0 ? Math.max(2, (point.yValue / yMax) * innerHeight) : 0;
      const x = pad.left + index * (barWidth + barGap);
      const y = bottom - barHeight;
      return `
        <g class="agent-point" tabindex="0" aria-label="${escHtml(point.xLabel)} ${formatNumber(point.yValue)}">
          <rect class="agent-bar" x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${barWidth.toFixed(1)}" height="${barHeight.toFixed(1)}" rx="6" />
          <text class="agent-bar-value" text-anchor="middle" x="${(x + barWidth / 2).toFixed(1)}" y="${(y - 8).toFixed(1)}">${escHtml(formatNumber(point.yValue))}</text>
          <text class="agent-point-label" text-anchor="middle" x="${(x + barWidth / 2).toFixed(1)}" y="${height - 18}">${escHtml(point.xLabel)}</text>
          ${tooltipMarkup({ x: x + barWidth / 2, y, label: formatNumber(point.yValue), meta: point.meta }, width)}
        </g>
      `;
    })
    .join("");
  return `
    <svg role="img" aria-label="${escHtml(chart.title || "Bar chart")}" viewBox="0 0 ${width} ${height}">
      <line x1="${pad.left}" y1="${bottom}" x2="${width - pad.right}" y2="${bottom}" stroke="rgba(255,255,255,0.14)" />
      ${bars}
    </svg>
  `;
}

function renderPercentileChart(chart) {
  const width = 720;
  const height = 290;
  const pad = { top: 38, right: 26, bottom: 54, left: 54 };
  const series = asArray(chart.series)[0];
  const points = asArray(series?.points)
    .map((point) => ({
      xLabel: String(point.x ?? ""),
      yValue: Number(point.y),
      meta: String(point.meta ?? ""),
    }))
    .filter((point) => Number.isFinite(point.yValue));
  if (points.length === 0) {
    return '<div class="empty-state"><strong>No chart data.</strong></div>';
  }
  const innerWidth = width - pad.left - pad.right;
  const innerHeight = height - pad.top - pad.bottom;
  const bottom = pad.top + innerHeight;
  // Percentiles always live on a fixed 0-100 scale so bars are comparable.
  const yFor = (value) => bottom - (Math.max(0, Math.min(100, value)) / 100) * innerHeight;
  const grid = [0, 25, 50, 75, 100]
    .map((tick) => {
      const y = yFor(tick);
      return `
        <line class="agent-grid" x1="${pad.left}" y1="${y.toFixed(1)}" x2="${width - pad.right}" y2="${y.toFixed(1)}" />
        <text class="agent-axis" text-anchor="end" x="${pad.left - 8}" y="${(y + 4).toFixed(1)}">${tick}</text>
      `;
    })
    .join("");
  const barGap = 16;
  const barWidth = Math.max(30, (innerWidth - barGap * (points.length - 1)) / points.length);
  const bars = points
    .map((point, index) => {
      const x = pad.left + index * (barWidth + barGap);
      const y = yFor(point.yValue);
      const barHeight = Math.max(2, bottom - y);
      const above = point.yValue >= 50;
      const valueLabel = String(Math.round(point.yValue));
      return `
        <g class="agent-point" tabindex="0" aria-label="${escHtml(point.xLabel)} ${escHtml(valueLabel)}th percentile">
          <rect class="agent-bar ${above ? "is-above" : "is-below"}" x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${barWidth.toFixed(1)}" height="${barHeight.toFixed(1)}" rx="6" />
          <text class="agent-bar-value" text-anchor="middle" x="${(x + barWidth / 2).toFixed(1)}" y="${(y - 8).toFixed(1)}">${escHtml(valueLabel)}</text>
          <text class="agent-point-label" text-anchor="middle" x="${(x + barWidth / 2).toFixed(1)}" y="${(bottom + 22).toFixed(1)}">${escHtml(point.xLabel)}</text>
          ${tooltipMarkup({ x: x + barWidth / 2, y, label: `${valueLabel}th percentile`, meta: point.meta }, width)}
        </g>
      `;
    })
    .join("");
  const refY = yFor(50);
  const reference = `
    <line class="agent-reference" x1="${pad.left}" y1="${refY.toFixed(1)}" x2="${width - pad.right}" y2="${refY.toFixed(1)}" />
    <text class="agent-reference-label" x="${pad.left + 2}" y="${(refY - 7).toFixed(1)}">League average (50th)</text>
  `;
  return `
    <svg role="img" aria-label="${escHtml(chart.title || "Percentile chart")}" viewBox="0 0 ${width} ${height}">
      <text class="agent-axis-title" x="14" y="${(pad.top - 16).toFixed(1)}">Percentile (vs qualified players)</text>
      ${grid}
      ${bars}
      ${reference}
    </svg>
  `;
}

function isPercentileChart(chart) {
  const yLabel = String(chart.y_label ?? "").toLowerCase();
  const title = String(chart.title ?? "").toLowerCase();
  return yLabel.includes("percentile") || title.includes("percentile");
}

function renderChart(chart) {
  let chartBody;
  if (isPercentileChart(chart)) {
    chartBody = renderPercentileChart(chart);
  } else if (chart.type === "bar") {
    chartBody = renderBarChart(chart);
  } else {
    chartBody = renderLineChart(chart);
  }
  return `
    <article class="agent-chart">
      <div class="agent-chart-title">${escHtml(chart.title || "Chart")}</div>
      ${chartBody}
    </article>
  `;
}

function initialsForName(name) {
  return String(name || "NBA")
    .trim()
    .split(/\s+/)
    .slice(0, 2)
    .map((part) => part.charAt(0))
    .join("")
    .toUpperCase() || "NBA";
}

function renderPlayerProfile(profile) {
  if (!profile || typeof profile !== "object") return "";
  const player = profile.player && typeof profile.player === "object" ? profile.player : {};
  const name = String(player.player_name || "").trim();
  if (!name) return "";

  const initials = String(player.player_initials || initialsForName(name));
  const metaParts = [
    player.team_abbr,
    profile.availability_state,
    player.games_sampled ? `${player.games_sampled} games` : "",
  ].filter(Boolean);
  const trend = profile.trend && typeof profile.trend === "object" ? profile.trend : {};
  const archetype =
    profile.archetype && typeof profile.archetype === "object" ? profile.archetype : {};
  const chips = [
    player.overall_rank ? `Rank #${player.overall_rank}` : "",
    player.recommendation_score ? `Score ${formatNumber(Number(player.recommendation_score))}` : "",
    trend.status ? `Trend ${trend.status}` : "",
    archetype.archetype_label ? archetype.archetype_label : "",
    player.category_strengths ? `Strengths ${player.category_strengths}` : "",
    player.category_risks ? `Risks ${player.category_risks}` : "",
  ].filter(Boolean);
  const profileUrl = String(profile.profile_url || "");
  const safeProfileUrl = /^\/players\/\d+$/.test(profileUrl) ? profileUrl : "";
  const avatar = player.headshot_url
    ? `<img src="${escHtml(player.headshot_url)}" alt="" loading="lazy" onerror="this.hidden=true; this.nextElementSibling.hidden=false;" /><span class="player-avatar-fallback">${escHtml(initials)}</span>`
    : `<span class="player-avatar-fallback">${escHtml(initials)}</span>`;

  return `
    <section class="agent-player-profile">
      <div class="agent-profile-top">
        <div class="identity-row">
          <div class="player-avatar" aria-hidden="true">${avatar}</div>
          <div class="agent-profile-copy">
            <h3>${escHtml(name)}</h3>
            ${metaParts.length ? `<p class="meta">${metaParts.map(escHtml).join(" · ")}</p>` : ""}
          </div>
        </div>
        ${safeProfileUrl ? `<a class="button secondary agent-profile-link" href="${escHtml(safeProfileUrl)}">Open profile</a>` : ""}
      </div>
      ${chips.length ? `<div class="chip-row agent-profile-chips">${chips.map((chip) => `<span class="chip">${escHtml(chip)}</span>`).join("")}</div>` : ""}
      ${profile.availability_reason ? `<p class="meta">${escHtml(profile.availability_reason)}</p>` : ""}
    </section>
  `;
}

function renderContext(payload) {
  const assumptions = asArray(payload.assumptions);
  const definitions = asArray(payload.metric_definitions);
  const followups = asArray(payload.followups);
  const toolCalls = asArray(payload.tool_calls);
  const blocks = [];
  const playerProfile = renderPlayerProfile(payload.player_profile);
  if (playerProfile) {
    blocks.push(playerProfile);
  }
  if (assumptions.length) {
    blocks.push(`
      <section class="stack-list">
        <h3>Assumptions</h3>
        ${assumptions.map((item) => `<span class="chip">${escHtml(item)}</span>`).join("")}
      </section>
    `);
  }
  if (definitions.length) {
    blocks.push(`
      <section class="stack-list">
        <h3>Metrics</h3>
        ${definitions
          .map(
            (item) =>
              `<p><strong>${escHtml(item.label || item.key)}</strong><br /><span class="meta">${escHtml(item.definition)}</span></p>`
          )
          .join("")}
      </section>
    `);
  }
  if (toolCalls.length) {
    blocks.push(`
      <section class="stack-list">
        <h3>Tool Calls</h3>
        ${toolCalls.map((item) => `<span class="chip">${escHtml(item.name)} · ${escHtml(item.status)}</span>`).join("")}
      </section>
    `);
  }
  if (followups.length) {
    blocks.push(`
      <section class="stack-list">
        <h3>Followups</h3>
        ${followups.map((item) => `<button class="button secondary" type="button" data-agent-example="${escHtml(item)}">${escHtml(item)}</button>`).join("")}
      </section>
    `);
  }
  return blocks.join("") || '<div class="empty-state"><p>No extra context returned.</p></div>';
}

function bindExampleButtons(root = document) {
  root.querySelectorAll("[data-agent-example]").forEach((button) => {
    button.addEventListener("click", () => {
      const input = document.querySelector("[data-agent-question]");
      const form = document.querySelector("[data-agent-form]");
      if (!(input instanceof HTMLTextAreaElement)) return;
      input.value = button.dataset.agentExample || "";
      if (form instanceof HTMLFormElement) {
        form.requestSubmit();
      } else {
        input.focus();
      }
    });
  });
}

let activeConversationId = null;
let lastQuestion = "";
let currentAnswerEl = null;
let askInFlight = false;

function startTurn(label) {
  const empty = document.querySelector("[data-agent-empty]");
  const thread = document.querySelector("[data-agent-answer]");
  if (!thread || !empty) return;
  empty.hidden = true;
  thread.hidden = false;
  const turn = document.createElement("article");
  turn.className = "agent-turn";
  turn.innerHTML = `
    <div class="agent-turn-question">${escHtml(label)}</div>
    <div class="agent-turn-answer"><span class="meta">Working&hellip;</span></div>
  `;
  thread.appendChild(turn);
  currentAnswerEl = turn.querySelector(".agent-turn-answer");
  resetAuxiliaryPanels();
  turn.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function resetAuxiliaryPanels() {
  const tableCard = document.querySelector("[data-agent-table-card]");
  const tableEl = document.querySelector("[data-agent-tables]");
  const chartCard = document.querySelector("[data-agent-chart-card]");
  const chartEl = document.querySelector("[data-agent-charts]");
  const contextEl = document.querySelector("[data-agent-context]");
  if (tableCard) tableCard.hidden = true;
  if (tableEl) tableEl.innerHTML = "";
  if (chartCard) chartCard.hidden = true;
  if (chartEl) chartEl.innerHTML = "";
  if (contextEl) {
    contextEl.innerHTML = '<div class="empty-state"><p>Working&hellip;</p></div>';
  }
}

function renderClarifyOptions(payload) {
  const options = asArray(payload.clarification_options);
  if (!options.length) return "";
  return `
    <div class="agent-clarify-options">
      ${options
        .map((option) => {
          const name = String(option.player_name || "").trim();
          if (!name) return "";
          const team = option.team_abbr ? ` &middot; ${escHtml(option.team_abbr)}` : "";
          return `<button class="button secondary" type="button" data-agent-player-option data-player-id="${escHtml(option.player_id ?? "")}" data-player-name="${escHtml(name)}">${escHtml(name)}${team}</button>`;
        })
        .join("")}
    </div>
  `;
}

function bindClarifyOptions(root) {
  root.querySelectorAll("[data-agent-player-option]").forEach((button) => {
    button.addEventListener("click", () => {
      const playerId = Number(button.dataset.playerId);
      askQuestion(lastQuestion || button.dataset.playerName || "", {
        playerId: Number.isFinite(playerId) && playerId > 0 ? playerId : null,
        playerName: button.dataset.playerName || "",
      });
    });
  });
}

function renderPayload(payload) {
  const tableCard = document.querySelector("[data-agent-table-card]");
  const tableEl = document.querySelector("[data-agent-tables]");
  const chartCard = document.querySelector("[data-agent-chart-card]");
  const chartEl = document.querySelector("[data-agent-charts]");
  const contextEl = document.querySelector("[data-agent-context]");
  if (!currentAnswerEl) startTurn(lastQuestion || "Question");
  if (!currentAnswerEl || !tableCard || !tableEl || !chartCard || !chartEl || !contextEl) {
    return;
  }

  currentAnswerEl.innerHTML = `<div class="agent-answer-text">${escHtml(payload.answer || "No answer returned.")}</div>${renderClarifyOptions(payload)}`;
  bindClarifyOptions(currentAnswerEl);

  const tables = asArray(payload.tables);
  tableCard.hidden = tables.length === 0;
  tableEl.innerHTML = tables.map(renderTable).join("");

  const charts = asArray(payload.charts);
  chartCard.hidden = charts.length === 0;
  chartEl.innerHTML = charts.map(renderChart).join("");

  contextEl.innerHTML = renderContext(payload);
  bindExampleButtons(contextEl);
}

function setInterimAnswer(text) {
  if (!currentAnswerEl) return;
  currentAnswerEl.innerHTML = `<div class="agent-answer-text">${escHtml(text || "")}</div>`;
}

function applyConversation(payload) {
  if (payload?.conversation_id) {
    activeConversationId = payload.conversation_id;
  }
}

function handleStreamEvent(eventName, payload, state) {
  const statusEl = document.querySelector("[data-agent-status]");
  if (eventName === "meta") {
    applyConversation(payload);
    return;
  }
  if (eventName === "plan") {
    if (statusEl) statusEl.textContent = `Route ${payload.route || "planned"}`;
    return;
  }
  if (eventName === "tool_start") {
    if (statusEl) statusEl.textContent = `Calling ${payload.name || "tool"}`;
    return;
  }
  if (eventName === "tool_end") {
    if (statusEl) statusEl.textContent = `${payload.name || "Tool"} ${payload.status || "done"}`;
    return;
  }
  if (eventName === "answer_delta") {
    state.answerText += payload.delta || "";
    setInterimAnswer(state.answerText);
    if (statusEl) statusEl.textContent = "Writing";
    return;
  }
  if (eventName === "final") {
    applyConversation(payload.payload);
    renderPayload(payload.payload || {});
    if (statusEl) statusEl.textContent = "Answered";
    state.finished = true;
    return;
  }
  if (eventName === "error") {
    renderPayload({
      answer: payload.detail || "Ask NBA Stats is unavailable.",
      assumptions: [],
      tables: [],
      charts: [],
      metric_definitions: [],
      followups: [],
    });
    if (statusEl) statusEl.textContent = "Unavailable";
    state.finished = true;
  }
}

const PROVIDER_STORAGE_KEY = "askAgentProvider";
const MODEL_STORAGE_KEY_PREFIX = "askAgentModel:";

function readJsonScript(selector, fallback) {
  const element = document.querySelector(selector);
  if (!element) return fallback;
  try {
    return JSON.parse(element.textContent || "");
  } catch {
    return fallback;
  }
}

const MODEL_OPTIONS = readJsonScript("[data-agent-model-options]", {});
const DEFAULT_MODELS = readJsonScript("[data-agent-default-models]", {});

function selectedProvider() {
  const select = document.querySelector("[data-agent-provider]");
  const value = select instanceof HTMLSelectElement ? select.value : "";
  return value === "claude" ? "claude" : "openai";
}

function modelStorageKey(provider) {
  return `${MODEL_STORAGE_KEY_PREFIX}${provider}`;
}

function selectedModel() {
  const provider = selectedProvider();
  const select = document.querySelector("[data-agent-model]");
  const value = select instanceof HTMLSelectElement ? select.value : "";
  const options = Array.isArray(MODEL_OPTIONS[provider])
    ? MODEL_OPTIONS[provider]
    : [];
  if (options.some((option) => option.value === value)) return value;
  return DEFAULT_MODELS[provider] || (options[0] && options[0].value) || "";
}

function populateModelSelect() {
  const select = document.querySelector("[data-agent-model]");
  if (!(select instanceof HTMLSelectElement)) return;
  const provider = selectedProvider();
  const options = Array.isArray(MODEL_OPTIONS[provider])
    ? MODEL_OPTIONS[provider]
    : [];
  let stored = null;
  try {
    stored = window.localStorage.getItem(modelStorageKey(provider));
  } catch {
    stored = null;
  }
  const defaultModel =
    DEFAULT_MODELS[provider] || (options[0] && options[0].value) || "";
  const selected = options.some((option) => option.value === stored)
    ? stored
    : defaultModel;
  select.innerHTML = options
    .map(
      (option) =>
        `<option value="${escHtml(option.value)}">${escHtml(option.label || option.value)}</option>`
    )
    .join("");
  select.value = selected;
}

function initProviderSelect() {
  const select = document.querySelector("[data-agent-provider]");
  if (!(select instanceof HTMLSelectElement)) return;
  let stored = null;
  try {
    stored = window.localStorage.getItem(PROVIDER_STORAGE_KEY);
  } catch {
    stored = null;
  }
  if (stored === "openai" || stored === "claude") {
    select.value = stored;
  }
  populateModelSelect();
  select.addEventListener("change", () => {
    try {
      window.localStorage.setItem(PROVIDER_STORAGE_KEY, selectedProvider());
    } catch {
      // Private browsing can block storage; the toggle still works for the session.
    }
    populateModelSelect();
  });
  const modelSelect = document.querySelector("[data-agent-model]");
  if (modelSelect instanceof HTMLSelectElement) {
    modelSelect.addEventListener("change", () => {
      try {
        window.localStorage.setItem(
          modelStorageKey(selectedProvider()),
          selectedModel()
        );
      } catch {
        // Private browsing can block storage; the selection still works for the session.
      }
    });
  }
}

function buildAskBody(question, selection) {
  const body = {
    question,
    conversation_id: activeConversationId,
    provider: selectedProvider(),
    model: selectedModel(),
  };
  if (selection && selection.playerName) {
    body.selected_player_id = selection.playerId || null;
    body.selected_player_name = selection.playerName;
  }
  return body;
}

async function askQuestionJson(question, selection) {
  const statusEl = document.querySelector("[data-agent-status]");
  const response = await fetch("/api/agent/ask", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(buildAskBody(question, selection)),
  });
  const payload = await response.json();
  if (!response.ok) {
    renderPayload({
      answer: payload.detail || "Ask NBA Stats is unavailable.",
      assumptions: [],
      tables: [],
      charts: [],
      metric_definitions: [],
      followups: [],
    });
    if (statusEl) statusEl.textContent = "Unavailable";
    return;
  }
  applyConversation(payload);
  renderPayload(payload);
  if (statusEl) statusEl.textContent = "Answered";
}

function parseSseChunk(buffer, onEvent) {
  const parts = buffer.split("\n\n");
  const remaining = parts.pop() || "";
  parts.forEach((part) => {
    const lines = part.split("\n");
    const eventLine = lines.find((line) => line.startsWith("event:"));
    const dataLine = lines.find((line) => line.startsWith("data:"));
    if (!dataLine) return;
    const eventName = eventLine ? eventLine.slice(6).trim() : "message";
    try {
      onEvent(eventName, JSON.parse(dataLine.slice(5).trim()));
    } catch {
      // Ignore malformed SSE fragments; the final JSON fallback still protects UX.
    }
  });
  return remaining;
}

function renderAskFailure(message, statusText) {
  const statusEl = document.querySelector("[data-agent-status]");
  renderPayload({
    answer: message,
    assumptions: [],
    tables: [],
    charts: [],
    metric_definitions: [],
    followups: [],
  });
  if (statusEl) statusEl.textContent = statusText;
}

async function askQuestionStream(question, selection) {
  const response = await fetch("/api/agent/ask/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(buildAskBody(question, selection)),
  });
  if (!response.ok) {
    // The server answered (rate limit, validation, ...): surface its detail
    // instead of re-submitting via the JSON fallback, which would charge the
    // rate limit twice for the same question.
    let detail = "Ask NBA Stats is unavailable.";
    try {
      const payload = await response.json();
      if (payload && payload.detail) detail = payload.detail;
    } catch {
      // Keep the generic message when the error body is not JSON.
    }
    renderAskFailure(detail, "Unavailable");
    return;
  }
  if (!response.body) {
    throw new Error("stream unavailable");
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  const state = { answerText: "", finished: false, eventCount: 0 };
  let buffer = "";
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      buffer = parseSseChunk(buffer, (eventName, payload) => {
        state.eventCount += 1;
        handleStreamEvent(eventName, payload, state);
      });
    }
    if (!state.finished) {
      throw new Error("stream ended without final event");
    }
  } catch (error) {
    // Once any event has arrived the server is already running the agent;
    // mark the error so the caller does not re-submit the question.
    if (error instanceof Error) error.receivedEvents = state.eventCount > 0;
    throw error;
  }
}

async function askQuestion(question, selection = null) {
  if (askInFlight) return;
  askInFlight = true;
  const statusEl = document.querySelector("[data-agent-status]");
  const submit = document.querySelector("[data-agent-submit]");
  if (statusEl) statusEl.textContent = "Thinking";
  if (submit instanceof HTMLButtonElement) submit.disabled = true;
  if (selection && selection.playerName) {
    startTurn(selection.playerName);
  } else {
    lastQuestion = question;
    startTurn(question);
  }
  try {
    await askQuestionStream(question, selection);
  } catch (streamError) {
    if (streamError instanceof Error && streamError.receivedEvents) {
      renderAskFailure(
        "Ask NBA Stats lost the connection before finishing. Try again shortly.",
        "Failed"
      );
    } else {
      try {
        await askQuestionJson(question, selection);
      } catch {
        renderAskFailure("Ask NBA Stats failed to reach the API.", "Failed");
      }
    }
  } finally {
    askInFlight = false;
    if (submit instanceof HTMLButtonElement) submit.disabled = false;
  }
}

function initAgentPage() {
  bindExampleButtons();
  initProviderSelect();
  const form = document.querySelector("[data-agent-form]");
  const input = document.querySelector("[data-agent-question]");
  if (!(form instanceof HTMLFormElement) || !(input instanceof HTMLTextAreaElement)) {
    return;
  }
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const question = input.value.trim();
    if (!question) return;
    input.value = "";
    askQuestion(question);
  });
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      form.requestSubmit();
    }
  });
}

document.addEventListener("DOMContentLoaded", initAgentPage);
