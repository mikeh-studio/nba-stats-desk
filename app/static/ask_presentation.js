import {prepareClaimEvidence} from "./ask_claim_bindings.js?v=20261028-comparison-distributions-v1";
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
  return {claimEvidence:prepareClaimEvidence(payload), claimPayload:payload, paragraphs, summaryContext:evidence.summary_context || "", insights, playerName:name, metrics:visible, allMetrics:metrics,
    gameVariation:evidence.insights?.find(f=>f.game_variation)?.game_variation || null,
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
  return `<section class="result-takeaways" aria-label="Key takeaways"><h2 class="result-label">Key takeaways</h2><div class="takeaway-grid">${overview.insights.slice(0,3).map(i => `<article class="takeaway-card has-icon"><img src="/static/icons/${icons.has(i.icon) ? i.icon : 'chart-bar'}.svg" alt="" /><div><h3>${escape(i.title)}</h3><p>${escape(i.text)}</p></div></article>`).join('')}</div></section>`;
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
    const rankName = finite(m.percentile) && !limited ? `${ordinal(m.percentile)} percentile` : m.percentile_label || 'Not ranked';
    const rankControl = m.distribution ? `<button type="button" class="metric-distribution" data-metric-distribution="${escape(m.key)}" aria-label="${escape(metricAbbreviation(m))} distribution: ${escape(rankName)}" aria-description="${escape(m.percentile_note || '')}">${rankMarkup}</button>` : rankMarkup;
    return `<tr data-metric-key="${escape(m.key)}"><th scope="row">${metricLabel(m, `metric-help-${instance}-${group}-${index}`)}</th><td aria-label="${escape(value === null ? 'Unavailable' : `${value} ${unit}`)}">${value === null ? unavailable() : value + suffix}${limited ? ' <span class="metric-status">Limited</span>' : ''}</td><td>${rankControl}</td><td class="${changeClass(m, change)}" aria-label="${escape(delta === null ? 'Comparison unavailable' : `${delta} ${changeUnit}`)}">${delta === null ? unavailable('Comparison unavailable') : `${change >= 0.05 ? '+' : ''}${delta}${changeSuffix}`}</td></tr>`;
  };
  const table = (metrics, label) => `<div class="table-scroll"><table class="overview-table compact-metrics"><caption class="sr-only">${escape(label)}</caption><thead><tr><th scope="col">Stat</th><th scope="col">Average</th><th scope="col" aria-label="League percentile">League %</th><th scope="col">Change</th></tr></thead><tbody>${metrics.map((m,index)=>row(m,index,label.replaceAll(' ','-'))).join('')}</tbody></table></div>`;
  const rankMethodology = overview.allMetrics.some(m=>m.percentile_policy)
    ? 'Shooting ranks use provisional minimums: 100 FGA for FG%/TS%/eFG%, 50 FTA for FT%, 50 3PA for 3P%. At least five complete appearances and two qualified players are required. TOV ranks favor fewer turnovers; MIN ranks exposure. These are project rules, not NBA official qualification.'
    : 'Saved ranking policy: ask again for qualified shooting percentiles and current metric selection.';
  const sampleRows = overview.allMetrics.map(m => `<tr><th scope="row">${escape(metricAbbreviation(m))}</th><td>${escape(m.sample?.text || `${m.valid_games ?? 'Unknown'}/${m.observed_games ?? 'Unknown'} valid appearances`)}</td></tr>`).join('');
  return `<p class="metric-comparison">Versus ${escape(overview.comparison)}</p>${table(overview.metrics, 'Key metrics')}${peerComparisonDetails(overview)}${gameVariationDetails(overview)}<p class="metric-footnote compact-legend">Per-game averages · pp = percentage points · TOV colors reversed; MIN neutral.</p><div class="metric-details"><details data-sample-details><summary>Samples &amp; methodology</summary>${overview.policy ? `<p class="meta">${escape(overview.policy.scope_note)}</p><p class="meta">${escape(overview.policy.limitation)}</p>` : ''}<div class="table-scroll"><table class="overview-table sample-table"><caption class="sr-only">Sample / opportunity</caption><thead><tr><th scope="col">Stat</th><th scope="col">Sample / opportunity</th></tr></thead><tbody>${sampleRows}</tbody></table></div><p class="meta">Percentiles use complete eligible observations in the same dates and phases. ${rankMethodology}</p></details>${overview.additionalMetrics.length ? `<details data-additional-metrics><summary>View all metrics (${overview.additionalMetrics.length} additional)</summary>${table(overview.additionalMetrics, 'Additional metrics')}</details>` : ''}</div>`;
}

