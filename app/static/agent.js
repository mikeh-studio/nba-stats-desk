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
  const height = 250;
  const pad = { top: 26, right: 24, bottom: 48, left: 46 };
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
  const maxValue = Math.max(...points.map((point) => point.yValue), 100);
  const barGap = 10;
  const barWidth = Math.max(16, (innerWidth - barGap * (points.length - 1)) / points.length);
  const bars = points
    .map((point, index) => {
      const barHeight = (point.yValue / maxValue) * innerHeight;
      const x = pad.left + index * (barWidth + barGap);
      const y = bottom - barHeight;
      return `
        <g class="agent-point" tabindex="0" aria-label="${escHtml(point.xLabel)} ${formatNumber(point.yValue)}">
          <rect class="agent-bar" x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${barWidth.toFixed(1)}" height="${barHeight.toFixed(1)}" rx="6" />
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

function renderChart(chart) {
  const chartBody = chart.type === "bar" ? renderBarChart(chart) : renderLineChart(chart);
  return `
    <article class="agent-chart">
      <div class="agent-chart-title">${escHtml(chart.title || "Chart")}</div>
      ${chartBody}
    </article>
  `;
}

function renderContext(payload) {
  const assumptions = asArray(payload.assumptions);
  const definitions = asArray(payload.metric_definitions);
  const followups = asArray(payload.followups);
  const toolCalls = asArray(payload.tool_calls);
  const blocks = [];
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
      if (!(input instanceof HTMLTextAreaElement)) return;
      input.value = button.dataset.agentExample || "";
      input.focus();
    });
  });
}

function renderPayload(payload) {
  const empty = document.querySelector("[data-agent-empty]");
  const answerEl = document.querySelector("[data-agent-answer]");
  const tableCard = document.querySelector("[data-agent-table-card]");
  const tableEl = document.querySelector("[data-agent-tables]");
  const chartCard = document.querySelector("[data-agent-chart-card]");
  const chartEl = document.querySelector("[data-agent-charts]");
  const contextEl = document.querySelector("[data-agent-context]");
  if (!answerEl || !empty || !tableCard || !tableEl || !chartCard || !chartEl || !contextEl) {
    return;
  }

  empty.hidden = true;
  answerEl.hidden = false;
  answerEl.innerHTML = `<div class="agent-answer-text">${escHtml(payload.answer || "No answer returned.")}</div>`;

  const tables = asArray(payload.tables);
  tableCard.hidden = tables.length === 0;
  tableEl.innerHTML = tables.map(renderTable).join("");

  const charts = asArray(payload.charts);
  chartCard.hidden = charts.length === 0;
  chartEl.innerHTML = charts.map(renderChart).join("");

  contextEl.innerHTML = renderContext(payload);
  bindExampleButtons(contextEl);
}

let activeConversationId = null;

function setInterimAnswer(text) {
  const empty = document.querySelector("[data-agent-empty]");
  const answerEl = document.querySelector("[data-agent-answer]");
  if (!answerEl || !empty) return;
  empty.hidden = true;
  answerEl.hidden = false;
  answerEl.innerHTML = `<div class="agent-answer-text">${escHtml(text || "")}</div>`;
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

async function askQuestionJson(question) {
  const statusEl = document.querySelector("[data-agent-status]");
  const response = await fetch("/api/agent/ask", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question, conversation_id: activeConversationId }),
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

async function askQuestionStream(question) {
  const response = await fetch("/api/agent/ask/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question, conversation_id: activeConversationId }),
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

async function askQuestion(question) {
  const statusEl = document.querySelector("[data-agent-status]");
  const submit = document.querySelector("[data-agent-submit]");
  if (statusEl) statusEl.textContent = "Thinking";
  if (submit instanceof HTMLButtonElement) submit.disabled = true;
  try {
    await askQuestionStream(question);
  } catch (streamError) {
    if (streamError instanceof Error && streamError.receivedEvents) {
      renderAskFailure(
        "Ask NBA Stats lost the connection before finishing. Try again shortly.",
        "Failed"
      );
    } else {
      try {
        await askQuestionJson(question);
      } catch {
        renderAskFailure("Ask NBA Stats failed to reach the API.", "Failed");
      }
    }
  } finally {
    if (submit instanceof HTMLButtonElement) submit.disabled = false;
  }
}

function initAgentPage() {
  bindExampleButtons();
  const form = document.querySelector("[data-agent-form]");
  const input = document.querySelector("[data-agent-question]");
  if (!(form instanceof HTMLFormElement) || !(input instanceof HTMLTextAreaElement)) {
    return;
  }
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const question = input.value.trim();
    if (!question) return;
    askQuestion(question);
  });
}

document.addEventListener("DOMContentLoaded", initAgentPage);
