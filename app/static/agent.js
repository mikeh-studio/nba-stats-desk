import { seasonFetch, selectedSeason } from "./season.js";
import {
  comparisonPresentation,
  comparisonAnswer,
  comparisonProfiles,
  comparisonTables,
  comparisonChart,
} from "./ask_comparison.js?v=20260912-comparison-v3";
import {
  normalizeTabs,
  openTab,
  closeTab,
  matchesSession,
} from "./ask_sessions.js?v=20260913-review-fixes-v1";
import {
  overviewPresentation,
  ordinal,
} from "./ask_presentation.js?v=20260912-v3";
function escHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

const HISTORY_STORAGE_KEY =
  selectedSeason() === "2025-26"
    ? "askChatHistory:v1"
    : `askChatHistory:v1:${selectedSeason()}`;
const HISTORY_CONVERSATION_CAP = 25;
const TABS_STORAGE_KEY = `askOpenTabs:v1:${selectedSeason()}`;
let tabState = normalizeTabs();
let historyVisible = false;
let historyQuery = "";
let historyOffset = 0;
let historyGeneration = 0;
let historyHasMore = false;
let viewedTurn = -1;
let viewedPayload = null;
let navigationGeneration = 0;

function storageNotice(message) {
  const el = document.querySelector("[data-storage-notice]");
  if (el) {
    el.textContent = message;
    el.hidden = !message;
  }
}
function saveTabs() {
  try {
    storageProvider()?.setItem(TABS_STORAGE_KEY, JSON.stringify(tabState));
  } catch {
    storageNotice(
      "Your tabs or drafts could not be saved. Keep this page open to retain this session.",
    );
  }
}
function rememberDraft() {
  const key = activeConversationId || "new";
  tabState.drafts[key] = {
    question: document.querySelector("[data-agent-question]")?.value || "",
    followup: document.querySelector("[data-followup-question]")?.value || "",
  };
  saveTabs();
}
function restoreDraft() {
  const draft = tabState.drafts[activeConversationId || "new"] || {};
  const query = document.querySelector("[data-agent-question]");
  const followup = document.querySelector("[data-followup-question]");
  if (query) query.value = draft.question ?? lastQuestion;
  if (followup) followup.value = draft.followup || "";
}
function renderTabs() {
  const el = document.querySelector("[data-chat-tabs]");
  if (!el) return;
  const ids = tabState.open;
  el.innerHTML =
    ids
      .map((id, index) => {
        const chat = historyState.conversations.find((c) => c.id === id);
        const title = chat?.title || "Saved chat";
        const playerName = chat?.turns.find(
          (t) => t.payload?.player_profile?.player?.player_name,
        )?.payload.player_profile.player.player_name;
        const window =
          /(?:past|last)\s+(\d+\s+(?:months?|games?|days?|weeks?))/i.exec(
            title,
          );
        const shortTitle =
          playerName && window
            ? `${playerName} · ${window[1]}`
            : title.replace(/^(?:tell me |show me |how has |how is )/i, "");
        const active = activeConversationId === id && !historyVisible;
        return `<div class="chat-tab${active ? " is-active" : ""}"><button type="button" role="tab" aria-controls="chat-analysis" aria-selected="${active}" tabindex="${active ? 0 : -1}" data-chat-id="${escHtml(id)}" title="${escHtml(title)}">${escHtml(shortTitle)}</button><button class="chat-close" type="button" aria-label="Close tab: ${escHtml(title)}" data-close-chat="${index}"><img src="/static/icons/x.svg" alt="" /></button></div>`;
      })
      .join("") +
    (!activeConversationId && !historyVisible
      ? '<div class="chat-tab is-active"><button type="button" role="tab" aria-controls="chat-analysis" aria-selected="true" tabindex="0">New question</button></div>'
      : "");
  el.querySelectorAll("[data-chat-id]").forEach((button) => {
    button.addEventListener("click", () =>
      restoreConversation(button.dataset.chatId),
    );
    button.addEventListener("keydown", (event) => {
      const buttons = [...el.querySelectorAll("[role=tab]")].filter(
        (b) => b.getClientRects().length,
      );
      const index = buttons.indexOf(button);
      let next;
      if (event.key === "ArrowRight") next = (index + 1) % buttons.length;
      if (event.key === "ArrowLeft")
        next = (index - 1 + buttons.length) % buttons.length;
      if (event.key === "Home") next = 0;
      if (event.key === "End") next = buttons.length - 1;
      if (next !== undefined) {
        event.preventDefault();
        buttons[next]?.focus();
      }
    });
  });
  el.querySelectorAll("[data-close-chat]").forEach((button) =>
    button.addEventListener("click", () =>
      closeConversationTab(ids[Number(button.dataset.closeChat)]),
    ),
  );
  const more = document.querySelector("[data-chat-more]");
  more?.setAttribute("aria-pressed", String(historyVisible));
  el.querySelectorAll("button").forEach((button) => {
    button.disabled = askInFlight;
  });
}
function closeConversationTab(id) {
  if (askInFlight) return;
  rememberDraft();
  tabState = closeTab(tabState, id);
  saveTabs();
  if (activeConversationId === id) {
    if (tabState.active) restoreConversation(tabState.active);
    else startNewChat();
  } else renderTabs();
  document.querySelector('[data-chat-tabs] [aria-selected="true"]')?.focus();
}
function showHistory(show = true) {
  if (askInFlight) return;
  if (show) rememberDraft();
  historyVisible = show;
  const history = document.querySelector("[data-chat-history]");
  const analysis = document.querySelector("[data-chat-analysis]");
  if (history) history.hidden = !show;
  if (analysis) analysis.hidden = show;
  renderTabs();
  if (show) {
    renderHistoryList();
    loadServerHistory(true);
    document.querySelector("[data-history-search]")?.focus();
  }
}

function looksLikeMarkdownTableLine(line) {
  const trimmed = String(line || "").trim();
  return trimmed.includes("|") && trimmed.split("|").length >= 3;
}

function looksLikeMarkdownTableSeparator(line) {
  return /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(
    String(line || ""),
  );
}

function stripMarkdownTables(value) {
  const lines = String(value ?? "").split("\n");
  const kept = [];
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    if (
      looksLikeMarkdownTableLine(line) &&
      looksLikeMarkdownTableSeparator(lines[index + 1] || "")
    ) {
      index += 2;
      while (index < lines.length && looksLikeMarkdownTableLine(lines[index])) {
        index += 1;
      }
      index -= 1;
      continue;
    }
    kept.push(line);
  }
  return kept.join("\n");
}

