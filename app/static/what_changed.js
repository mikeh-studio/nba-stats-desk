import { seasonFetch } from "./season.js";
const escape = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"}[char]));
export const number = (value, signed = false) => value == null || !Number.isFinite(Number(value)) ? "—" : `${signed && Number(value) > 0 ? "+" : ""}${Number(value).toFixed(1)}`;
export function selectMovers(items, view, search = "") {
  const score = view === "surging" ? "surge_score" : "top_score";
  const query = search.trim().toLocaleLowerCase();
  return items.filter((item) => item[score] != null && (!query || `${item.player_name} ${item.team_abbr}`.toLocaleLowerCase().includes(query)))
    .sort((a, b) => b[score] - a[score] || a.player_id - b.player_id);
}
function dates(window) { return window?.start_date ? `${window.start_date} → ${window.end_date}` : "No comparison games"; }
function safeSource(url) {
  try { const parsed = new URL(url); return parsed.protocol === "https:" ? escape(parsed.href) : ""; } catch { return ""; }
}
function metricTable(cluster) {
  return `<table><thead><tr><th scope="col">Metric</th><th scope="col">Recent</th><th scope="col">Prior</th><th scope="col">Change</th></tr></thead><tbody>${cluster.metrics.map((metric) => {
    const delta = metric.delta;
    const favorable = delta == null || delta === 0 ? "" : (metric.lower_is_better ? delta < 0 : delta > 0) ? "delta-positive" : "delta-negative";
    return `<tr><th scope="row">${escape(metric.label)}</th><td>${number(metric.current)}${metric.unit === "percent" ? "%" : ""}${metric.current_total != null ? `<small>${number(metric.current_total)} total</small>` : ""}</td><td>${number(metric.previous)}${metric.unit === "percent" ? "%" : ""}${metric.previous_total != null ? `<small>${number(metric.previous_total)} total</small>` : ""}</td><td class="${favorable}">${number(delta, true)}${metric.unit === "percent" && delta != null ? " pp" : ""}</td></tr>`;
  }).join("")}</tbody></table>`;
}
function availability(item) {
  const c = item.current, p = item.previous;
  const reportList = c.injuries.reports.map((report) => {
    const url = safeSource(report.source_url);
    const text = `${report.game_date}: ${report.injury_status} — ${report.reason || "No reason supplied"}`;
    return `<li>${url ? `<a href="${url}" target="_blank" rel="noopener noreferrer">${escape(text)}</a>` : escape(text)} <span>(report ${escape(report.report_date)})</span></li>`;
  }).join("");
  return `<p class="metric-note">Recorded appearances: <strong>${c.games_played}/${c.team_games}</strong> recent · ${p.games_played}/${p.team_games} prior.<br>No recorded appearance: ${c.no_recorded_appearance} recent · ${p.no_recorded_appearance} prior. Reasons are unconfirmed.</p>
  <p class="metric-note">Injury-report coverage: ${c.injuries.games_covered}/${c.team_games} recent · ${p.injuries.games_covered}/${p.team_games} prior. ${c.injuries.available ? "Missing reports do not establish health." : "Historical injury reports are unavailable."}<br>Games with an Out report: ${number(c.injuries.out_reports)} recent · ${number(p.injuries.out_reports)} prior. These are reported statuses, not confirmed missed games.<br>DNP-CD: not available from this source.</p>${reportList ? `<ul class="mover-injuries">${reportList}</ul>` : ""}`;
}
export function renderMover(item, view) {
  const score = view === "surging" ? item.surge_score : item.top_score;
  const rank = view === "surging" ? item.surge_rank : item.top_rank;
  return `<article class="mover-card" data-player-id="${Number(item.player_id)}">
    <div class="mover-heading"><div><h2><span class="mover-rank">${rank}.</span><a href="/players/${Number(item.player_id)}">${escape(item.player_name)}</a></h2><p class="meta">${escape(item.team_abbr)} · ${item.current.games_played} recent appearances</p></div><div class="mover-score">${number(score, view === "surging")}<small>${view === "surging" ? "production increase / appearance" : "production index / appearance"}</small></div></div>
    <p class="mover-sample">Recent ${dates(item.current)} · Prior ${dates(item.previous)}<br>${escape(item.sample_note)}</p>
    <div class="mover-clusters">${item.clusters.map((cluster) => {
      const headline = cluster.metrics[0];
      return `<details class="mover-cluster"><summary>${escape(cluster.label)}<span>${escape(headline.label)}: ${number(headline.current)} · ${number(headline.delta, true)}</span></summary>${metricTable(cluster)}${cluster.key === "availability" ? availability(item) : `<p class="metric-note">Counting stats are per appearance; totals appear underneath.${cluster.key === "defense" ? " Box-score events do not measure all defensive impact." : " Shooting changes are percentage points."}</p>`}</details>`;
    }).join("")}</div>
    <details class="mover-games"><summary>Inspect the games behind this comparison</summary><div class="table-scroll"><table><thead><tr><th scope="col">Window / date</th><th scope="col">Opponent</th><th scope="col">MIN</th><th scope="col">PTS</th><th scope="col">REB</th><th scope="col">AST</th><th scope="col">STL</th><th scope="col">BLK</th><th scope="col">TOV</th></tr></thead><tbody>${[["Recent", item.current], ["Prior", item.previous]].flatMap(([label, window]) => window.games.map((game) => `<tr><th scope="row">${label} ${escape(game.game_date)}${game.played ? "" : "<br>No recorded appearance"}</th><td>${escape(game.opponent)}</td>${["min", "pts", "reb", "ast", "stl", "blk", "tov"].map((key) => `<td>${number(game.stats[key])}</td>`).join("")}</tr>`)).join("")}</tbody></table></div></details>
  </article>`;
}

