// Render server-owned distributions; no client-side statistical estimates.
const esc = v => String(v ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
const ordinal = n => {const v=Math.round(n), rem=v%100; return v + (rem>=11 && rem<=13 ? 'th' : ({1:'st',2:'nd',3:'rd'})[v%10] || 'th');};
const count = n => Number.isFinite(n) && n >= 0 ? n : 0;
const finite = n => typeof n === 'number' && Number.isFinite(n);
const fmt = (n, unit) => finite(n) ? n.toFixed(1) + (unit === 'percent' ? '%' : '') : 'Unavailable';
const opportunity = (point, metric) => metric.key === 'ts_pct' && point.components && finite(point.components.fga) && finite(point.components.fta)
  ? `${point.components.fga} FGA, ${point.components.fta} FTA`
  : finite(point.attempts) ? point.attempts + ' ' + (({fga:'FGA',fta:'FTA',fg3a:'3PA'})[point.attempt_field] || point.attempt_field || 'attempts') : '';
let dialog, trigger;

export function distributionChart(metric, view) {
  const data = metric.distribution?.[view], summary = data?.summary;
  if (!finite(summary?.n) || summary.n <= 0) return '<p class="drawer-empty">No ' + (view === 'among_players' ? 'qualified players' : 'valid game observations') + ' in this period.</p>';
  const bins = (data.bins || []).filter(b=>[b.start,b.end,b.count].every(finite) && b.count>=0), marker = view === 'among_players' ? metric.value : summary.median;
  let lo = Math.min(bins[0]?.start ?? summary.min, finite(marker) ? marker : summary.min);
  let hi = Math.max(bins.at(-1)?.end ?? summary.max, finite(marker) ? marker : summary.max);
  if (hi === lo) { lo -= .5; hi += .5; }
  const x = n => 24 + (n-lo)/(hi-lo)*312;
  const max = Math.max(1,...bins.map(b=>b.count));
  const bars = bins.map(b=>`<rect x="${x(b.start)}" y="${174-b.count/max*126}" width="${Math.max(1,x(b.end)-x(b.start)-2)}" height="${b.count/max*126}" fill="#626975"><title>${esc(fmt(b.start,metric.unit))} to ${esc(fmt(b.end,metric.unit))}: ${b.count} ${view === 'among_players' ? 'qualified players' : 'valid games'}</title></rect>`).join('');
  const points = view === 'across_games' ? (data.points || []).filter(p=>finite(p.value)).map((p,i)=>`<circle cx="${x(p.value)}" cy="${183+(i%3)*4}" r="2.5" fill="#ff6253" opacity=".75" tabindex="0" role="img" data-game-point data-game-index="${i}" aria-label="${esc(p.date)}: ${esc(fmt(p.value,metric.unit))}${opportunity(p,metric) ? ', '+esc(opportunity(p,metric)) : ''}"><title>${esc(p.date)} · ${esc(fmt(p.value,metric.unit))}${opportunity(p,metric) ? ' · '+esc(opportunity(p,metric)) : ''}</title></circle>`).join('') : '';
  const band = view === 'across_games' ? `<rect x="${x(summary.q1)}" y="46" width="${Math.max(1,x(summary.q3)-x(summary.q1))}" height="106" fill="#626975" opacity=".22"/>` : '';
  const eligible = view !== 'among_players' || data.focal_in_cohort;
  const line = finite(marker) ? `<line x1="${x(marker)}" x2="${x(marker)}" y1="34" y2="178" stroke="${eligible ? '#ff6253' : '#b3b9c2'}" stroke-width="2" ${eligible ? '' : 'stroke-dasharray="5 4"'}/>` : '';
  return `<svg class="distribution-chart" viewBox="0 0 360 214" role="img" aria-label="${esc(metric.display_label || metric.label)} ${view === 'among_players' ? 'distribution of qualified player values' : 'individual game values; shaded middle 50 percent'}"><line x1="24" x2="336" y1="178" y2="178" stroke="#626975"/>${band}${bars}${points}${line}<text x="20" y="44" text-anchor="end" fill="#b3b9c2" font-size="10">${max}</text><text x="24" y="201" fill="#b3b9c2" font-size="11">${esc(fmt(lo,metric.unit))}</text><text x="336" y="201" text-anchor="end" fill="#b3b9c2" font-size="11">${esc(fmt(hi,metric.unit))}</text></svg>`;
}

export function drawerContent(metric, overview, view='among_players') {
  const data=metric.distribution?.[view], s=data?.summary || {};
  const across=view==='across_games', peers=metric.distribution?.among_players;
  const own=metric.value, note=metric.percentile_note || '';
  const policy=metric.percentile_policy || {};
  const attempts=policy.attempt_field ? metric.components?.[policy.attempt_field] : null;
  const qualification=!peers?.focal_in_cohort ? `${metric.percentile_label || 'Not qualified'}${finite(attempts) && policy.min_attempts ? ' · '+attempts+' attempts; '+policy.min_attempts+' required' : ''}. ${finite(own) && peers?.summary?.n ? 'Not included in the qualified cohort; the dashed marker shows your recorded value without a rank.' : 'The period value or qualified cohort is unavailable; no marker comparison is shown.'}` : '';
  return `<header class="drawer-header"><div><p class="followup-eyebrow">Metric detail</p><h2 id="metric-drawer-title">${esc(metric.display_label || metric.label)} · ${esc(overview.playerName)}</h2></div><button type="button" class="button subtle" data-drawer-close aria-label="Close metric details">×</button></header><p class="drawer-scope">${esc(overview.range)} · ${esc(overview.scope.phases.join(' + '))}</p><div class="drawer-views" role="group" aria-label="Distribution view"><button class="button subtle" data-distribution-view="among_players" aria-pressed="${!across}">Among players</button><button class="button subtle" data-distribution-view="across_games" aria-pressed="${across}">Across games</button></div><p class="drawer-caption">${across ? 'Bars count valid appearances; dots show recorded game values. Shading shows the middle 50%; the line marks the game median.' : `Each observation is one qualified player’s ${metric.unit === 'percent' ? 'pooled percentage' : 'per-game average'}. The line marks your player; bars count players.`}</p><div class="drawer-value"><strong>${esc(fmt(own,metric.unit))}</strong><span>${across ? 'Period '+(metric.unit==='percent' ? 'pooled percentage' : 'average') : finite(metric.percentile) ? ordinal(metric.percentile)+' percentile' : esc(metric.percentile_label || 'Not ranked')}</span></div>${distributionChart(metric,view)}${across ? `<label class="drawer-game-control">Review a game<select class="input" data-game-select aria-label="Review a game"><option value="">Choose an appearance</option>${(data?.points || []).filter(p=>finite(p.value)).map((p,i)=>`<option value="${i}">${esc(p.date)} · ${esc(fmt(p.value,metric.unit))}</option>`).join('')}</select></label><p class="drawer-point-info" role="status" data-game-point-info>Hover, tap or focus a dot for game details.</p>` : ''}<dl class="drawer-stats"><div><dt>${across ? 'Valid games' : 'Qualified players'}</dt><dd>${count(s.n)}</dd></div><div><dt>${across ? 'Game median' : 'League median'}</dt><dd>${esc(fmt(s.median,metric.unit))}</dd></div><div><dt>Middle 50%</dt><dd>${finite(s.q1) ? esc(fmt(s.q1,metric.unit)+' – '+fmt(s.q3,metric.unit)) : 'Unavailable'}</dd></div></dl>${!across && qualification ? `<p class="drawer-warning">${esc(qualification)}</p>` : ''}${across ? `<p class="drawer-note">${count(data?.observed_games)} observed appearances; ${count(data?.missing_games)} with missing components; ${count(data?.zero_denominator_games)} with zero denominators omitted.${data?.points_truncated ? ' Showing the latest '+count(data.displayed_games)+' dots; summaries use all valid games.' : ''} ${metric.unit==='percent' ? 'Game percentage medians differ from the pooled period percentage. Hover or focus a dot for its attempts.' : 'Zero-event games remain observations.'}</p>` : `<details class="drawer-method"><summary>Qualification & methodology</summary><p>${esc(note)}</p><p>Quantiles use linear interpolation at (n−1). Histograms use equal-width bins; maximum values belong to the last bin. These are observed distributions, not uncertainty intervals.</p></details>`}`;
}

export function bindMetricDrawer(root, overview) {
  root.querySelectorAll('[data-metric-distribution]').forEach(button=>{
    button.addEventListener('click',()=>{
      const metric=overview.allMetrics.find(m=>m.key===button.dataset.metricDistribution);
      if (!metric?.distribution) return;
      if (!dialog) {
        dialog=document.createElement('dialog'); dialog.className='metric-drawer'; dialog.setAttribute('aria-labelledby','metric-drawer-title'); dialog.setAttribute('aria-modal','true');
        document.body.append(dialog);
        dialog.addEventListener('close',()=>{ if(trigger?.isConnected) trigger.focus({preventScroll:true}); });
        dialog.addEventListener('keydown',event=>{
          if(event.key !== 'Tab') return;
          const controls=[...dialog.querySelectorAll('button, select, summary, [tabindex="0"]')].filter(el=>el.getClientRects().length && (el.tagName === 'SUMMARY' || !el.closest('details:not([open])')));
          const first=controls[0], last=controls.at(-1);
          if(event.shiftKey && document.activeElement === first) {event.preventDefault();last?.focus();}
          else if(!event.shiftKey && document.activeElement === last) {event.preventDefault();first?.focus();}
        });
        dialog.addEventListener('click',e=>{ if(e.target===dialog) {const r=dialog.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)dialog.close();} });
      }
      trigger=button;
      const paint=view=>{
        dialog.innerHTML=drawerContent(metric,overview,view);
        dialog.querySelector('[data-drawer-close]').addEventListener('click',()=>dialog.close());
        dialog.querySelectorAll('[data-game-point]').forEach(point=>{
          const reveal=()=>{ dialog.querySelector('[data-game-point-info]').textContent=point.getAttribute('aria-label'); };
          ['mouseenter','focus','click'].forEach(event=>point.addEventListener(event,reveal));
        });
        dialog.querySelector('[data-game-select]')?.addEventListener('change',event=>{
          const index=Number(event.target.value);
          if(event.target.value === '') return;
          dialog.querySelector(`[data-game-index="${index}"]`)?.focus({preventScroll:true});
        });
        dialog.querySelectorAll('[data-distribution-view]').forEach(b=>b.addEventListener('click',()=>{paint(b.dataset.distributionView);dialog.querySelector(`[data-distribution-view="${b.dataset.distributionView}"]`).focus();}));
      };
      paint('among_players');
      if (!dialog.open) dialog.showModal();
      dialog.querySelector('[data-drawer-close]').focus();
    });
  });
}
