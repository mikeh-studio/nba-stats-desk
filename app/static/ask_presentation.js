// Governed values, explicit display policy and server-authored balanced takeaways.
const finite = (value) => typeof value === 'number' && Number.isFinite(value);
const escape = (value) => String(value ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
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
  return {paragraphs, insights, metrics:visible, allMetrics:metrics,
    additionalMetrics:Array.isArray(keys) ? metrics.filter(m=>!keys.includes(m.key)) : [],
    range:`${dateLabel(scope.start)} – ${dateLabel(scope.end)}`,
    comparison:`${(scope.previous_phases || scope.phases).join(' + ')} · ${dateLabel(scope.previous_start)} – ${dateLabel(scope.previous_end)}`,
    scope, policy, minGames:evidence.min_games || 5,
    preferredMetric:policy?.preferred_metric || 'pts',
    followups:[`Show ${name}'s points from ${scope.start} through ${scope.end} (${scope.phases.join(' + ')}).`, `Show ${name}'s performance from ${scope.start} through ${scope.end}, regular season.`],
  };
}
export function overviewContributions(overview) {
  if (!overview.insights.length) return '';
  return `<aside class="takeaways compact-takeaways" aria-label="Notable contributions and limitations"><h2>Contributions</h2>${overview.insights.map(i => `<details class="compact-contribution"><summary>${escape(i.title)}</summary><p>${escape(i.text)}</p></details>`).join('')}</aside>`;
}

export function overviewMetricsTable(overview) {
  const unavailable = (label = 'Unavailable') => `<span aria-label="${escape(label)}" title="${escape(label)}">—</span>`;
  const number = n => finite(n) ? (Math.abs(n) < 0.05 ? '0.0' : n.toFixed(1)) : null;
  const row = m => {
    const unit = m.unit || 'per game';
    const value = number(m.value);
    const suffix = unit === 'percent' ? '%' : unit === 'minutes per game' ? ' min' : unit === 'per 36 minutes' ? ' /36' : '';
    const change = m.sample ? m.display_change : m.change;
    const changeUnit = m.change_unit || unit;
    const changeSuffix = changeUnit === 'percentage points' ? ' pp' : changeUnit === 'minutes per game' ? ' min' : changeUnit === 'per 36 minutes' ? ' /36' : '';
    const delta = number(change);
    const limited = m.sample?.status === 'limited';
    return `<tr data-metric-key="${escape(m.key)}"><th scope="row">${escape(m.label)}</th><td aria-label="${escape(value === null ? 'Unavailable' : `${value} ${unit}`)}">${value === null ? unavailable() : value + suffix}${limited ? ' <span class="metric-status">Limited</span>' : ''}</td><td>${finite(m.percentile) && !limited ? `<div class="percentile-cell"><meter min="0" max="100" value="${Math.round(Math.max(0, Math.min(100, m.percentile)))}" aria-label="${escape(m.label)} league percentile">${ordinal(m.percentile)}</meter><span>${ordinal(m.percentile)}</span></div>` : unavailable(finite(m.value) ? 'Not ranked' : 'Unavailable')}</td><td aria-label="${escape(delta === null ? 'Comparison unavailable' : `${delta} ${changeUnit}`)}">${delta === null ? unavailable('Comparison unavailable') : `${change >= 0.05 ? '+' : ''}${delta}${changeSuffix}`}</td></tr>`;
  };
  const table = (metrics, label) => `<div class="table-scroll"><table class="overview-table compact-metrics"><caption class="sr-only">${escape(label)}</caption><thead><tr><th scope="col">Stat</th><th scope="col">Average</th><th scope="col">Percentile</th><th scope="col">Change</th></tr></thead><tbody>${metrics.map(row).join('')}</tbody></table></div>`;
  const sampleRows = overview.allMetrics.map(m => `<tr><th scope="row">${escape(m.label)}</th><td>${escape(m.sample?.text || `${m.valid_games ?? 'Unknown'}/${m.observed_games ?? 'Unknown'} valid appearances`)}</td></tr>`).join('');
  return `<p class="metric-comparison">Versus ${escape(overview.comparison)}</p>${table(overview.metrics, 'Key metrics')}<p class="metric-footnote compact-legend">Per-game averages · percentages shown as % · pp = percentage points.</p><div class="metric-details"><details data-sample-details><summary>Samples &amp; methodology</summary>${overview.policy ? `<p class="meta">${escape(overview.policy.scope_note)}</p><p class="meta">${escape(overview.policy.limitation)}</p>` : ''}<div class="table-scroll"><table class="overview-table sample-table"><caption class="sr-only">Sample / opportunity</caption><thead><tr><th scope="col">Stat</th><th scope="col">Sample / opportunity</th></tr></thead><tbody>${sampleRows}</tbody></table></div><p class="meta">Percentiles use complete eligible observations in the same dates and phases. Shooting ratios have no inferred ranking threshold. Rare-event counts describe observed events, not a defensive grade.</p></details>${overview.additionalMetrics.length ? `<details data-additional-metrics><summary>View all metrics (${overview.additionalMetrics.length} additional)</summary>${table(overview.additionalMetrics, 'Additional metrics')}</details>` : ''}</div>`;
}