// A point requires a comparable baseline; high-production players can still
// appear in the ranked list when their previous window is insufficient.
export function opportunityPoints(items, minimum = 3, fullWindow = true) {
  return items.flatMap((item) => {
    const recent = item.current?.values?.min, prior = item.previous?.values?.min;
    const delta = item.production_delta;
    if (recent == null || prior == null || delta == null || item.team_changed
      || item.previous.games_played < minimum || (fullWindow && item.previous.team_games < 4)
      || ![recent, prior, delta].every(Number.isFinite)) return [];
    return [{item, x: recent - prior, y: delta}];
  });
}
export function chartAxis(values) {
  const low = Math.min(0, ...values), high = Math.max(0, ...values);
  const rough = Math.max(high - low, 2) / 5;
  const power = 10 ** Math.floor(Math.log10(rough));
  const step = [1, 2, 5, 10].map(value => value * power).find(value => value >= rough);
  const min = Math.floor(low / step) * step - step, max = Math.ceil(high / step) * step + step;
  const ticks = [];
  for (let i = 0; i <= Math.round((max - min) / step); i++) ticks.push(Number((min + i * step).toFixed(6)));
  return {min, max, ticks};
}
export function renderOpportunityMap(points, selectedId) {
  if (!points.length) return '<p class="empty-state">No comparable prior windows are available to plot. Player details remain available in the ranked list.</p>';
  const xa = chartAxis(points.map(point => point.x)), ya = chartAxis(points.map(point => point.y));
  const x = value => 75 + (value - xa.min) / (xa.max - xa.min) * 590;
  const y = value => 395 - (value - ya.min) / (ya.max - ya.min) * 335;
  const selected = points.find(point => point.item.player_id === selectedId);
  // The selected point is drawn last so it stays visible when coordinates overlap.
  const ordered = points.filter(point => point !== selected).concat(selected ? [selected] : []);
  return `<div class="opportunity-scroll"><svg viewBox="0 0 740 465" role="group" aria-label="Opportunity map: minutes change versus production change">
    <text x="18" y="22" class="axis-title">PRODUCTION CHANGE / APPEARANCE</text>
    ${ya.ticks.map(value => `<line x1="75" x2="665" y1="${y(value)}" y2="${y(value)}" class="${value === 0 ? 'zero-line' : 'grid-line'}"/><text x="62" y="${y(value)+4}" text-anchor="end">${number(value,true)}</text>`).join('')}
    ${xa.ticks.map(value => `<line x1="${x(value)}" x2="${x(value)}" y1="60" y2="395" class="${value === 0 ? 'zero-line' : 'grid-line'}"/><text x="${x(value)}" y="420" text-anchor="middle">${number(value,true)}</text>`).join('')}
    <text x="370" y="453" text-anchor="middle" class="axis-title">MINUTES CHANGE / APPEARANCE</text>
    ${ordered.map(point => `<g role="button" tabindex="0" data-select-player="${Number(point.item.player_id)}" aria-pressed="${point.item.player_id === selectedId}" aria-label="Select ${escape(point.item.player_name)}: minutes ${number(point.x,true)}, production ${number(point.y,true)}"><title>${escape(point.item.player_name)} · ${escape(point.item.team_abbr)}: minutes ${number(point.x,true)}, production ${number(point.y,true)}</title><circle class="point-hit" cx="${x(point.x)}" cy="${y(point.y)}" r="13"/><circle class="point" cx="${x(point.x)}" cy="${y(point.y)}" r="${point.item.player_id === selectedId ? 7 : 4}"/></g>`).join('')}
    ${selected ? `<text class="point-label" x="${x(selected.x)}" y="${y(selected.y)-18}" text-anchor="${x(selected.x) > 540 ? 'end' : x(selected.x) < 180 ? 'start' : 'middle'}">${escape(selected.item.player_name)}</text>` : ''}
  </svg></div>`;
}