export function peerComparisonDetails(overview) {
  const metrics=overview.allMetrics.filter(m=>m.peer_comparison?.mode && m.peer_comparison.mode !== 'withheld');
  if (!metrics.length) return '';
  return `<details data-peer-details><summary>Overview peer comparisons</summary><p class="meta">Table percentiles remain league-wide. These distributions use the reference named for each Overview comparison.</p>${metrics.map((m, peerIndex)=>{
    const p=m.peer_comparison, bins=p.distribution?.among_players?.bins || [], max=Math.max(1,...bins.map(b=>b.count));
    const group=p.kind==='position' ? ({Guard:'Guards',Wing:'Wings/forwards',Big:'Bigs/centers'}[p.group] || p.group) : 'League-wide';
    const standing=p.mode==='ordinal' ? `${p.tied?'Tied ':''}${ordinal(p.rank)} of ${p.cohort_size}` : `${p.percentile.toFixed(1)}% of other qualifiers below this value`;
    const bars=bins.map((b,i)=>`<rect x="${i*240/bins.length}" y="${60-55*b.count/max}" width="${Math.max(1,240/bins.length-1)}" height="${55*b.count/max}" fill="currentColor"><title>${escape(`${b.start.toFixed(1)} to ${b.end.toFixed(1)}: ${b.count} players`)}</title></rect>`).join('');
    return `<section data-peer-index="${peerIndex}" class="peer-comparison"><h4>${escape(metricAbbreviation(m))} · ${escape(group)}</h4><p>${escape(standing)} · ${escape(p.cohort_size)} qualifiers · ${escape(p.season || '')} ${escape(p.phases.join(' + '))}</p><p class="meta">${escape(p.start)} through ${escape(p.end)} · at least ${escape(p.qualification.min_games)} complete appearances${Number.isFinite(p.classified_players) ? ` · ${escape(p.classified_players)}/${escape(p.eligible_players)} eligible players classified` : ''}</p><svg role="img" aria-label="${escape(metricAbbreviation(m)+' '+group+' distribution, '+p.cohort_size+' qualifying players')}" viewBox="0 0 240 64" width="240" height="64">${bars}</svg></section>`;
  }).join('')}<p class="meta">${metrics.some(m=>m.peer_comparison.kind==='position') ? 'Historical listed positions: G = Guard, F = Wing/forward, C = Big/center. Hybrids belong to both listed groups; each player counts once per comparison. Mapping: position_groups/2. ' : ''}At least five complete appearances and 10 qualifiers; percentile prose requires 20. Project qualification rules.</p></details>`;
}


export function gameVariationDetails(overview) {
  const v=overview.gameVariation;
  if (!v) return '';
  const m=overview.allMetrics.find(m=>m.key===v.metric_key);
  const games=m?.distribution?.across_games, stats=v.summary;
  if (!games?.points?.length || !stats || ![stats.min,stats.max,stats.median,stats.q1,stats.q3].every(finite)) return '';
  const points=games.points.filter(p=>finite(p.value) && Number.isFinite(Date.parse(p.date)));
  if (!points.length) return '';
  const times=points.map(p=>Date.parse(p.date)), low=Math.min(...times), high=Math.max(...times);
  const x=t=>28+440*(t-low)/(high-low || 1), y=value=>112-90*(value-stats.min)/(stats.max-stats.min || 1);
  const label=metricAbbreviation(m);
  const dots=points.map(p=>`<circle cx="${x(Date.parse(p.date))}" cy="${y(p.value)}" r="${p.value===stats.max ? 4 : 2.5}" fill="${p.value===stats.max ? '#e7b45b' : 'currentColor'}"><title>${escape(`${p.date}${p.opponent?' vs '+p.opponent:''}: ${p.value} ${label}`)}</title></circle>`).join('');
  return `<details data-game-variation><summary>${escape(label)} game-by-game spread</summary><p>${escape(v.spread_text)} ${escape(v.peak_text)}</p><svg style="width:100%;max-width:600px;height:auto" viewBox="0 0 500 150" role="img" aria-label="${escape(label+' by game date; shaded band is the middle 50%, dashed line is the median, gold dots mark highs')}"><rect x="28" y="${y(stats.q3)}" width="440" height="${Math.max(1,y(stats.q1)-y(stats.q3))}" fill="currentColor" opacity="0.12"/><line x1="28" x2="468" y1="${y(stats.median)}" y2="${y(stats.median)}" stroke="currentColor" stroke-dasharray="4 4"/>${dots}<text x="28" y="138" fill="currentColor" font-size="16">${escape(points[0].date)}</text><text x="468" y="138" text-anchor="end" fill="currentColor" font-size="16">${escape(points.at(-1).date)}</text><text x="4" y="24" fill="currentColor" font-size="14">${escape(stats.max)}</text><text x="4" y="114" fill="currentColor" font-size="14">${escape(stats.min)}</text></svg><p class="meta">${escape(stats.n)} complete appearances · one dot per game · shaded middle 50% · dashed median · gold highs${games.points_truncated ? ' · latest '+games.displayed_games+' games plotted; summaries and high use the full sample' : ''}</p></details>`;
}
