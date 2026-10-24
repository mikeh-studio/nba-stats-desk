import {prepareClaimEvidence} from "./ask_claim_bindings.js?v=20261023-ask-stable-buttons-v5";
import { dateLabel, ordinal } from "./ask_presentation.js?v=20261023-ask-stable-buttons-v5";

const esc = (v) =>
  String(v ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
const num = (v, digits = 1) =>
  Number.isFinite(v) ? v.toFixed(digits) : "Unavailable";

// Old saved comparisons retain their payloads; hide their obsolete missing-feed copy.
const legacyImpactDisclosure = (text) =>
  /Win Shares.*(?:unavailable|not connected|need a verified)|on\/off possession data are not connected/i.test(String(text));

export function comparisonAssumptions(payload) {
  const assumptions = Array.isArray(payload.assumptions) ? payload.assumptions : [];
  return payload.semantic_evidence?.kind === "player_comparison"
    ? assumptions.filter((text) => !legacyImpactDisclosure(text))
    : assumptions;
}

export function comparisonPresentation(payload) {
  const evidence = payload.semantic_evidence;
  if (
    evidence?.kind !== "player_comparison" ||
    payload.player_profiles?.length !== 2
  )
    return null;
  const profiles = payload.player_profiles;
  const names = profiles.map((p) => p.player.player_name);
  const metrics = evidence.metrics;
  const scope = `${evidence.scope.phases.join(" + ")} · ${dateLabel(evidence.scope.start)} – ${dateLabel(evidence.scope.end)}`;
  const edges = (keys) =>
    metrics
      .filter((m) => keys.includes(m.key) && Number.isFinite(m.delta))
      .map((m) =>
        m.delta === 0
          ? `${m.label} are tied`
          : `${names[m.delta > 0 ? 0 : 1]} leads ${m.label.toLowerCase()} by ${num(Math.abs(m.delta))} per game`,
      )
      .join("; ");
  return {
    ...evidence,
    claimEvidence:prepareClaimEvidence(payload),
    claimPayload:payload,
    impact: (Array.isArray(evidence.impact) ? evidence.impact : []).filter(
      (m) => Number.isFinite(m.left) && Number.isFinite(m.right),
    ),
    paragraphs: [...new Set((evidence.paragraphs || []).map((text) => legacyImpactDisclosure(text)
      ? "These box-score differences do not establish an overall winner. Playing time, roles, teammates and opponents affect the comparison."
      : text))],
    sections: Array.isArray(evidence.comparison_sections) ? evidence.comparison_sections : [],
    profiles,
    names,
    scopeLabel: scope,
    insights: [
      {
        icon: "chart-bar",
        title: "Scoring and creation",
        text:
          (edges(["pts", "ast"]) ||
            "Complete scoring and assist data are unavailable") +
          ". Higher volume alone does not establish greater efficiency.",
      },
      {
        icon: "shield",
        title: "Rebounding and defensive activity",
        text:
          (edges(["reb", "stl", "blk"]) ||
            "Complete rebound, steal and block data are unavailable") +
          ". Steals and blocks are not a complete defensive assessment.",
      },
      {
        icon: "chart-line",
        title: "Playing time and workload",
        text: profiles.map((p, i) => `${names[i]}: ${p.player.games_sampled} appearances${Number.isFinite(p.minutes_per_game) ? `, ${num(p.minutes_per_game)} minutes per game` : ""}`).join("; ") + ". Minutes and run length affect totals; appearances are not a measure of health.",
      },
    ],
  };
}

export function comparisonAnswer(view) {
  return `<div class="comparison-overall"><div class="overall-copy"><h2>Overall</h2>${view.paragraphs.map((p) => `<p>${esc(p)}</p>`).join("")}</div><aside class="comparison-takeaways" aria-label="Key takeaways">${view.insights.map((i) => `<section class="takeaway"><img src="/static/icons/${i.icon}.svg" alt="" /><div><h3>${esc(i.title)}</h3><p>${esc(i.text)}</p></div></section>`).join("")}</aside></div>`;
}

export function comparisonProfiles(view, renderProfile) {
  return `<p class="meta comparison-eyebrow">Head-to-head scorecard</p><div class="comparison-identities">${view.profiles.map((p) => renderProfile({ ...p, scopeLabel: `${p.player.games_sampled === 0 ? "0 appearances · " : ""}${num(p.minutes_per_game)} min / game · ${num(p.minutes)} total minutes` })).join("")}</div><p class="meta">${esc(view.scopeLabel)} · Data through ${esc(dateLabel(view.data_through))}</p>`;
}

function metricCell(m, label) {
  return `<td>${num(m.value)}${m.missing_component_games ? `<small>Partial: ${m.valid_games}/${m.observed_games} games</small>` : ""}</td><td><div class="percentile-cell">${Number.isFinite(m.percentile) ? `<meter min="0" max="100" value="${Math.round(m.percentile)}" aria-label="${esc(label)} percentile">${ordinal(m.percentile)}</meter><span>${ordinal(m.percentile)}</span>` : "Unavailable"}</div></td>`;
}

export function comparisonTables(view) {
  return `<p class="metric-comparison">${esc(view.scopeLabel)}</p><div class="table-scroll comparison-scroll" tabindex="0" role="region" aria-label="Comparison table; scroll horizontally for all columns"><table class="overview-table comparison-table"><caption class="sr-only">Per-game player comparison in the same dates and phases</caption><thead><tr><th colspan="2" scope="colgroup" class="comparison-left">${esc(view.names[0])}</th><th></th><th colspan="2" scope="colgroup" class="comparison-right">${esc(view.names[1])}</th><th></th></tr><tr><th scope="col">Average</th><th scope="col">Scope percentile</th><th scope="col">Stat</th><th scope="col">Average</th><th scope="col">Scope percentile</th><th scope="col">Edge</th></tr></thead><tbody>${view.metrics.map((m) => `<tr>${metricCell(m.left, `${view.names[0]} ${m.label}`)}<th scope="row">${esc(m.label)}</th>${metricCell(m.right, `${view.names[1]} ${m.label}`)}<td class="${m.delta > 0 ? "comparison-left" : m.delta < 0 ? "comparison-right" : ""}">${m.delta === null ? "Unavailable" : m.delta === 0 ? "Tied" : `${esc(view.names[m.delta > 0 ? 0 : 1])} by ${num(Math.abs(m.delta))}`}</td></tr>`).join("")}</tbody></table></div><p class="metric-footnote">Scroll horizontally for all columns on narrow screens. Same dates and phases · minimum ${view.min_games} complete games per metric · ${new Set(view.metrics.map((m) => m.left.cohort_size)).size === 1 ? `${view.metrics[0].left.cohort_size} qualified players` : "Qualification varies by metric; see methodology"}. Percentiles describe each stat, not overall quality. Edges use unrounded averages.</p>${comparisonAdditionalTables(view)}${comparisonImpactTable(view)}`;
}

function comparisonAdditionalTables(view) {
  return view.sections.map((section, sectionIndex) => `<section data-comparison-section-index="${sectionIndex}" class="comparison-additional" data-comparison-section="${esc(section.key)}"><h2>${esc(section.title)}</h2><p class="meta">${esc(section.description)}</p><div class="table-scroll comparison-scroll" tabindex="0" role="region" aria-label="${esc(section.title)} comparison"><table class="overview-table comparison-detail-table"><caption class="sr-only">${esc(section.title)} in the same dates and phases</caption><thead><tr><th scope="col">Metric</th><th scope="col">${esc(view.names[0])}</th><th scope="col">${esc(view.names[1])}</th><th scope="col">Comparison</th></tr></thead><tbody>${section.metrics.map((m) => `<tr><th scope="row">${esc(m.label)}<small>${esc(m.unit)}</small></th>${[m.left, m.right].map(side => `<td>${num(side.value)}${m.unit === "percent" && Number.isFinite(side.value) ? "%" : ""}<small>${side.valid_games} of ${side.observed_games} games${side.missing_component_games ? " · partial inputs" : ""}</small>${m.unit === "percent" ? `<small>${Object.entries(side.components || {}).filter(([key]) => ["fga", "fg3a", "fta"].includes(key)).map(([key, value]) => `${num(value, 0)} ${key === "fg3a" ? "3PA" : key.toUpperCase()}`).join(" · ")}</small>` : ""}</td>`).join("")}<td>${esc(m.comparison)}</td></tr>`).join("")}</tbody></table></div></section>`).join("");
}

function comparisonImpactTable(view) {
  if (!view.impact.length) return "";
  return `<section class="comparison-impact"><h2>Impact on winning</h2><div class="table-scroll comparison-scroll" tabindex="0" role="region" aria-label="Winning impact comparison"><table class="overview-table impact-table"><thead><tr><th scope="col">Metric</th><th scope="col">${esc(view.names[0])}</th><th scope="col">${esc(view.names[1])}</th><th scope="col">How to read it</th></tr></thead><tbody>${view.impact.map((m) => `<tr><th scope="row">${esc(m.label)}</th><td>${num(m.left, m.key === "ws48" ? 3 : 1)}</td><td>${num(m.right, m.key === "ws48" ? 3 : 1)}</td><td>${esc(m.definition)}</td></tr>`).join("")}</tbody></table></div></section>`;
}

export function comparisonChart(view, key = "pts") {
  const metric = view.metrics.find((m) => m.key === key) || view.metrics[0];
  const controls = `<div class="chart-switcher" aria-label="Comparison chart metric">${view.metrics.map((m) => `<button type="button" data-comparison-metric="${esc(m.key)}" aria-pressed="${metric.key === m.key}">${esc(m.key.toUpperCase())}</button>`).join("")}</div>`;
  const values = [metric.left, metric.right];
  const available = values.map((m) =>
    m.missing_component_games ? null : m.value,
  );
  const max = Math.max(1, ...available.filter(Number.isFinite)) * 1.15;
  return `<div class="chart-header"><div><h2>${esc(metric.label)} per game comparison</h2><p class="meta">${esc(view.scopeLabel)}</p></div>${controls}</div><div class="comparison-bars" role="img" aria-label="${esc(view.names.map((n, i) => `${n}: ${num(available[i])} ${metric.label} per game`).join("; "))}">${available.map((value, i) => `<div class="comparison-bar-row"><span>${esc(view.names[i])}</span><div class="comparison-bar-track">${Number.isFinite(value) ? `<div class="comparison-bar comparison-bar-${i}" style="width:${Math.max(0, (value / max) * 100)}%"></div>` : ""}</div><strong>${num(value)}</strong></div>`).join("")}<div class="comparison-bar-axis"><span>0</span><span>${num(max / 2)}</span><span>${num(max)}</span></div></div><p class="meta">Shared zero baseline · per-game averages, not total contribution · incomplete metric samples are withheld from the chart.</p>`;
}