export function initWhatChanged(doc = document, fetcher = seasonFetch) {
  const form = doc.querySelector("#changed-controls");
  if (!form) return;
  let payload = null, view = "top", shown = 20, requestId = 0, selectedId = null;
  const list = doc.querySelector("#changed-list"), status = doc.querySelector("#changed-status"), more = doc.querySelector("#changed-more"), search = doc.querySelector("#changed-search");
  const chart = doc.querySelector("#changed-chart"), detail = doc.querySelector("#changed-detail");
  function bindSelections(root) {
    root.querySelectorAll("[data-select-player]").forEach(node => {
      const select = () => {
        selectedId = Number(node.dataset.selectPlayer);
        const inChart = Boolean(node.closest("#changed-chart"));
        render();
        const target = (inChart ? chart : list).querySelector(`[data-select-player="${selectedId}"]`);
        target?.focus({preventScroll:true});
      };
      node.addEventListener("click", select);
      if (node.tagName.toLowerCase() === "g") node.addEventListener("keydown", event => {
        if (event.key === "Enter" || event.key === " ") { event.preventDefault(); select(); }
      });
    });
  }
  function render() {
    if (!payload) return;
    const items = selectMovers(payload.items, view, search.value);
    const excluded = payload.items.filter((item) => item.top_score == null).length;
    doc.querySelector("#changed-ranking").textContent = `${view === "surging" ? payload.ranking.surging : payload.ranking.top}. ${payload.ranking.formula}.`;
    status.textContent = items.length ? `${items.length} matching players. ${excluded} players excluded from top rankings for limited samples or minutes.` : payload.state === "empty" ? "No games are available for this season phase and date." : view === "surging" ? "No players meet the positive-surge and sample requirements for these filters." : "No players meet the sample and minutes requirements for these filters.";
    if (!items.some(item => item.player_id === selectedId)) selectedId = items[0]?.player_id ?? null;
    const points = opportunityPoints(items, payload.ranking.minimum_games, payload.period === "four_games");
    chart.innerHTML = items.length ? renderOpportunityMap(points, selectedId) : "";
    doc.querySelector("#changed-chart-note").textContent = items.length ? `${points.length} of ${items.length} matching players have comparable baselines. Select a point or ranked player. Overlapping points remain individually selectable below. More minutes and more production appear above and right of zero; this does not establish causation.` : "";
    list.innerHTML = items.slice(0, shown).map(item => `<button type="button" class="mover-select" data-select-player="${Number(item.player_id)}" aria-pressed="${item.player_id === selectedId}"><span><strong>${escape(item.player_name)}</strong><small>${escape(item.team_abbr)} · ${item.current.games_played} appearances</small></span><span class="mover-select-score">${number(view === "surging" ? item.surge_score : item.top_score, view === "surging")}<small>${view === "surging" ? 'production change' : 'production index'}</small></span></button>`).join("");
    const selected = items.find(item => item.player_id === selectedId);
    detail.innerHTML = selected ? `<p class="eyebrow">Selected player</p>${renderMover(selected, view)}` : "";
    detail.querySelectorAll('.mover-cluster').forEach(node => {node.open = true;});
    bindSelections(chart); bindSelections(list);
    more.hidden = items.length <= shown;
  }
  async function load() {
    const id = ++requestId;
    payload = null;
    list.innerHTML = "";
    chart.innerHTML = ""; detail.innerHTML = "";
    doc.querySelector("#changed-chart-note").textContent = "";
    list.setAttribute("aria-busy", "true");
    more.hidden = true;
    status.textContent = "Loading league movers…";
    doc.querySelector("#changed-dates").textContent = "";
    doc.querySelector("#changed-ranking").textContent = "";
    const params = new URLSearchParams();
    for (const [key, value] of new FormData(form)) if (value) params.set(key, value);
    try {
      const response = await fetcher(`/api/what-changed?${params}`);
      if (!response.ok) throw new Error("unavailable");
      const result = await response.json();
      if (id !== requestId) return;
      payload = result; shown = 20;
      const weeks = result.calendar_windows;
      doc.querySelector("#changed-dates").textContent = weeks
        ? `Recent week ${weeks.current.start} → ${weeks.current.end} · Previous week ${weeks.previous.start} → ${weeks.previous.end}. Source games through ${result.data_through}.`
        : `Latest games through ${result.data_through || "—"}. Each team’s last four games are compared with its previous four; selected player details show exact dates.`;
      render();
    } catch {
      if (id === requestId) status.textContent = "Unable to load a complete league comparison. Try Update again; no partial ranking is shown.";
    } finally { if (id === requestId) list.setAttribute("aria-busy", "false"); }
  }
  form.addEventListener("submit", (event) => { event.preventDefault(); load(); });
  doc.querySelector("#changed-period")?.addEventListener("change", () => load());
  doc.querySelector("#changed-phase")?.addEventListener("change", () => load());
  search.addEventListener("input", () => { shown = 20; render(); });
  doc.querySelectorAll("[data-mover-view]").forEach((button) => button.addEventListener("click", () => {
    view = button.dataset.moverView; shown = 20;
    doc.querySelectorAll("[data-mover-view]").forEach((node) => { const selected = node === button; node.classList.toggle("is-active", selected); node.setAttribute("aria-pressed", String(selected)); });
    render();
  }));
  more.addEventListener("click", () => { shown += 20; render(); });
  load();
}
if (typeof document !== "undefined") initWhatChanged();
