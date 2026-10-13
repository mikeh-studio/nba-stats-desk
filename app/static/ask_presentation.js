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
export function overviewMetricsTable(overview) {
  const number=n=>finite(n)?n.toFixed(1):'Unavailable';
  const sampleLabel = (m) => {
    const counts = `${m.valid_games ?? 'Unknown'}/${m.observed_games ?? 'Unknown'} games`;
    if (!m.sample) return counts;
    const parts = [counts];
    if (['fg_pct','fg3_pct','ft_pct'].includes(m.key) && finite(m.numerator) && finite(m.denominator))
      parts.push(`${m.numerator}/${m.denominator} makes/attempts`);
    if (m.key === 'ts_pct' && m.components)
      parts.push(`${m.components.fga ?? 'Unknown'} FGA · ${m.components.fta ?? 'Unknown'} FTA`);
    if (m.sample.status === 'limited') parts.push(`Limited${m.missing_component_games ? ` · ${m.missing_component_games} missing` : ''}`);
    if (m.sample.status === 'unavailable') parts.push('Unavailable');
    return parts.join(' · ');
  };
  const table=(metrics,label)=>`<div class="table-scroll"><table class="overview-table"><caption class="sr-only">${escape(label)}</caption><thead><tr><th scope="col">Stat</th><th scope="col">Value / unit</th><th scope="col">League percentile</th><th scope="col">Change</th><th scope="col">Sample / opportunity</th></tr></thead><tbody>${metrics.map(m=>`<tr data-metric-key="${escape(m.key)}"><th scope="row">${escape(m.label)}</th><td>${number(m.value)}<br><small>${escape(m.unit || 'per game')}</small></td><td>${finite(m.percentile) && m.sample?.status !== 'limited' ? `<div class="percentile-cell"><meter min="0" max="100" value="${Math.round(Math.max(0,Math.min(100,m.percentile)))}" aria-label="${escape(m.label)} league percentile">${ordinal(m.percentile)}</meter><span>${ordinal(m.percentile)}</span></div>` : 'Unavailable'}</td><td>${finite(m.sample ? m.display_change : m.change) ? `${(m.sample ? m.display_change : m.change)>0?'+':''}${number(m.sample ? m.display_change : m.change)} ${escape(m.change_unit || m.unit || 'per game')}` : 'Unavailable'}</td><td title="${escape(m.sample?.text || '')}"><small>${escape(sampleLabel(m))}</small></td></tr>`).join('')}</tbody></table></div>`;
  return `<p class="metric-comparison">Versus ${escape(overview.comparison)}</p>${overview.policy ? `<p class="metric-footnote">${escape(overview.policy.scope_note)}</p><p class="meta">${escape(overview.policy.limitation)}</p>` : ''}${table(overview.metrics,'Key metrics')}${overview.additionalMetrics.length ? `<details data-additional-metrics><summary>View all metrics (${overview.additionalMetrics.length} additional)</summary>${table(overview.additionalMetrics,'Additional metrics')}</details>` : ''}<p class="metric-footnote">Percentiles use complete eligible observations in the same dates and phases. Shooting ratios have no inferred ranking threshold. Rare-event counts describe observed events, not a defensive grade.</p>`;
}
