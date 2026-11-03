import {comparisonPlot} from "./comparison_plot_ui.js?v=20261030-unified-checkpoint-v1";
import {comparisonLead, comparisonStatus} from "./comparison_policy.js?v=20261030-unified-checkpoint-v1";
import {prepareClaimEvidence} from "./ask_claim_bindings.js?v=20261030-unified-checkpoint-v1";
import { dateLabel, ordinal } from "./ask_presentation.js?v=20261030-unified-checkpoint-v1";
import {availableColumns, comparisonTableColumns, comparisonPrecisionColumns, comparisonDetailColumns} from "./table_visibility.js?v=20261030-unified-checkpoint-v1";
import {periodRange} from "./period_labels.js?v=20261030-unified-checkpoint-v1";

const esc = (v) =>
  String(v ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
const finite = (v) => typeof v === "number" && Number.isFinite(v);
const num = (v, digits = 1) =>
  Number.isFinite(v) ? v.toFixed(digits) : "Unavailable";
const usable = (side) => side && finite(side.value) && !side.missing_component_games;
const ranked = (m) => [m.left, m.right].every((s) => usable(s) && finite(s.percentile));
const SHORT = { pts: "PTS", reb: "REB", ast: "AST", stl: "STL", blk: "BLK", pts_per36: "PTS/36", ast_per36: "AST/36", ts_pct: "TS%" };
const RATE_LABEL = {pts: "Scoring", reb: "Rebounding", ast: "Assist", stl: "Steal", blk: "Block"};
const AREAS = [
  { key: "offense", title: "Scoring & assists", icon: "chart-bar" },
  { key: "defense", title: "Rebounds, steals & blocks", icon: "shield" },
  { key: "availability", title: "Playing time", icon: "clock" },
];

// Old saved comparisons retain their payloads; hide their obsolete missing-feed copy.
const legacyImpactDisclosure = (text) =>
  /Win Shares.*(?:unavailable|not connected|need a verified)|on\/off possession data are not connected/i.test(String(text));

export function comparisonAssumptions(payload) {
  const assumptions = Array.isArray(payload.assumptions) ? payload.assumptions : [];
  return payload.semantic_evidence?.kind === "player_comparison"
    ? assumptions.filter((text) => !legacyImpactDisclosure(text))
    : assumptions;
}

// Initials keep the meter compact; identical initials fall back to surnames.
function shortNames(names) {
  const suffix = /^(jr\.?|sr\.?|ii|iii|iv)$/i;
  const parts = (n) => n.split(/\s+/).filter((p) => p && !suffix.test(p));
  const surnames = names.map((n) => parts(n).at(-1) || n);
  const initials = names.map((n) => parts(n).filter((p) => /^[A-Za-z]/.test(p)).map((p) => p[0].toUpperCase()).join(""));
  return { surnames, tags: initials[0] && initials[0] !== initials[1] ? initials : surnames };
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
  const { tags, surnames } = shortNames(names);
  const metrics = evidence.metrics;
  const byKey = Object.fromEntries(metrics.map((m) => [m.key, m]));
  const sections = Array.isArray(evidence.comparison_sections) ? evidence.comparison_sections : [];
  // Per-36 and efficiency rows come from the server's governed detail sections.
  const detail = Object.fromEntries(sections.flatMap((s) => s.metrics).map((m) => [m.key, m]));
  const scope = `${evidence.scope.phases.join(" + ")} · ${periodRange(evidence.scope)}`;
  const games = profiles.map((p) => p.player.games_sampled || 0);
  const mpg = profiles.map((p) => p.minutes_per_game);
  const minutes = profiles.map((p) => p.minutes);
  const leadOf = (left, right) =>
    finite(left) && finite(right) && left !== right ? (left > right ? 0 : 1) : null;
  const deltaLead = comparisonLead;
  const cells = (values, lead, format) =>
    values.map((v, i) => ({ text: format(v), strong: lead === i, muted: lead === 1 - i }));
  const metricRow = (m) =>
    m && { label: SHORT[m.key] || m.label, cells: cells([m.left, m.right], deltaLead(m), (s) => (usable(s) ? s.value.toFixed(1) : "—")) };
  const detailRow = (m) =>
    m && {
      label: SHORT[m.key] || m.label,
      cells: cells(
        [m.left, m.right],
        usable(m.left) && usable(m.right) ? leadOf(m.left.value, m.right.value) : null,
        (s) => (usable(s) ? `${s.value.toFixed(1)}${m.unit === "percent" ? "%" : ""}` : "—"),
      ),
    };
  const edge = (m) =>
    !m || !finite(m.delta)
      ? ""
      : deltaLead(m) === null
        ? comparisonStatus(m) === "Similar rates"
          ? `${RATE_LABEL[m.key] || m.label} rates are similar`
          : ""
        : `${surnames[deltaLead(m)]} averages ${num(Math.abs(m.delta))} more ${m.label.toLowerCase()}`;
  const pair = (m, lead) => {
    const [a, b] = lead === 0 ? [m.left, m.right] : [m.right, m.left];
    return `${a.value.toFixed(1)} vs ${b.value.toFixed(1)} per game (${ordinal(a.percentile)} vs ${ordinal(b.percentile)} percentile)`;
  };
  const widest = (list) =>
    list
      .filter((m) => ranked(m) && deltaLead(m) !== null)
      .sort((a, b) => Math.abs(b.left.percentile - b.right.percentile) - Math.abs(a.left.percentile - a.right.percentile))[0];

  const pts36 = detail.pts_per36, ts = detail.ts_pct;
  const pointsLead = deltaLead(byKey.pts), assistLead = deltaLead(byKey.ast);
  const per36Lead = pts36 && usable(pts36.left) && usable(pts36.right) ? leadOf(pts36.left.value, pts36.right.value) : null;
  const offense = [
    pointsLead !== null && assistLead !== null && pointsLead === assistLead
      ? `${surnames[pointsLead]} averages ${num(Math.abs(byKey.pts.delta))} more points and ${num(Math.abs(byKey.ast.delta))} more assists`
      : [edge(byKey.pts), edge(byKey.ast)].filter(Boolean).join("; ") || "Complete scoring and assist data are unavailable",
    per36Lead !== null
      ? `Per 36 minutes, ${surnames[0]} scores ${pts36.left.value.toFixed(1)} and ${surnames[1]} ${pts36.right.value.toFixed(1)}${pointsLead !== null && per36Lead !== pointsLead ? ", so the per-game scoring edge comes with more minutes" : ""}`
      : "",
    ts && usable(ts.left) && usable(ts.right) ? `True shooting: ${ts.left.value.toFixed(1)}% vs ${ts.right.value.toFixed(1)}%` : "",
  ];
  const activity = ["reb", "stl", "blk"].map((k) => byKey[k]).filter(Boolean);
  const activityEdge = widest(activity);
  const defense = [
    "Box-score activity only: rebounds, steals and blocks",
    activityEdge ? `${surnames[deltaLead(activityEdge)]} has a higher recorded ${(RATE_LABEL[activityEdge.key] || activityEdge.label).toLowerCase()} rate, ${pair(activityEdge, deltaLead(activityEdge))}` : "",
    activity.filter((m) => comparisonStatus(m) === "Similar rates").map((m) => `${RATE_LABEL[m.key] || m.label} rates are similar`).join("; "),
    "These counts do not measure overall defensive impact",
  ];
  const absent = names.filter((_, i) => games[i] === 0);
  const availability = [
    names.map((_, i) => `${surnames[i]}: ${games[i]} appearances${finite(mpg[i]) ? `, ${mpg[i].toFixed(1)} minutes per game` : ""}`).join("; "),
    absent.length ? `No recorded appearances for ${absent.join(" and ")} in this scope; missing results are not zero` : "",
    "Minutes and run length affect totals; appearances are not a measure of health",
  ];
  const sentences = (parts) => parts.filter(Boolean).map((p) => `${p}.`).join(" ");
  const areas = [
    { ...AREAS[0], text: sentences(offense), rows: [metricRow(byKey.pts), metricRow(byKey.ast), detailRow(pts36), detailRow(detail.ast_per36), detailRow(ts)].filter(Boolean) },
    { ...AREAS[1], text: sentences(defense), rows: activity.map(metricRow) },
    {
      ...AREAS[2],
      text: sentences(availability),
      rows: [
        { label: "Games", cells: cells(games, leadOf(games[0], games[1]), String) },
        { label: "MPG", cells: cells(mpg, leadOf(mpg[0], mpg[1]), (v) => (finite(v) ? v.toFixed(1) : "—")) },
        { label: "Minutes", cells: cells(minutes, leadOf(minutes[0], minutes[1]), (v) => (finite(v) ? Math.round(v).toLocaleString("en-US") : "—")) },
      ],
    },
  ];

  const summaryEdges = [edge(byKey.pts), edge(byKey.reb)].filter(Boolean);
  const verdict = {
    text: summaryEdges.length
      ? summaryEdges.join("; ") + "."
      : "Complete points and rebounds data are unavailable.",
    note: "Recorded rates describe this sample and do not establish an overall winner or defensive ability.",
  };

  const takeaways = [];
  const evidenceLine = (label, a, b) => `${label} · ${tags[0]} ${a} · ${tags[1]} ${b}`;
  const top = widest(metrics);
  if (top) {
    const lead = deltaLead(top);
    takeaways.push({
      title: `${surnames[lead]} has a higher ${(RATE_LABEL[top.key] || top.label).toLowerCase()} rate`,
      text: `${pair(top, lead)}: the widest percentile gap in this comparison.`,
      evidence: evidenceLine(SHORT[top.key], top.left.value.toFixed(1), top.right.value.toFixed(1)),
    });
    const other = 1 - lead;
    const reply = widest(metrics.filter((m) => deltaLead(m) === other));
    const close = metrics.find((m) => comparisonStatus(m) === "Similar rates");
    if (reply)
      takeaways.push({
        title: `${surnames[other]} has a higher ${(RATE_LABEL[reply.key] || reply.label).toLowerCase()} rate`,
        text: `${pair(reply, other)}.`,
        evidence: evidenceLine(SHORT[reply.key], reply.left.value.toFixed(1), reply.right.value.toFixed(1)),
      });
    else if (close)
      takeaways.push({
        title: `${RATE_LABEL[close.key] || close.label} rates are similar`,
        text: `${close.left.value.toFixed(1)} vs ${close.right.value.toFixed(1)} per game; the difference is small under the display rule.`,
        evidence: evidenceLine(SHORT[close.key], close.left.value.toFixed(1), close.right.value.toFixed(1)),
      });
  }
  if (finite(mpg[0]) && finite(mpg[1])) {
    const diff = mpg[0] - mpg[1];
    takeaways.push(
      Math.abs(diff) >= 1
        ? {
            title: `${surnames[diff > 0 ? 0 : 1]} plays ${Math.abs(diff).toFixed(1)} more minutes per game`,
            text: "Higher per-game averages can reflect more playing time, better per-minute production, or both.",
            evidence: evidenceLine("MPG", mpg[0].toFixed(1), mpg[1].toFixed(1)),
          }
        : {
            title: "Playing time is similar",
            text: "Per-game comparisons rest on similar minutes.",
            evidence: evidenceLine("MPG", mpg[0].toFixed(1), mpg[1].toFixed(1)),
          },
    );
  }

  return {
    ...evidence,
    claimEvidence:prepareClaimEvidence(payload),
    claimPayload:payload,
    impact: (Array.isArray(evidence.impact) ? evidence.impact : []).filter(
      (m) => Number.isFinite(m.left) && Number.isFinite(m.right),
    ),
    sections,
    profiles,
    names,
    tags,
    surnames,
    scopeLabel: scope,
    verdict,
    areas,
    insights: takeaways.slice(0, 3),
  };
}

export function comparisonAnswer(view) {
  const areas = view.areas
    .map(
      (a) => {
        const columns = availableColumns([0, 1], a.rows.map(r => r.cells.map(c => c.text)));
        return `<section class="result-area" aria-labelledby="area-${a.key}"><div class="area-head"><img src="/static/icons/${a.icon}.svg" alt="" /><h3 id="area-${a.key}">${esc(a.title)}</h3></div><p class="area-text">${esc(a.text)}</p><table class="area-numbers"><caption class="sr-only">${esc(a.title)} numbers</caption><thead><tr><td></td>${columns.map(i => `<th scope="col">${esc(view.tags[i])}</th>`).join("")}</tr></thead><tbody>${a.rows.map((r) => `<tr><th scope="row">${esc(r.label)}</th>${columns.map(i => { const c = r.cells[i]; return `<td class="${c.strong ? "is-leader" : c.muted ? "is-muted" : ""}">${esc(c.text)}</td>`; }).join("")}</tr>`).join("")}</tbody></table></section>`;
      },
    )
    .join("");
  const takeaways = view.insights.length
    ? `<section class="result-takeaways" aria-label="Key takeaways"><h3 class="result-label">Key takeaways</h3><div class="takeaway-grid">${view.insights.map((i) => `<article class="takeaway-card"><h4>${esc(i.title)}</h4><p>${esc(i.text)}</p><p class="takeaway-evidence">${esc(i.evidence)}</p></article>`).join("")}</div></section>`
    : "";
  return `<div class="result-layout"><div class="result-verdict"><p class="result-label">Comparison summary</p><p class="verdict-text">${esc(view.verdict.text)} <span class="verdict-note">${esc(view.verdict.note)}</span></p></div><p class="meta area-note">Similar rates receive equal emphasis. Small gaps and rounded ties do not support a player-quality judgment.</p><div class="result-areas">${areas}</div>${takeaways}</div>`;
}

function profileCard(profile, side) {
  const player = profile.player || {};
  const name = player.player_name || "Player";
  const initials = name.split(/\s+/).map((p) => p[0] || "").join("").slice(0, 2).toUpperCase();
  const url = /^\/players\/\d+$/.test(String(profile.profile_url || "")) ? profile.profile_url : "";
  const meta = [
    player.team_abbr,
    `${player.games_sampled || 0} GP`,
    Number.isFinite(profile.minutes_per_game) ? `${profile.minutes_per_game.toFixed(1)} MPG` : "",
    player.overall_rank ? `Rank #${player.overall_rank}` : "",
  ].filter(Boolean);
  const avatar = player.headshot_url
    ? `<img src="${esc(player.headshot_url)}" alt="" loading="lazy" onerror="this.hidden=true; this.nextElementSibling.hidden=false;" /><span class="player-avatar-fallback" hidden>${esc(initials)}</span>`
    : `<span class="player-avatar-fallback">${esc(initials)}</span>`;
  return `<article class="compare-card compare-card-${side}"><div class="player-avatar" aria-hidden="true">${avatar}</div><div class="compare-card-copy"><h3>${esc(name)}</h3><p class="meta">${meta.map(esc).join(" · ")}</p></div>${url ? `<a href="${esc(url)}">Profile</a>` : ""}</article>`;
}

export function comparisonProfiles(view) {
  return `<div class="compare-cards">${profileCard(view.profiles[0], "a")}<span class="compare-vs" aria-hidden="true">vs</span>${profileCard(view.profiles[1], "b")}</div><p class="meta compare-scope">${esc(view.scopeLabel)} · Data through ${esc(dateLabel(view.data_through))}</p>`;
}

// Each side keeps its numeric average and its own full-sample game distribution.
function playerCell(metric, side, name, align) {
  const m = metric[side];
  const partial = m?.missing_component_games ? `<small>Partial: ${m.valid_games}/${m.observed_games} games</small>` : "";
  return `<td class="h2h-player h2h-player-${align}"><div class="h2h-side"><div class="h2h-value"><span class="h2h-num">${num(m?.value)}${partial}</span><small class="h2h-average-label">avg</small></div>${comparisonPlot(metric, side, name)}</div></td>`;
}

export function comparisonTables(view) {
  const columns = comparisonTableColumns(view.metrics);
  const left = columns.some(k => k === 'left' || k === 'left_percentile');
  const right = columns.some(k => k === 'right' || k === 'right_percentile');
  const ranked = columns.some(k => k.endsWith('_percentile'));
  const rows = view.metrics.map((m) => `<tr>${left ? playerCell(m, "left", view.names[0], "a") : ""}<th scope="row"><span class="h2h-label">${esc(SHORT[m.key] || m.label)}</span>${comparisonStatus(m) === "Similar rates" ? `<small class="h2h-status">Similar rates</small>` : ""}</th>${right ? playerCell(m, "right", view.names[1], "b") : ""}</tr>`).join("");
  const cohort = view.metrics.length && new Set(view.metrics.map((m) => m.left.cohort_size)).size === 1 ? `${view.metrics[0].left.cohort_size} qualified players` : "qualification varies by metric";
  return `<p class="metric-comparison">${esc(view.scopeLabel)}</p><table class="h2h-table"><caption class="sr-only">Per-game head-to-head in the same dates and phases. Each box plot describes that player's recorded games. Both players use the same increasing scale for each stat.</caption><thead><tr>${left ? `<th scope="col" class="h2h-name h2h-name-a">${esc(view.names[0])}</th>` : ""}<th scope="col">Stat</th>${right ? `<th scope="col" class="h2h-name h2h-name-b">${esc(view.names[1])}</th>` : ""}</tr></thead><tbody>${rows}</tbody></table><details class="h2h-plot-key"><summary>Plot key &amp; scale</summary><p>Box: middle 50% of recorded games (Q1–Q3). Line: median. Diamond: average. Whiskers: observed minimum–maximum, not confidence intervals. Hover, focus or tap a plot for Med, Min, Max, Q1, Q3.</p><p>Each pair shares the same per-game scale, increasing left to right. Narrower boxes describe less variation in the middle half of games, not better performance. Minutes, role and opponents can affect that variation. Incomplete game samples withhold the plot.</p>${ranked ? `<p>League percentiles describe season or period averages, not individual games or overall quality: minimum ${view.min_games} complete games per metric · ${cohort}. Similar rates is a display policy; exact values remain below.</p>` : `<p>Captured appearances only; league ranks and percentiles are withheld because league coverage is incomplete. Exact values remain below.</p>`}</details>${comparisonPrecision(view)}${view.sections.length ? `<details class="comparison-details"><summary>Shooting, ball security &amp; per-36 detail</summary>${comparisonAdditionalTables(view)}</details>` : ""}${comparisonImpactTable(view)}`;
}

function comparisonPrecision(view) {
  const columns = comparisonPrecisionColumns(view.metrics);
  const sides = ['left', 'right'].filter(side => columns.includes(side));
  const caption = columns.includes('gap') ? 'Recorded rates to three decimals; gaps use unrounded values' : 'Recorded rates to three decimals';
  return `<details class="comparison-precision"><summary>More precision &amp; comparison rules</summary><p>Values above are rounded to one decimal. Rates are marked similar when they round to the same value, or their gap is at most 0.1 per game or 5% of the larger rate. This is a display policy, not a test of statistical significance. Rebounds, steals and blocks do not establish defensive ability.</p><div class="table-scroll"><table class="precision-table"><caption>${caption}</caption><thead><tr><th scope="col">Stat</th>${sides.map(side => `<th scope="col">${esc(view.names[side === 'left' ? 0 : 1])}</th>`).join('')}${columns.includes('gap') ? '<th scope="col">Absolute gap</th>' : ''}</tr></thead><tbody>${view.metrics.map((m) => `<tr><th scope="row">${esc(SHORT[m.key] || m.label)}</th>${sides.map(side => `<td data-side="${side}">${usable(m[side]) ? num(m[side].value, 3) : "Unavailable"}</td>`).join('')}${columns.includes('gap') ? `<td class="precision-gap">${finite(m.delta) && usable(m.left) && usable(m.right) ? num(Math.abs(m.delta), 3) : "Unavailable"}</td>` : ''}</tr>`).join("")}</tbody></table></div></details>`;
}

function comparisonAdditionalTables(view) {
  return view.sections.map((section, sectionIndex) => {
    const columns = comparisonDetailColumns(section.metrics);
    const sides = ['left', 'right'].filter(side => columns.includes(side));
    return `<section data-comparison-section-index="${sectionIndex}" class="comparison-additional" data-comparison-section="${esc(section.key)}"><h2>${esc(section.title)}</h2><p class="meta">${esc(section.description)}</p><div class="table-scroll comparison-scroll" tabindex="0" role="region" aria-label="${esc(section.title)} comparison"><table class="overview-table comparison-detail-table"><caption class="sr-only">${esc(section.title)} in the same dates and phases</caption><thead><tr><th scope="col">Metric</th>${sides.map(side => `<th scope="col">${esc(view.names[side === 'left' ? 0 : 1])}</th>`).join('')}${columns.includes('comparison') ? '<th scope="col">Comparison</th>' : ''}</tr></thead><tbody>${section.metrics.map((m) => `<tr><th scope="row">${esc(m.label)}<small>${esc(m.unit)}</small></th>${sides.map(key => { const side = m[key]; return `<td data-side="${key}">${num(side.value)}${m.unit === "percent" && Number.isFinite(side.value) ? "%" : ""}<small>${side.valid_games} of ${side.observed_games} games${side.missing_component_games ? " · partial inputs" : ""}</small>${m.unit === "percent" ? `<small>${Object.entries(side.components || {}).filter(([key]) => ["fga", "fg3a", "fta"].includes(key)).map(([key, value]) => `${num(value, 0)} ${key === "fg3a" ? "3PA" : key.toUpperCase()}`).join(" · ")}</small>` : ""}</td>`; }).join('')}${columns.includes('comparison') ? `<td>${esc(m.comparison)}</td>` : ''}</tr>`).join("")}</tbody></table></div></section>`;
  }).join('');
}

function comparisonImpactTable(view) {
  if (!view.impact.length) return "";
  return `<section class="comparison-impact"><h2>Impact on winning</h2><div class="table-scroll comparison-scroll" tabindex="0" role="region" aria-label="Winning impact comparison"><table class="overview-table impact-table"><thead><tr><th scope="col">Metric</th><th scope="col">${esc(view.names[0])}</th><th scope="col">${esc(view.names[1])}</th><th scope="col">How to read it</th></tr></thead><tbody>${view.impact.map((m) => `<tr><th scope="row">${esc(m.label)}</th><td>${num(m.left, m.key === "ws48" ? 3 : 1)}</td><td>${num(m.right, m.key === "ws48" ? 3 : 1)}</td><td>${esc(m.definition)}</td></tr>`).join("")}</tbody></table></div></section>`;
}
