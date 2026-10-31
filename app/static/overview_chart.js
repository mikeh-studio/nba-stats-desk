// Render descriptive game distributions from server summaries, never league ranks.
const finite = Number.isFinite;
const esc = v => String(v ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
export const boxValue = v => finite(v) ? v.toFixed(1) : 'Unavailable';
export function boxplotContext(metric) {
  const games = metric?.distribution?.across_games, summary = games?.summary;
  if (!summary || summary.n < 1 || !['min','q1','median','q3','max'].every(k => finite(summary[k]))) return null;
  const points = (games.points || []).map((point,index) => ({...point,index})).filter(p => finite(p.value) && Number.isFinite(Date.parse(p.date))).slice(-40);
  if (!points.length) return null;
  const lo = summary.min === summary.max ? summary.min - .5 : summary.min;
  const hi = summary.min === summary.max ? summary.max + .5 : summary.max;
  return {games,summary,points,lo,hi};
}
export const boxX = (value, c) => 28 + (value-c.lo)/(c.hi-c.lo)*444;
export const boxPointLabel = p => `${p.date}${p.opponent ? ' vs '+p.opponent : ''}: ${boxValue(p.value)}`;
export function boxSample(c) {
  const observed = finite(c.games.observed_games) ? c.games.observed_games : c.summary.n;
  return `${c.summary.n} of ${observed} appearances${c.summary.n > c.points.length ? ` · latest ${c.points.length} game values plotted; box summarizes all valid appearances` : ''}`;
}
export function overviewBoxplot(metric) {
  const c = boxplotContext(metric);
  if (!c) return '';
  const s = c.summary, x = v => boxX(v,c);
  const dots = c.points.map((p,i) => `<circle data-box-point="${p.index}" cx="${x(p.value)}" cy="${84+(i%5)*4}" r="3" tabindex="0" role="img" aria-label="${esc(boxPointLabel(p))}"><title>${esc(boxPointLabel(p))}</title></circle>`).join('');
  return `<section class="overview-boxplot" aria-label="Game-to-game spread"><h3>Game-to-game spread</h3><p class="meta box-sample">${esc(boxSample(c))}</p><svg viewBox="0 0 500 112" role="img" aria-label="Box shows the middle 50% of game values; line shows the median; whiskers show minimum and maximum; dots show recorded games"><line data-box-range x1="${x(s.min)}" x2="${x(s.max)}" y1="40" y2="40"/><line data-box-min x1="${x(s.min)}" x2="${x(s.min)}" y1="26" y2="54"/><line data-box-max x1="${x(s.max)}" x2="${x(s.max)}" y1="26" y2="54"/><rect data-box-middle x="${x(s.q1)}" y="20" width="${Math.max(1,x(s.q3)-x(s.q1))}" height="40"/><line data-box-median x1="${x(s.median)}" x2="${x(s.median)}" y1="20" y2="60"/>${dots}</svg><dl class="box-values">${[['min','Minimum'],['q1','25th %'],['median','Median'],['q3','75th %'],['max','Maximum']].map(([key,label])=>`<div><dt>${label}</dt><dd data-box-stat="${key}">${boxValue(s[key])}</dd></div>`).join('')}</dl><p class="meta">Box: middle 50% · line: median · whiskers: minimum–maximum · dots: recorded games. Values use ${esc(metric.unit || 'per-game units')}.</p></section>`;
}
export function overviewTrend(chart, metric) {
  const c = boxplotContext(metric);
  if (!c || c.games.points_truncated || c.summary.n > 31) return chart;
  const points = (c.games.points || []).filter(p => finite(p.value) && Number.isFinite(Date.parse(p.date)));
  const dates = points.map(p => Date.parse(p.date));
  if (Math.max(...dates)-Math.min(...dates) >= 31*86400000) return chart;
  return {...chart, grain:'daily', series:[{...chart.series[0],points:points.map(p=>({x:p.date,y:p.value,meta:boxPointLabel(p)}))}]};
}
