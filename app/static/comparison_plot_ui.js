import {comparisonDistribution, boxPosition, meanDiamond, distributionNumber} from "./comparison_distribution.js?v=20261030-unified-checkpoint-v1";

const esc = (v) => String(v ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
const ordinal = (v) => {
  const n = Math.round(v), suffix = n % 100 >= 11 && n % 100 <= 13 ? "th" : ({1:"st",2:"nd",3:"rd"}[n % 10] || "th");
  return `${n}${suffix}`;
};

export function comparisonPlot(metric, side, name) {
  const m = metric[side], scale = comparisonDistribution(metric), s = scale[side];
  const rank = Number.isFinite(m.percentile)
    ? `<button type="button" class="h2h-pct" data-h2h-tip="rank">${ordinal(m.percentile)} percentile</button><div class="h2h-tooltip h2h-rank-tooltip" popover="manual" role="tooltip">League standing by per-game average among qualified players in the same dates and phases.</div>`
    : "";
  if (!s) return `<div class="h2h-distribution">${rank}<span class="h2h-unavailable">${m.missing_component_games ? "Incomplete data" : "Distribution unavailable"}</span></div>`;
  const x = (v) => boxPosition(v, scale), label = `${name} ${metric.label}: game-by-game distribution over ${s.n} recorded games`;
  return `<div class="h2h-distribution">${rank}<button type="button" class="h2h-plot-trigger" data-h2h-tip="plot" aria-label="${esc(label)}"><svg class="h2h-boxplot" viewBox="0 0 100 24" preserveAspectRatio="none" aria-hidden="true"><line class="h2h-whisker" x1="${x(s.min)}" x2="${x(s.max)}" y1="12" y2="12"/><line class="h2h-min" x1="${x(s.min)}" x2="${x(s.min)}" y1="7" y2="17"/><line class="h2h-max" x1="${x(s.max)}" x2="${x(s.max)}" y1="7" y2="17"/><rect class="h2h-box" x="${x(s.q1)}" y="6" width="${Number((x(s.q3)-x(s.q1)).toFixed(3))}" height="12"/><line class="h2h-median" x1="${x(s.median)}" x2="${x(s.median)}" y1="6" y2="18"/><polygon class="h2h-mean" points="${meanDiamond(m.value, scale)}"/></svg><span class="h2h-axis" aria-hidden="true"><span class="h2h-axis-min">${distributionNumber(scale.lo)}</span><span class="h2h-axis-max">${distributionNumber(scale.hi)}</span></span></button><div class="h2h-tooltip h2h-plot-tooltip" popover="manual" role="tooltip"><dl>${[["median","Med"],["min","Min"],["max","Max"],["q1","Q1"],["q3","Q3"]].map(([key,label])=>`<dt>${label}</dt><dd data-summary="${key}">${distributionNumber(s[key])}</dd>`).join("")}</dl></div></div>`;
}

let tooltipId = 0;
export function bindComparisonPlots(root) {
  root.h2hTooltipController?.abort();
  const controller = new AbortController(), options = {signal: controller.signal};
  root.h2hTooltipController = controller;
  let current = null;
  const hide = () => {
    if (current?.tip.matches(":popover-open")) current.tip.hidePopover();
    if (current) current.pinned = false;
    current = null;
  };
  root.querySelectorAll("[data-h2h-tip]").forEach((button) => {
    const tip = button.nextElementSibling;
    tip.id = `h2h-tooltip-${++tooltipId}`;
    button.setAttribute("aria-describedby", tip.id);
    const item = {button, tip, pinned: false};
    const show = () => {
      if (current !== item) hide();
      current = item;
      if (!tip.matches(":popover-open")) tip.showPopover();
      const rect = button.getBoundingClientRect(), size = tip.getBoundingClientRect();
      tip.style.left = `${Math.max(8, Math.min(innerWidth - size.width - 8, rect.left + (rect.width-size.width)/2))}px`;
      tip.style.top = `${Math.max(8, rect.bottom + size.height + 8 < innerHeight ? rect.bottom + 5 : rect.top - size.height - 5)}px`;
    };
    item.reposition = show;
    button.addEventListener("pointerenter", (e) => { if (e.pointerType !== "touch") show(); }, options);
    button.addEventListener("pointerleave", () => { if (current === item && !item.pinned && document.activeElement !== button) hide(); }, options);
    button.addEventListener("focus", show, options);
    button.addEventListener("blur", () => { if (current === item) hide(); }, options);
    button.addEventListener("click", () => { if (item.pinned) hide(); else { show(); item.pinned = true; } }, options);
    button.addEventListener("keydown", (e) => { if (e.key === "Escape" && current === item) { hide(); e.stopPropagation(); } }, options);
  });
  window.addEventListener("resize", hide, options);
  window.addEventListener("scroll", () => {
    if (!current) return;
    const rect = current.button.getBoundingClientRect();
    if (rect.bottom < 0 || rect.top > innerHeight) hide();
    else current.reposition();
  }, {...options, capture:true});
  controller.signal.addEventListener("abort", hide, {once:true});
}
