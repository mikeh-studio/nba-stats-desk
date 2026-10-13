// Governed values, explicit display policy and server-authored balanced takeaways.
const finite = (value) => typeof value === 'number' && Number.isFinite(value);
const escape = (value) => String(value ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
const metricNames = {
  pts:['PTS','Points'], reb:['REB','Rebounds'], ast:['AST','Assists'],
  stl:['STL','Steals'], blk:['BLK','Blocks'], tov:['TOV','Turnovers'],
  min:['MIN','Minutes'], fg3m:['3PM','Three-pointers made'], fg3a:['3PA','Three-point attempts'],
  fg3_pct:['3P%','Three-point percentage'], fga:['FGA','Field-goal attempts'],
  fta:['FTA','Free-throw attempts'], fg_pct:['FG%','Field-goal percentage'],
  ft_pct:['FT%','Free-throw percentage'], efg_pct:['eFG%','Effective field-goal percentage'],
  ts_pct:['TS%','True shooting percentage'], pts_per36:['PTS/36','Points per 36 minutes'],
  ast_per36:['AST/36','Assists per 36 minutes'],
};
export function metricAbbreviation(metric) { return metricNames[metric.key]?.[0] || metric.label || metric.key; }
export function changeClass(metric, change) {
  if (!finite(change) || Math.abs(change) < 0.05 || metric.key === 'min') return 'change-neutral';
  const positive = metric.key === 'tov' ? change < 0 : change > 0;
  return positive ? 'change-up' : 'change-down';
}
function metricLabel(metric, id) {
  const [short, full] = metricNames[metric.key] || [metric.label, metric.label];
  if (metric.key !== 'ts_pct') return `<abbr title="${escape(full)}" aria-label="${escape(short + ': ' + full)}">${escape(short)}</abbr>`;
  return `<button type="button" class="metric-help" data-metric-help aria-label="TS%: True shooting percentage calculation" aria-describedby="${id}" aria-expanded="false">TS%</button><span id="${id}" class="metric-tooltip" role="tooltip" popover="manual"><strong>True shooting percentage</strong><br>100 × SUM(PTS) ÷ [2 × (SUM(FGA) + 0.44 × SUM(FTA))]<br>Measures scoring efficiency across field goals and free throws. Uses summed components from valid games; 0.44 estimates free-throw possessions.</span>`;
}
let helpId = 0;
let activeHelp = null;
let positionHelp = null;
let helpListenersBound = false;
export function bindMetricHelp(root) {
  const buttons = root.querySelectorAll('[data-metric-help]');
  if (!buttons.length) return;
  if (!helpListenersBound) {
    window.addEventListener('scroll', () => positionHelp?.(), {passive:true, capture:true});
    window.addEventListener('resize', () => positionHelp?.(), {passive:true});
    helpListenersBound = true;
  }
  buttons.forEach(button => {
    const tip = root.querySelector('#' + button.getAttribute('aria-describedby'));
    const hide = () => { tip.hidePopover(); button.setAttribute('aria-expanded','false'); delete button.dataset.helpPinned; if (activeHelp === hide) { activeHelp = null; positionHelp = null; } };
    const position = () => {
      const rect=button.getBoundingClientRect(), box=tip.getBoundingClientRect();
      if (!button.isConnected || rect.bottom < 0 || rect.top > window.innerHeight) { hide(); return; }
      tip.style.left=`${Math.max(8, Math.min(rect.left, window.innerWidth-box.width-8))}px`;
      tip.style.top=`${Math.max(8, Math.min(rect.bottom+6, window.innerHeight-box.height-8))}px`;
    };
    const show = () => {
      if (activeHelp && activeHelp !== hide) activeHelp();
      activeHelp = hide; positionHelp = position;
      tip.showPopover(); button.setAttribute('aria-expanded','true');
      position();
    };
    button.addEventListener('mouseenter',show);
    button.addEventListener('mouseleave',() => { if (document.activeElement !== button) hide(); });
    button.addEventListener('focus',show);
    button.addEventListener('blur',hide);
    button.addEventListener('click',() => { if (button.dataset.helpPinned) { delete button.dataset.helpPinned; hide(); } else { button.dataset.helpPinned='true'; show(); } });
    button.addEventListener('keydown',event => { if(event.key==='Escape') { delete button.dataset.helpPinned; hide(); } });
  });
}
export function dateLabel(value) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value || '')) return String(value || '');
  return new Intl.DateTimeFormat('en-US', {month:'short',day:'numeric',year:'numeric',timeZone:'UTC'}).format(new Date(`${value}T00:00:00Z`));
}
export function ordinal(value) {
  const n=Math.round(value), last=n%100;
  return `${n}${last>=11 && last<=13 ? 'th' : {1:'st',2:'nd',3:'rd'}[n%10] || 'th'}`;
}
export function overviewPresentation(payload) {
  const evidence=payload?.semantic_evidence;
  if (!Array.isArray(evidence?.metrics) || !evidence.scope?.start) return null;
  const metrics=evidence.metrics, scope=evidence.scope;
  const policy=evidence.presentation;
  const supplied=evidence.summary_paragraphs;
  const canonical=Array.isArray(supplied) && supplied.length && supplied.every(p=>typeof p==='string');
  const name=payload.player_profile?.player?.player_name || 'The player';
  const pts=metrics.find(m=>m.key==='pts' && finite(m.value) && !m.missing_component_games);
  const paragraphs=canonical ? supplied : [pts ? `${name} averaged ${pts.value.toFixed(1)} points per game in the requested period.` : 'Complete metric data is unavailable for an overall assessment.', 'Saved answer: ask again for the current metric selection and sample assessment.'];
  // Saved results retain their own scope and metrics. No browser-created standout ranking.
  const keys=policy?.primary_keys;
  const visible=Array.isArray(keys) ? keys.map(k=>metrics.find(m=>m.key===k)).filter(Boolean) : metrics;
  const insights=Array.isArray(policy?.takeaways) ? policy.takeaways : [{area:'Sample',icon:'chart-bar',title:'Recorded observations',text:`${payload.player_profile?.player?.games_sampled || 0} recorded appearances. See each metric's evidence; box scores do not establish overall defensive impact.`}];
  return {paragraphs, insights, playerName:name, metrics:visible, allMetrics:metrics,
    additionalMetrics:Array.isArray(keys) ? metrics.filter(m=>!keys.includes(m.key)) : [],
    range:`${dateLabel(scope.start)} – ${dateLabel(scope.end)}`,
    comparison:`${(scope.previous_phases || scope.phases).join(' + ')} · ${dateLabel(scope.previous_start)} – ${dateLabel(scope.previous_end)}`,
    scope, policy, minGames:evidence.min_games || 5,
    preferredMetric:policy?.preferred_metric || 'pts',
    followups:[`Show ${name}'s points from ${scope.start} through ${scope.end} (${scope.phases.join(' + ')}).`, `Show ${name}'s performance from ${scope.start} through ${scope.end}, regular season.`],
  };
}
export function overviewTakeaways(overview) {
  if (!overview.insights.length) return '';
  const icons = new Set(['chart-bar','chart-line','shield']);
  return `<aside class="takeaways compact-takeaways" aria-label="Key takeaways"><h2>Key Takeaways</h2>${overview.insights.slice(0,3).map(i => `<section class="takeaway"><img src="/static/icons/${icons.has(i.icon) ? i.icon : 'chart-bar'}.svg" alt="" /><div><h3>${escape(i.title)}</h3><p>${escape(i.text)}</p></div></section>`).join('')}</aside>`;
}