const BLOCK_LINE_START = /^(?:#{1,4}\s|[-*]\s|\d+[.)]\s|>\s?|-{3,}$|\*{3,}$)/;

// Best-effort repairs for models that cram headings/list items onto a single
// line instead of using newlines. They run per line and only on lines that are
// not already block-level Markdown, so prose the model formatted correctly
// (real headings, list items, rules) is left untouched. Because each line is
// processed in isolation, the `\s` runs here never span an existing newline.
function repairInlineMarkdownLine(line) {
  if (BLOCK_LINE_START.test(line.trim())) return line;
  return line
    .replace(/(\S)[ \t]+(#{2,4})[ \t]+(?=\S)/g, "$1\n\n$2 ")
    .replace(/(\S)[ \t]+(-{3,}|\*{3,})(?=[ \t]|$)/g, "$1\n\n$2")
    .replace(
      /[ \t]+-[ \t]+(?=(?:\*\*)?[A-Za-z0-9][^:\n]{0,42}:|\*\*)/g,
      "\n- ",
    );
}

function normalizeAnswerMarkdown(markdown) {
  let text = String(markdown ?? "")
    .replace(/\r\n?/g, "\n")
    .replace(/\u00a0/g, " ");
  text = stripMarkdownTables(text);
  text = text.split("\n").map(repairInlineMarkdownLine).join("\n");
  return text.replace(/\n{3,}/g, "\n\n").trim();
}

function renderInlineMarkdown(value) {
  const codeSegments = [];
  const withCodePlaceholders = escHtml(value).replace(
    /`([^`]+)`/g,
    (_, code) => {
      const index = codeSegments.length;
      codeSegments.push(`<code>${code}</code>`);
      return `@@CODE${index}@@`;
    },
  );
  return withCodePlaceholders
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(
      /@@CODE(\d+)@@/g,
      (match, index) => codeSegments[Number(index)] || match,
    );
}

function renderAnswerMarkdown(markdown) {
  const lines = normalizeAnswerMarkdown(markdown).split("\n");
  const blocks = [];
  let paragraph = [];
  let listType = "";
  let listItems = [];

  function flushParagraph() {
    if (!paragraph.length) return;
    blocks.push(`<p>${renderInlineMarkdown(paragraph.join(" "))}</p>`);
    paragraph = [];
  }

  function flushList() {
    if (!listType || !listItems.length) return;
    blocks.push(
      `<${listType}>${listItems.map((item) => `<li>${item}</li>`).join("")}</${listType}>`,
    );
    listType = "";
    listItems = [];
  }

  function addListItem(type, value) {
    flushParagraph();
    if (listType && listType !== type) flushList();
    listType = type;
    listItems.push(renderInlineMarkdown(value));
  }

  lines.forEach((rawLine) => {
    const line = rawLine.trim();
    if (!line) {
      flushParagraph();
      flushList();
      return;
    }

    const heading = /^(#{1,4})\s+(.+)$/.exec(line);
    if (heading) {
      flushParagraph();
      flushList();
      const level =
        heading[1].length <= 2 ? 3 : Math.min(5, heading[1].length + 1);
      blocks.push(`<h${level}>${renderInlineMarkdown(heading[2])}</h${level}>`);
      return;
    }

    if (/^(-{3,}|\*{3,})$/.test(line)) {
      flushParagraph();
      flushList();
      blocks.push("<hr />");
      return;
    }

    const unorderedItem = /^[-*]\s+(.+)$/.exec(line);
    if (unorderedItem) {
      addListItem("ul", unorderedItem[1]);
      return;
    }

    const orderedItem = /^\d+[.)]\s+(.+)$/.exec(line);
    if (orderedItem) {
      addListItem("ol", orderedItem[1]);
      return;
    }

    flushList();
    paragraph.push(line);
  });

  flushParagraph();
  flushList();
  return blocks.join("") || "<p>No answer returned.</p>";
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

function referenceTable(table, payload) {
  const row = payload.reference_evidence?.rows?.[0];
  const name = payload.reference_player?.player?.player_name;
  if (
    Number.isInteger(table.reference_row_index) ||
    !row?.eligible ||
    !name ||
    !asArray(table.columns).some((column) => column.key === "gap")
  )
    return table;
  return {
    ...table,
    reference_row_index: asArray(table.rows).length,
    rows: [
      ...asArray(table.rows),
      [
        name,
        row.display_value.toFixed(1),
        String(row.observed_games),
        String(row.valid_games),
        String(row.rank),
        row.percentile == null ? "Unavailable" : row.percentile.toFixed(1),
        "0.0",
      ],
    ],
  };
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
                (row, index) =>
                  `<tr${index === table.reference_row_index ? ' class="reference-player-row"' : ""}>${asArray(
                    row,
                  )
                    .map((value) => `<td>${escHtml(value)}</td>`)
                    .join("")}</tr>`,
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
  const tx = Math.min(
    width - tooltipWidth - 8,
    Math.max(8, x - tooltipWidth / 2),
  );
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
  const width = chart.overview ? 960 : 720;
  const height = chart.overview ? 300 : 250;
  const pad = { top: 26, right: 26, bottom: 46, left: 46 };
  const series = asArray(chart.series).filter(
    (item) => asArray(item.points).length > 0,
  );
  const firstSeries = series[0];
  if (!firstSeries) {
    return '<div class="empty-state"><strong>No chart data.</strong></div>';
  }
  const pointsRaw = asArray(firstSeries.points)
    .filter(
      (point) => point.y !== null && point.y !== "" && point.y !== undefined,
    )
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
  const maxValue = Math.max(
    ...pointsRaw.map((point) => point.yValue),
    Number.isFinite(chart.average) ? chart.average : 0,
    1,
  );
  const yMax = Math.max(1, Math.ceil(maxValue * 1.15));
  const yMin = Math.min(
    0,
    Math.floor(Math.min(...pointsRaw.map((point) => point.yValue)) * 1.15),
  );
  const points = pointsRaw.map((point, index) => {
    const x =
      pointsRaw.length === 1
        ? pad.left + innerWidth / 2
        : pad.left + (index / (pointsRaw.length - 1)) * innerWidth;
    const y = bottom - ((point.yValue - yMin) / (yMax - yMin)) * innerHeight;
    return { ...point, x, y };
  });
  const grid = [0, 0.25, 0.5, 0.75, 1]
    .map((ratio) => {
      const y = bottom - ratio * innerHeight;
      return `
        <line x1="${pad.left}" y1="${y.toFixed(1)}" x2="${width - pad.right}" y2="${y.toFixed(1)}" stroke="rgba(255,255,255,0.08)" />
        <text class="agent-axis" x="12" y="${(y + 4).toFixed(1)}">${Number((yMin + (yMax - yMin) * ratio).toFixed(2))}</text>
      `;
    })
    .join("");
  const line = points
    .map((point) => `${point.x.toFixed(1)},${point.y.toFixed(1)}`)
    .join(" ");
  const dots = points
    .map((point) => {
      const label = `${firstSeries.label || firstSeries.key}: ${formatNumber(point.yValue)}`;
      return `
        <g class="agent-point" tabindex="0" aria-label="${escHtml(point.xLabel)} ${escHtml(label)}">
          <circle class="agent-dot" cx="${point.x.toFixed(1)}" cy="${point.y.toFixed(1)}" r="5" />
          ${chart.overview ? `<text class="agent-bar-value" text-anchor="middle" x="${point.x}" y="${point.y - 12}">${point.yValue.toFixed(1)}</text>` : ""}
          ${tooltipMarkup({ x: point.x, y: point.y, label, meta: point.meta || point.xLabel }, width)}
        </g>
      `;
    })
    .join("");
  const referenceY =
    bottom - ((chart.average - yMin) / (yMax - yMin)) * innerHeight;
  const reference = Number.isFinite(chart.average)
    ? `<line class="agent-reference" x1="${pad.left}" x2="${width - pad.right}" y1="${referenceY}" y2="${referenceY}" /><text class="agent-reference-label" text-anchor="end" x="${width - pad.right}" y="16">Period average: ${chart.average.toFixed(1)}</text>`
    : "";
  const monthLabels = chart.overview
    ? points
        .map(
          (p, index) =>
            `<text class="agent-axis" text-anchor="${index === 0 ? "start" : index === points.length - 1 ? "end" : "middle"}" x="${p.x}" y="${height - 16}">${escHtml(p.xLabel)}</text>`,
        )
        .join("")
    : `<text class="agent-axis" x="${pad.left}" y="${height - 16}">${escHtml(pointsRaw[0].xLabel)}</text><text class="agent-axis" text-anchor="end" x="${width - pad.right}" y="${height - 16}">${escHtml(pointsRaw[pointsRaw.length - 1].xLabel)}</text>`;
  return `
    <svg role="img" aria-label="${escHtml(chart.title || "Line chart")}" viewBox="0 0 ${width} ${height}">
      ${grid}
      ${reference}
      <polyline class="agent-line" points="${line}" />
      ${dots}
      ${monthLabels}
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
  const barWidth = Math.max(
    20,
    (innerWidth - barGap * (points.length - 1)) / points.length,
  );
  const bars = points
    .map((point, index) => {
      const barHeight =
        point.yValue > 0 ? Math.max(2, (point.yValue / yMax) * innerHeight) : 0;
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
  const yFor = (value) =>
    bottom - (Math.max(0, Math.min(100, value)) / 100) * innerHeight;
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
  const barWidth = Math.max(
    30,
    (innerWidth - barGap * (points.length - 1)) / points.length,
  );
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
  return (
    String(name || "NBA")
      .trim()
      .split(/\s+/)
      .slice(0, 2)
      .map((part) => part.charAt(0))
      .join("")
      .toUpperCase() || "NBA"
  );
}

function renderPlayerProfile(profile) {
  if (!profile || typeof profile !== "object") return "";
  const player =
    profile.player && typeof profile.player === "object" ? profile.player : {};
  const name = String(player.player_name || "").trim();
  if (!name) return "";

  const initials = String(player.player_initials || initialsForName(name));
  const metaParts = [
    player.team_abbr,
    profile.availability_state,
    player.games_sampled ? `${player.games_sampled} appearances` : "",
    profile.scopeLabel,
  ].filter(Boolean);
  const trend =
    profile.trend && typeof profile.trend === "object" ? profile.trend : {};
  const archetype =
    profile.archetype && typeof profile.archetype === "object"
      ? profile.archetype
      : {};
  const chips = [
    player.overall_rank ? `Rank #${player.overall_rank}` : "",
    player.recommendation_score
      ? `P-Rating ${formatNumber(Number(player.recommendation_score))}`
      : "",
    trend.status ? `Trend ${trend.status}` : "",
    archetype.archetype_label ? archetype.archetype_label : "",
    player.category_strengths ? `Strengths ${player.category_strengths}` : "",
    player.category_risks ? `Risks ${player.category_risks}` : "",
  ].filter(Boolean);
  const profileUrl = String(profile.profile_url || "");
  const safeProfileUrl = /^\/players\/\d+$/.test(profileUrl) ? profileUrl : "";
  const avatar = player.headshot_url
    ? `<img src="${escHtml(player.headshot_url)}" alt="" loading="lazy" onerror="this.hidden=true; this.nextElementSibling.hidden=false;" /><span class="player-avatar-fallback" hidden>${escHtml(initials)}</span>`
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
              `<p><strong>${escHtml(item.label || item.key)}</strong><br /><span class="meta">${escHtml(item.definition)}</span></p>`,
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
  return (
    blocks.join("") ||
    '<div class="empty-state"><p>No extra context returned.</p></div>'
  );
}

function bindExampleButtons(root = document) {
  root.querySelectorAll("[data-agent-example]").forEach((button) => {
    button.addEventListener("click", () => {
      const followup = document.querySelector("[data-followup-question]");
      const input =
        activeConversationId && followup
          ? followup
          : document.querySelector("[data-agent-question]");
      if (!(input instanceof HTMLTextAreaElement)) return;
      input.value = button.dataset.agentExample || "";
      rememberDraft();
      input.focus();
    });
  });
}

let activeConversationId = null;
let lastQuestion = "";
let currentAnswerEl = null;
let askInFlight = false;
let historyState = { version: 1, conversations: [] };

function startTurn(label) {
  const empty = document.querySelector("[data-agent-empty]");
  const thread = document.querySelector("[data-agent-answer]");
  if (!thread || !empty) return null;
  empty.hidden = true;
  thread.hidden = false;
  viewedTurn = -1;
  thread.querySelectorAll("[data-agent-player-option]").forEach((button) => {
    button.disabled = true;
  });
  const turn = document.createElement("article");
  turn.className = "agent-turn";
  turn.tabIndex = -1;
  turn.innerHTML = `
    <div class="agent-turn-question">${escHtml(label)}</div>
    <div data-agent-profile></div>
    <div class="agent-turn-answer"><span class="meta" data-thinking="true" role="status">Thinking…</span></div>
    ${turnEvidenceMarkup()}
  `;
  thread.appendChild(turn);
  currentAnswerEl = turn.querySelector(".agent-turn-answer");
  // Keep the document anchored while streaming; don't jump to the composer.
  return turn;
}

function turnEvidenceMarkup() {
  return `<details class="methodology" data-methodology hidden><summary>Methodology &amp; data</summary><div data-agent-context></div></details><section class="evidence-section" data-agent-table-card hidden><h2 data-table-heading>Key metrics</h2><div data-agent-tables></div></section><section class="evidence-section" data-agent-chart-card hidden><div data-agent-charts></div></section>`;
}

function resetAuxiliaryPanels(message = "Working&hellip;") {
  viewedPayload = null;
  for (const selector of ["[data-agent-profile]", "[data-turn-navigation]"]) {
    const el = document.querySelector(selector);
    if (el) el.innerHTML = "";
  }
  for (const selector of ["[data-followup-section]", "[data-methodology]"]) {
    const el = document.querySelector(selector);
    if (el) el.hidden = true;
  }
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
    contextEl.innerHTML = `<div class="empty-state"><p>${message}</p></div>`;
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
          const team = option.team_abbr
            ? ` &middot; ${escHtml(option.team_abbr)}`
            : "";
          const label = String(option.label || name);
          return `<button class="button secondary" type="button" data-agent-player-option data-player-id="${escHtml(option.player_id ?? "")}" data-player-name="${escHtml(name)}">${escHtml(label)}${team}</button>`;
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
  if (!currentAnswerEl) startTurn(lastQuestion || "Question");
  if (!currentAnswerEl) return;

  renderAnswerPayload(payload, currentAnswerEl);
  renderAuxiliaryPayload(payload, currentAnswerEl.parentElement || document);
}

function renderAnswerPayload(payload, targetEl) {
  if (!targetEl) return;
  const comparison = comparisonPresentation(payload);
  if (comparison) {
    targetEl.innerHTML = comparisonAnswer(comparison);
    return;
  }
  const overview = overviewPresentation(payload);
  targetEl.innerHTML = overview
    ? `<div class="analysis-overview"><div class="overall-copy"><h2>Overall</h2>${overview.paragraphs.map((p) => `<p>${escHtml(p)}</p>`).join("")}</div>${overview.insights.length ? `<aside class="takeaways" aria-label="Key takeaways"><h2>Key takeaways</h2>${overview.insights.map((i) => `<section class="takeaway"><img src="/static/icons/${i.icon}.svg" alt="" /><div><h3>${escHtml(i.title)}</h3><p>${escHtml(i.text)}</p></div></section>`).join("")}</aside>` : ""}</div>`
    : `<div class="agent-answer-text agent-answer-markdown">${renderAnswerMarkdown(payload.answer || "No answer returned.")}</div>${renderClarifyOptions(payload)}`;
  bindClarifyOptions(targetEl);
}

function renderAuxiliaryPayload(
  payload,
  root = document,
  updateComposer = true,
) {
  viewedPayload = payload;
  const tableCard = root.querySelector("[data-agent-table-card]");
  const tableEl = root.querySelector("[data-agent-tables]");
  const chartCard = root.querySelector("[data-agent-chart-card]");
  const chartEl = root.querySelector("[data-agent-charts]");
  const contextEl = root.querySelector("[data-agent-context]");
  if (!tableCard || !tableEl || !chartCard || !chartEl || !contextEl) return;
  const tables = asArray(payload.tables);
  const comparison = comparisonPresentation(payload);
  const tableHeading = root.querySelector("[data-table-heading]");
  if (tableHeading)
    tableHeading.textContent = comparison ? "Player comparison" : "Key metrics";
  if (comparison) {
    tableCard.hidden = false;
    tableEl.innerHTML = comparisonTables(comparison);
    chartCard.hidden = false;
    const paint = (key = "pts") => {
      chartEl.innerHTML = comparisonChart(comparison, key);
      chartEl.querySelectorAll("[data-comparison-metric]").forEach((button) =>
        button.addEventListener("click", () => {
          paint(button.dataset.comparisonMetric);
          chartEl
            .querySelector(
              `[data-comparison-metric="${button.dataset.comparisonMetric}"]`,
            )
            ?.focus();
        }),
      );
    };
    paint(comparison.preferred_metric || "pts");
    contextEl.innerHTML = renderContext({ ...payload, followups: [] });
    const profile = root.querySelector("[data-agent-profile]");
    if (profile)
      profile.innerHTML = comparisonProfiles(comparison, renderPlayerProfile);
    const methodology = root.querySelector("[data-methodology]");
    if (methodology) methodology.hidden = false;
    if (!updateComposer) return;
    const followup = document.querySelector("[data-followup-section]");
    if (followup) followup.hidden = false;
    const heading = document.querySelector("[data-followup-heading]");
    if (heading) heading.textContent = "Ask a follow-up about this comparison";
    const scopeLabel = document.querySelector("[data-followup-context]");
    if (scopeLabel)
      scopeLabel.textContent = `${comparison.names.join(" and ")} · ${comparison.scopeLabel} · This chat`;
    const suggestions = document.querySelector("[data-followup-suggestions]");
    if (suggestions) {
      suggestions.innerHTML = asArray(payload.followups)
        .slice(0, 2)
        .map(
          (text, i) =>
            `<button class="button secondary" type="button" data-agent-example="${escHtml(text)}">${["Review scoring", "Review playmaking"][i]}</button>`,
        )
        .join("");
      bindExampleButtons(suggestions);
    }
    return;
  }
  const overview = overviewPresentation(payload);
  tableCard.hidden = tables.length === 0;
  tableEl.innerHTML = overview
    ? renderOverviewTable(overview)
    : tables
        .map((table) => payload.study_id
          ? `<details><summary>All study metrics, uncertainty, and sample support</summary>${renderTable(referenceTable(table, payload))}</details>`
          : renderTable(referenceTable(table, payload)))
        .join("");

  const charts = asArray(payload.charts);
  chartCard.hidden = charts.length === 0;
  if (overview && charts.length)
    renderOverviewCharts(payload, overview.preferredMetric, root);
  else chartEl.innerHTML = charts.map(renderChart).join("");

  contextEl.innerHTML = renderContext({
    ...payload,
    player_profile: null,
    followups: [],
  });
  const profile = root.querySelector("[data-agent-profile]");
  if (profile)
    profile.innerHTML = renderPlayerProfile(
      payload.player_profile
        ? {
            ...payload.player_profile,
            scopeLabel: overview
              ? `${overview.scope.phases.join(" + ")} · ${overview.range}`
              : "",
          }
        : null,
    );
  const methodology = root.querySelector("[data-methodology]");
  if (methodology) methodology.hidden = false;
  if (!updateComposer) return;
  const followup = document.querySelector("[data-followup-section]");
  if (followup) followup.hidden = false;
  const heading = document.querySelector("[data-followup-heading]");
  const name = payload.player_profile?.player?.player_name;
  if (heading)
    heading.textContent = name
      ? `Ask a follow-up about ${name}`
      : "Ask a follow-up";
  const scopeLabel = document.querySelector("[data-followup-context]");
  if (scopeLabel)
    scopeLabel.textContent = overview
      ? `${overview.range} · ${overview.scope.phases.join(" + ")} · Continues this chat`
      : "Continues this chat. Include the player and dates when changing the scope.";
  const suggestions = document.querySelector("[data-followup-suggestions]");
  if (suggestions) {
    const prompts =
      overview?.followups || asArray(payload.followups).slice(0, 2);
    suggestions.innerHTML = prompts
      .map(
        (text, i) =>
          `<button class="button secondary" type="button" data-agent-example="${escHtml(text)}">${escHtml(overview ? ["Review scoring in these dates", "Review regular season only"][i] : text)}</button>`,
      )
      .join("");
    bindExampleButtons(suggestions);
  }
}

function renderOverviewTable(overview) {
  const number = (n) => (Number.isFinite(n) ? n.toFixed(1) : "Unavailable");
  const sameCohort =
    new Set(overview.metrics.map((m) => m.cohort_size)).size === 1;
  const coverage = sameCohort
    ? `${overview.metrics[0].cohort_size} qualified players`
    : "Qualification varies by metric; see methodology";
  return `<p class="metric-comparison">Versus ${escHtml(overview.comparison)}</p><div class="table-scroll"><table class="overview-table"><caption class="sr-only">Per-game averages, league percentiles and change versus ${escHtml(overview.comparison)}</caption><thead><tr><th scope="col">Stat</th><th scope="col">Per game</th><th scope="col">League percentile</th><th scope="col">Change</th></tr></thead><tbody>${overview.metrics.map((m) => `<tr><th scope="row">${escHtml(m.label)}</th><td>${number(m.value)}</td><td>${Number.isFinite(m.percentile) ? `<div class="percentile-cell"><meter min="0" max="100" value="${Math.round(Math.max(0, Math.min(100, m.percentile)))}" aria-label="${escHtml(m.label)} league percentile">${ordinal(m.percentile)}</meter><span>${ordinal(m.percentile)}</span></div>` : "Unavailable"}</td><td class="${m.change > 0 ? "change-up" : m.change < 0 ? "change-down" : ""}">${Number.isFinite(m.change) ? `${m.change > 0 ? "+" : ""}${number(m.change)}` : "Unavailable"}</td></tr>`).join("")}</tbody></table></div><p class="metric-footnote">${coverage} · minimum ${overview.minGames} complete games per metric · same dates and phases.</p>`;
}

function renderOverviewCharts(payload, key, root = document) {
  const charts = asArray(payload.charts);
  const chart = charts.find((c) => c.series?.[0]?.key === key) || charts[0];
  const el = root.querySelector("[data-agent-charts]");
  if (!chart || !el) return;
  const metric = payload.semantic_evidence.metrics.find(
    (m) => m.key === chart.series[0].key,
  );
  el.innerHTML = `<div class="chart-header"><div><h2>Monthly ${escHtml(metric.label.toLowerCase())}</h2><p class="meta">${escHtml(metric.label)} per game · months with appearances</p></div><div class="chart-switcher" aria-label="Chart metric">${charts.map((c) => `<button type="button" data-chart-key="${escHtml(c.series[0].key)}" aria-pressed="${c === chart}">${escHtml(c.series[0].key.toUpperCase())}</button>`).join("")}</div></div><div class="agent-chart">${renderLineChart({ ...chart, overview: true, average: metric.value })}</div><p class="meta">Hover or focus a point for sample size. Months without appearances are omitted; lines do not imply games were played between observations.</p>`;
  el.querySelectorAll("[data-chart-key]").forEach((button) =>
    button.addEventListener("click", () => {
      renderOverviewCharts(payload, button.dataset.chartKey, root);
      root
        .querySelector(`[data-chart-key="${button.dataset.chartKey}"]`)
        ?.focus();
    }),
  );
}

function setInterimAnswer(text) {
  if (!currentAnswerEl) return;
  currentAnswerEl.innerHTML = `<div class="agent-answer-text agent-answer-markdown">${renderAnswerMarkdown(text || "")}</div>`;
}

function applyConversation(payload) {
  if (payload?.conversation_id) {
    activeConversationId = payload.conversation_id;
  }
}

function truncateText(value, maxLength = 72) {
  const text = String(value || "")
    .replace(/\s+/g, " ")
    .trim();
  if (text.length <= maxLength) return text;
  return `${text.slice(0, maxLength - 1).trim()}…`;
}

function nowIso() {
  return new Date().toISOString();
}

function historyTimestamp(value) {
  const text = String(value || "").trim();
  return text || nowIso();
}

function formatHistoryDate(value) {
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) return "Saved";
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(new Date(timestamp));
}

function storageProvider() {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

function normalizeHistoryTurn(rawTurn, fallbackIndex = 0) {
  if (!rawTurn || typeof rawTurn !== "object") return null;
  const payload =
    rawTurn.payload && typeof rawTurn.payload === "object"
      ? rawTurn.payload
      : {};
  const timestamp = historyTimestamp(rawTurn.timestamp || rawTurn.created_at);
  const requestId = String(
    rawTurn.request_id ||
      payload.request_id ||
      `${timestamp}:${fallbackIndex}:${rawTurn.question || ""}`,
  );
  return {
    request_id: requestId,
    question: String(rawTurn.question || "Question"),
    provider: String(rawTurn.provider || ""),
    model: String(rawTurn.model || ""),
    timestamp,
    created_at: timestamp,
    payload,
  };
}

function normalizeHistoryConversation(rawConversation) {
  if (!rawConversation || typeof rawConversation !== "object") return null;
  const id = String(
    rawConversation.id || rawConversation.conversation_id || "",
  ).trim();
  if (!id) return null;
  const turns = asArray(rawConversation.turns)
    .map((turn, index) => normalizeHistoryTurn(turn, index))
    .filter(Boolean);
  const newestTurn = turns[turns.length - 1] || null;
  const title = truncateText(
    rawConversation.title || turns[0]?.question || "Ask NBA Stats chat",
    80,
  );
  return {
    id,
    conversation_id: id,
    title,
    updated_at: historyTimestamp(
      rawConversation.updated_at || newestTurn?.timestamp,
    ),
    turns,
  };
}

function normalizeHistoryState(value) {
  const conversations = asArray(value?.conversations)
    .map((conversation, index) => ({
      conversation: normalizeHistoryConversation(conversation),
      index,
    }))
    .filter((item) => item.conversation)
    .sort((a, b) => {
      const delta =
        Date.parse(b.conversation.updated_at) -
        Date.parse(a.conversation.updated_at);
      return delta || b.index - a.index;
    })
    .map((item) => item.conversation);
  return { version: 1, conversations };
}

function loadHistoryState() {
  const storage = storageProvider();
  if (!storage) return { version: 1, conversations: [] };
  try {
    return normalizeHistoryState(
      JSON.parse(storage.getItem(HISTORY_STORAGE_KEY) || "{}"),
    );
  } catch {
    return { version: 1, conversations: [] };
  }
}

function saveHistoryState(nextState) {
  const storage = storageProvider();
  const normalized = normalizeHistoryState(nextState);
  historyState = normalized;
  if (!storage) {
    storageNotice(
      "Browser storage is unavailable. Saving this chat depends on the local app’s history file.",
    );
    return false;
  }

  const candidate = {
    version: 1,
    conversations: [...normalized.conversations]
      .sort(
        (a, b) =>
          Number(tabState.open.includes(b.id)) -
          Number(tabState.open.includes(a.id)),
      )
      .slice(0, HISTORY_CONVERSATION_CAP),
  };
  while (candidate.conversations.length >= 0) {
    try {
      storage.setItem(HISTORY_STORAGE_KEY, JSON.stringify(candidate));
      if (
        candidate.conversations.length <
        Math.min(normalized.conversations.length, HISTORY_CONVERSATION_CAP)
      )
        storageNotice(
          "Browser storage is nearly full. Some chats are only available from the local app’s history file; keep this page open if that file is unavailable.",
        );
      return true;
    } catch {
      if (candidate.conversations.length === 0) {
        storageNotice(
          "Browser storage is unavailable. This chat remains on screen; saving depends on the local app’s history file.",
        );
        return false;
      }
      candidate.conversations.pop();
    }
  }
  return false;
}

function removeStoredHistory() {
  const storage = storageProvider();
  if (!storage) return;
  try {
    storage.removeItem(HISTORY_STORAGE_KEY);
  } catch {
    // History is optional; blocked storage should not affect Ask.
  }
}

function mergeHistoryConversations(conversations) {
  const merged = new Map();
  historyState.conversations.forEach((conversation) => {
    merged.set(conversation.id, {
      ...conversation,
      turns: [...conversation.turns],
    });
  });

  asArray(conversations).forEach((rawConversation) => {
    const incoming = normalizeHistoryConversation(rawConversation);
    if (!incoming) return;
    const existing = merged.get(incoming.id) || {
      ...incoming,
      turns: [],
    };
    const turnsById = new Map();
    [...existing.turns, ...incoming.turns].forEach((turn) => {
      turnsById.set(turn.request_id, turn);
    });
    const turns = [...turnsById.values()].sort(
      (a, b) => Date.parse(a.timestamp) - Date.parse(b.timestamp),
    );
    const newestTurn = turns[turns.length - 1] || null;
    merged.set(incoming.id, {
      ...existing,
      ...incoming,
      title: existing.title || incoming.title,
      updated_at: newestTurn?.timestamp || incoming.updated_at,
      turns,
    });
  });

  return normalizeHistoryState({ conversations: [...merged.values()] });
}

function renderHistoryList() {
  const list = document.querySelector("[data-agent-history-list]");
  if (!list) return;
  const conversations = historyState.conversations.filter((c) =>
    matchesSession(c, historyQuery),
  );
  if (!conversations.length) {
    list.innerHTML = `
      <div class="empty-state">
        <p>${historyQuery ? "No matching chats found." : "Saved chats will appear here after your first answer."}</p>
      </div>
    `;
    return;
  }
  list.innerHTML = conversations
    .map((conversation) => {
      const turnCount = conversation.turns.length;
      const active =
        conversation.id === activeConversationId ? " is-active" : "";
      return `
        <button class="agent-history-item${active}" type="button" data-history-conversation-id="${escHtml(conversation.id)}">
          <span class="agent-history-title">${escHtml(conversation.title)}</span>
          <span class="agent-history-meta">${escHtml(formatHistoryDate(conversation.updated_at))} · ${Math.max(0, turnCount - 1)} ${turnCount === 2 ? "follow-up" : "follow-ups"} · Open analysis →</span>
        </button>
      `;
    })
    .join("");
  list.querySelectorAll("[data-history-conversation-id]").forEach((button) => {
    button.addEventListener("click", () => {
      restoreConversation(button.dataset.historyConversationId || "");
    });
  });
}

function persistHistoryTurn(question, payload) {
  const payloadObject = payload && typeof payload === "object" ? payload : {};
  const conversationId = String(
    payloadObject.conversation_id ||
      activeConversationId ||
      `local-${Date.now().toString(36)}`,
  );
  activeConversationId = conversationId;
  let provider = "";
  let model = "";
  try {
    provider = selectedProvider();
    model = selectedModel();
  } catch {
    provider = "";
    model = "";
  }
  const turn = normalizeHistoryTurn({
    request_id: payloadObject.request_id,
    question,
    provider,
    model,
    timestamp: nowIso(),
    payload: payloadObject,
  });
  if (!turn) return;
  const title = truncateText(
    question || payloadObject.answer || "Ask NBA Stats chat",
    80,
  );
  const nextState = mergeHistoryConversations([
    {
      id: conversationId,
      title,
      updated_at: turn.timestamp,
      turns: [turn],
    },
  ]);
  saveHistoryState(nextState);
  tabState = openTab(tabState, conversationId);
  saveTabs();
  renderTabs();
  renderTurnNavigation();
  renderHistoryList();
}

function appendRestoredTurn(turn, isLatest) {
  const thread = document.querySelector("[data-agent-answer]");
  if (!thread) return null;
  const article = document.createElement("article");
  article.className = "agent-turn";
  article.tabIndex = 0;
  article.dataset.historyRequestId = turn.request_id;
  article.innerHTML = `
    <div class="agent-turn-question">${escHtml(turn.question)}</div>
    <div data-agent-profile></div>
    <div class="agent-turn-answer"></div>
    ${turnEvidenceMarkup()}
  `;
  const answerEl = article.querySelector(".agent-turn-answer");
  renderAnswerPayload(turn.payload, answerEl);
  thread.appendChild(article);
  renderAuxiliaryPayload(
    turn.payload,
    answerEl?.parentElement || document,
    isLatest,
  );
  if (!isLatest)
    article.querySelectorAll("[data-agent-player-option]").forEach((button) => {
      button.disabled = true;
    });
  if (isLatest) currentAnswerEl = answerEl;
  return article;
}

async function restoreConversation(conversationId) {
  if (askInFlight) return;
  const navigation = ++navigationGeneration;
  if (!historyState.conversations.some((c) => c.id === conversationId)) {
    try {
      const response = await seasonFetch(
        `/api/agent/history?conversation_id=${encodeURIComponent(conversationId)}`,
      );
      if (!response.ok) throw new Error("History unavailable");
      saveHistoryState(
        mergeHistoryConversations((await response.json()).conversations),
      );
    } catch {
      storageNotice(
        "This saved chat is not in the browser cache and local history could not be reached.",
      );
      return;
    }
  }
  if (navigation !== navigationGeneration || askInFlight) return;
  const conversation = historyState.conversations.find(
    (item) => item.id === conversationId,
  );
  if (!conversation) return;
  rememberDraft();
  const empty = document.querySelector("[data-agent-empty]");
  const thread = document.querySelector("[data-agent-answer]");
  const statusEl = document.querySelector("[data-agent-status]");
  if (!thread || !empty) return;
  activeConversationId = conversation.id;
  lastQuestion =
    conversation.turns[conversation.turns.length - 1]?.question || "";
  currentAnswerEl = null;
  thread.innerHTML = "";
  empty.hidden = true;
  thread.hidden = false;
  viewedTurn = conversation.turns.length - 1;
  conversation.turns.forEach((turn, index) =>
    appendRestoredTurn(turn, index === viewedTurn),
  );
  if (statusEl) statusEl.textContent = "Restored";
  if (statusEl) statusEl.hidden = true;
  tabState = openTab(tabState, conversationId);
  showHistory(false);
  restoreDraft();
  saveTabs();
  renderTabs();
  renderTurnNavigation();
  renderHistoryList();
}

function startNewChat() {
  if (askInFlight) return;
  navigationGeneration += 1;
  rememberDraft();
  const empty = document.querySelector("[data-agent-empty]");
  const thread = document.querySelector("[data-agent-answer]");
  const statusEl = document.querySelector("[data-agent-status]");
  activeConversationId = null;
  tabState.active = null;
  // The draft tab also occupies one of the five visible positions.
  if (tabState.open.length >= 5) tabState.open = tabState.open.slice(-4);
  lastQuestion = "";
  currentAnswerEl = null;
  if (thread) {
    thread.innerHTML = "";
    thread.hidden = true;
  }
  if (empty) empty.hidden = false;
  resetAuxiliaryPanels(
    "Metric definitions and assumptions will appear after an answer.",
  );
  if (statusEl) statusEl.textContent = "Ready";
  if (statusEl) statusEl.hidden = true;
  showHistory(false);
  restoreDraft();
  saveTabs();
  renderTabs();
  document.querySelector("[data-agent-question]")?.focus();
  renderHistoryList();
}

async function clearHistory() {
  historyState = { version: 1, conversations: [] };
  removeStoredHistory();
  startNewChat();
  renderHistoryList();
  try {
    await seasonFetch("/api/agent/history", { method: "DELETE" });
  } catch {
    // Server-local history is best effort and should not block the UI.
  }
}

async function loadServerHistory(reset = false) {
  if (reset) {
    historyOffset = 0;
    historyGeneration += 1;
  }
  const generation = historyGeneration;
  const state = document.querySelector("[data-history-state]");
  const more = document.querySelector("[data-history-load-more]");
  if (state) state.textContent = "Loading saved chats…";
  if (more) more.disabled = true;
  try {
    const response = await seasonFetch(
      `/api/agent/history?limit=25&offset=${historyOffset}&q=${encodeURIComponent(historyQuery)}`,
    );
    if (!response.ok) throw new Error("History unavailable");
    const payload = await response.json();
    if (generation !== historyGeneration) return;
    const merged = mergeHistoryConversations(payload.conversations);
    saveHistoryState(merged);
    renderHistoryList();
    historyHasMore = payload.next_offset != null;
    historyOffset = payload.next_offset ?? historyOffset;
    if (state)
      state.textContent =
        "Saved locally. Select a chat to restore its original answer, table and charts.";
  } catch {
    if (generation === historyGeneration && state)
      state.textContent =
        "Local history is unavailable. Showing matching chats from this browser’s recent cache only.";
  } finally {
    if (generation === historyGeneration && more) {
      more.hidden = !historyHasMore;
      more.disabled = false;
    }
  }
}

function initHistory() {
  historyState = loadHistoryState();
  try {
    tabState = normalizeTabs(
      JSON.parse(storageProvider()?.getItem(TABS_STORAGE_KEY) || "{}"),
    );
  } catch {
    tabState = normalizeTabs();
  }
  if (!tabState.open.length)
    tabState.open = historyState.conversations.slice(0, 4).map((c) => c.id);
  renderHistoryList();
  renderTabs();
  const newButton = document.querySelector("[data-agent-new-chat]");
  const clearButton = document.querySelector("[data-agent-clear-history]");
  if (newButton instanceof HTMLButtonElement) {
    newButton.addEventListener("click", startNewChat);
  }
  if (clearButton instanceof HTMLButtonElement) {
    clearButton.addEventListener("click", clearHistory);
  }
  document
    .querySelector("[data-chat-more]")
    ?.addEventListener("click", () => showHistory());
  document
    .querySelector("[data-history-back]")
    ?.addEventListener("click", () => showHistory(false));
  document
    .querySelector("[data-history-load-more]")
    ?.addEventListener("click", () => loadServerHistory());
  let timer;
  document
    .querySelector("[data-history-search]")
    ?.addEventListener("input", (event) => {
      historyQuery = event.target.value.trim();
      historyGeneration += 1;
      renderHistoryList();
      clearTimeout(timer);
      timer = setTimeout(() => loadServerHistory(true), 250);
    });
  if (tabState.active) restoreConversation(tabState.active);
  else restoreDraft();
  loadServerHistory();
}

function renderTurnNavigation() {
  const el = document.querySelector("[data-turn-navigation]");
  const chat = historyState.conversations.find(
    (c) => c.id === activeConversationId,
  );
  if (!el || !chat) return;
  el.hidden = chat.turns.length < 2;
  if (viewedTurn < 0) viewedTurn = chat.turns.length - 1;
  el.innerHTML = `<label for="chat-turn">Review question</label><select id="chat-turn" class="input" ${askInFlight ? "disabled" : ""}>${chat.turns.map((t, i) => `<option value="${i}" ${i === viewedTurn ? "selected" : ""}>${i + 1}. ${escHtml(truncateText(t.question, 100))}</option>`).join("")}</select><span class="meta">Follow-ups continue the latest question in this chat.</span>`;
  el.querySelector("select")?.addEventListener("change", (event) => {
    if (askInFlight) return;
    viewedTurn = Number(event.target.value);
    const thread = document.querySelector("[data-agent-answer]");
    const turn = thread.children[viewedTurn];
    turn?.scrollIntoView({ block: "start" });
    turn?.focus({ preventScroll: true });
  });
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
    if (statusEl)
      statusEl.textContent = `${payload.name || "Tool"} ${payload.status || "done"}`;
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
    persistHistoryTurn(lastQuestion || "Question", payload.payload || {});
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
        `<option value="${escHtml(option.value)}">${escHtml(option.label || option.value)}</option>`,
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
          selectedModel(),
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
  const conversation = historyState.conversations.find(
    (item) => item.id === activeConversationId,
  );
  const previous = [...(conversation?.turns || [])]
    .reverse()
    .find((turn) => turn.payload?.status === "ok");
  if (previous) {
    const payload = previous.payload;
    const evidence = payload.semantic_evidence || {};
    const scope = evidence.scope || {};
    const profiles = payload.player_profiles || [
      payload.player_profile || payload.reference_player,
    ];
    body.previous_context = {
      question: previous.question.slice(0, 4000),
      players: profiles
        .filter((p) => p?.player?.player_id && p.player.player_name)
        .slice(0, 2)
        .map((p) => ({
          player_id: p.player.player_id,
          player_name: p.player.player_name.slice(0, 80),
        })),
      scope: {
        start: scope.start || scope.start_date || null,
        end: scope.end || scope.as_of || null,
        phases: (scope.phases || (scope.season_type ? [scope.season_type] : []))
          .filter((phase) =>
            ["Regular Season", "Playoffs", "Both"].includes(phase),
          )
          .slice(0, 2),
      },
      metrics: (evidence.metrics || [evidence.metric])
        .map((m) => m?.key)
        .filter((key) => ["pts", "reb", "ast", "stl", "blk"].includes(key))
        .slice(0, 5),
    };
  }
  if (selection && selection.playerName) {
    body.selected_player_id = selection.playerId || null;
    body.selected_player_name = selection.playerName;
  }
  return body;
}

async function askQuestionJson(question, selection) {
  const statusEl = document.querySelector("[data-agent-status]");
  const response = await seasonFetch("/api/agent/ask", {
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
  persistHistoryTurn(question, payload);
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
  const response = await seasonFetch("/api/agent/ask/stream", {
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
  if (askInFlight || !String(question).trim()) return;
  navigationGeneration += 1;
  rememberDraft();
  const wasNewChat = !activeConversationId;
  askInFlight = true;
  setBusy(true);
  const statusEl = document.querySelector("[data-agent-status]");
  const submit = document.querySelector("[data-agent-submit]");
  if (statusEl) statusEl.textContent = "Thinking";
  if (statusEl) statusEl.hidden = false;
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
        "Failed",
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
    setBusy(false);
    renderTabs();
    renderTurnNavigation();
    if (wasNewChat && statusEl?.textContent === "Answered")
      delete tabState.drafts.new;
    rememberDraft();
    if (statusEl?.textContent === "Answered") statusEl.hidden = true;
    if (submit instanceof HTMLButtonElement) submit.disabled = false;
  }
}

function setBusy(busy) {
  for (const [selector, label] of [
    ["[data-agent-submit]", "Ask"],
    ["[data-followup-submit]", "Ask follow-up"],
  ]) {
    const button = document.querySelector(selector);
    if (button) {
      button.textContent = busy ? "Thinking…" : label;
      button.dataset.thinking = String(busy);
    }
  }
  const status = document.querySelector("[data-agent-status]");
  if (status) status.dataset.thinking = String(busy);
  document
    .querySelectorAll(
      "[data-chat-tabs] button, [data-chat-more], [data-agent-new-chat], [data-followup-submit], [data-agent-submit], [data-agent-provider], [data-agent-model], [data-season-selector], #chat-turn",
    )
    .forEach((el) => {
      el.disabled = busy;
    });
}

function initAgentPage() {
  bindExampleButtons();
  initProviderSelect();
  initHistory();
  const form = document.querySelector("[data-agent-form]");
  const input = document.querySelector("[data-agent-question]");
  if (
    !(form instanceof HTMLFormElement) ||
    !(input instanceof HTMLTextAreaElement)
  ) {
    return;
  }
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const question = input.value.trim();
    if (!question) return;
    askQuestion(question);
  });
  const followupForm = document.querySelector("[data-followup-form]");
  const followupInput = document.querySelector("[data-followup-question]");
  followupForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!followupInput.value.trim() || askInFlight) return;
    const sentQuestion = followupInput.value.trim();
    await askQuestion(sentQuestion);
    if (
      document.querySelector("[data-agent-status]")?.textContent ===
        "Answered" &&
      followupInput.value.trim() === sentQuestion
    )
      followupInput.value = "";
    rememberDraft();
  });
  followupInput?.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      followupForm.requestSubmit();
    }
  });
  input.addEventListener("input", rememberDraft);
  followupInput?.addEventListener("input", rememberDraft);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      form.requestSubmit();
    }
  });
}

if (typeof window === "undefined" || window.__NBA_ASK_TEST_HOOKS__) {
  globalThis.__askAgentTest = {
    referenceTable,
    renderTable,
    buildAskBody,
    startTurn,
    setBusy,
    renderLineChart,
    normalizeAnswerMarkdown,
    renderAnswerMarkdown,
    loadHistoryState,
    saveHistoryState,
    persistHistoryTurn,
    bindExampleButtons,
    restoreConversation,
    clearHistory,
    normalizeHistoryState,
    renderOverviewTable,
    closeConversationTab,
    startNewChat,
    renderTabs,
    getNavigation: () => ({ ...tabState, activeConversationId }),
  };
}

if (typeof document !== "undefined") {
  document.addEventListener("DOMContentLoaded", initAgentPage);
}