export function overviewMetricsTable(overview) {
  const instance = ++helpId;
  const unavailable = (label = 'Unavailable') => `<span aria-label="${escape(label)}" title="${escape(label)}">—</span>`;
  const number = n => finite(n) ? (Math.abs(n) < 0.05 ? '0.0' : n.toFixed(1)) : null;
  const rank = m => {
    const limited = m.sample?.status === 'limited';
    if (finite(m.percentile) && !limited) {
      const note = m.percentile_note || `${m.label} league percentile`;
      return `<div class="percentile-cell${m.key === 'min' ? ' percentile-neutral' : ''}" title="${escape(note)}"><meter min="0" max="100" value="${Math.round(Math.max(0, Math.min(100, m.percentile)))}" aria-label="${escape(note)}">${ordinal(m.percentile)}</meter><span>${ordinal(m.percentile)}</span></div>`;
    }
    if (m.percentile_status) return `<span class="rank-status"${m.distribution ? '' : ' tabindex="0"'} title="${escape((m.percentile_label || 'Not ranked') + '. ' + (m.percentile_note || ''))}" aria-label="${escape((m.percentile_label || 'Not ranked') + '. ' + (m.percentile_note || ''))}">${escape(m.percentile_label || 'Not ranked')}</span>`;
    return unavailable(finite(m.value) ? 'Not ranked' : 'Unavailable');
  };
  const row = (m, index, group) => {
    const unit = m.unit || 'per game';
    const value = number(m.value);
    const suffix = unit === 'percent' ? '%' : unit === 'minutes per game' ? ' min' : unit === 'per 36 minutes' ? ' /36' : '';
    const change = m.sample ? m.display_change : m.change;
    const changeUnit = m.change_unit || unit;
    const changeSuffix = changeUnit === 'percentage points' ? ' pp' : changeUnit === 'minutes per game' ? ' min' : changeUnit === 'per 36 minutes' ? ' /36' : '';
    const delta = number(change);
    const limited = m.sample?.status === 'limited';
    const rankMarkup = rank(m);
    const rankControl = m.distribution ? `<button type="button" class="metric-distribution" data-metric-distribution="${escape(m.key)}" aria-label="${escape(metricAbbreviation(m))} distribution">${rankMarkup}</button>` : rankMarkup;
    return `<tr data-metric-key="${escape(m.key)}"><th scope="row">${metricLabel(m, `metric-help-${instance}-${group}-${index}`)}</th><td aria-label="${escape(value === null ? 'Unavailable' : `${value} ${unit}`)}">${value === null ? unavailable() : value + suffix}${limited ? ' <span class="metric-status">Limited</span>' : ''}</td><td>${rankControl}</td><td class="${changeClass(m, change)}" aria-label="${escape(delta === null ? 'Comparison unavailable' : `${delta} ${changeUnit}`)}">${delta === null ? unavailable('Comparison unavailable') : `${change >= 0.05 ? '+' : ''}${delta}${changeSuffix}`}</td></tr>`;
  };
  const table = (metrics, label) => `<div class="table-scroll"><table class="overview-table compact-metrics"><caption class="sr-only">${escape(label)}</caption><thead><tr><th scope="col">Stat</th><th scope="col">Average</th><th scope="col">Percentile</th><th scope="col">Change</th></tr></thead><tbody>${metrics.map((m,index)=>row(m,index,label.replaceAll(' ','-'))).join('')}</tbody></table></div>`;
  const rankMethodology = overview.allMetrics.some(m=>m.percentile_policy)
    ? 'Shooting ranks use provisional minimums: 100 FGA for FG%/TS%/eFG%, 50 FTA for FT%, 50 3PA for 3P%. At least five complete appearances and two qualified players are required. TOV ranks favor fewer turnovers; MIN ranks exposure. These are project rules, not NBA official qualification.'
    : 'Saved ranking policy: ask again for qualified shooting percentiles and current metric selection.';
  const sampleRows = overview.allMetrics.map(m => `<tr><th scope="row">${escape(metricAbbreviation(m))}</th><td>${escape(m.sample?.text || `${m.valid_games ?? 'Unknown'}/${m.observed_games ?? 'Unknown'} valid appearances`)}</td></tr>`).join('');
  return `<p class="metric-comparison">Versus ${escape(overview.comparison)}</p>${table(overview.metrics, 'Key metrics')}<p class="metric-footnote compact-legend">Per-game averages · pp = percentage points · TOV colors reversed; MIN neutral.</p><div class="metric-details"><details data-sample-details><summary>Samples &amp; methodology</summary>${overview.policy ? `<p class="meta">${escape(overview.policy.scope_note)}</p><p class="meta">${escape(overview.policy.limitation)}</p>` : ''}<div class="table-scroll"><table class="overview-table sample-table"><caption class="sr-only">Sample / opportunity</caption><thead><tr><th scope="col">Stat</th><th scope="col">Sample / opportunity</th></tr></thead><tbody>${sampleRows}</tbody></table></div><p class="meta">Percentiles use complete eligible observations in the same dates and phases. ${rankMethodology} Rare-event counts describe observed events, not a defensive grade.</p></details>${overview.additionalMetrics.length ? `<details data-additional-metrics><summary>View all metrics (${overview.additionalMetrics.length} additional)</summary>${table(overview.additionalMetrics, 'Additional metrics')}</details>` : ''}</div>`;
}
