// Display-only vocabulary for governed metric results. Never mutate source evidence.
const METRICS = {
  gp: ['Games played', 'Games recorded for the player in the selected period.'],
  pts: ['Points', 'Points scored.'], reb: ['Rebounds', 'Offensive and defensive rebounds combined.'],
  ast: ['Assists', 'Passes credited with directly leading to a made basket.'],
  stl: ['Steals', 'Recorded steals; this alone does not measure overall defensive ability.'],
  blk: ['Blocks', 'Recorded blocked shots; this alone does not measure overall defensive ability.'],
  tov: ['Turnovers', 'Recorded turnovers; fewer turnovers alone do not establish better ball handling.'],
  fg3m: ['Three-pointers made', 'Made three-point shots.'], min: ['Minutes', 'Minutes played.'],
  fga: ['Field-goal attempts', 'Two-point and three-point shot attempts, excluding free throws.'],
  fg3a: ['Three-point attempts', 'Attempted three-point shots.'],
  fta: ['Free-throw attempts', 'Attempted free throws.'],
  fg_pct: ['Field-goal percentage', 'Made field goals divided by field-goal attempts.'],
  fg3_pct: ['Three-point percentage', 'Made three-pointers divided by three-point attempts.'],
  ft_pct: ['Free-throw percentage', 'Made free throws divided by free-throw attempts.'],
  ts_pct: ['True shooting percentage', 'Scoring efficiency including two-pointers, three-pointers and free throws: points ÷ [2 × (field-goal attempts + 0.44 × free-throw attempts)].'],
  efg_pct: ['Effective field-goal percentage', 'Shooting percentage adjusted for the extra point on three-pointers: (field goals made + 0.5 × three-pointers made) ÷ field-goal attempts.'],
  pts_per36: ['Points per 36 minutes', 'Points scaled to 36 minutes played; not a prediction of a full game.'],
  ast_per36: ['Assists per 36 minutes', 'Assists scaled to 36 minutes played; not a prediction of a full game.'],
  plus_minus: ['Plus-minus', 'Team points scored minus opponent points while the player is on court; influenced by teammates and opponents.'],
  fantasy_points_simple: ['Box-score index', 'Custom index: points + rebounds + assists + steals + blocks − turnovers. Not a standard fantasy score.'],
  fantasy_proxy_weighted: ['Estimated fantasy points', 'Custom scoring: points + 1.2 × rebounds + 1.5 × assists + 3 × steals + 3 × blocks + three-pointers made − turnovers. League scoring rules may differ.'],
  points_created: ['Estimated points created', 'Simple estimate: points + 2 × assists. This does not measure all points created for teammates.'],
};
const sections = payload => {
  const e = payload?.semantic_evidence;
  return e?.current ? [e.current, e.baseline] : e ? [e] : [];
};
export function metricName(metric) { return METRICS[metric?.key]?.[0] || metric?.label || metric?.key || 'Statistic'; }
export function metricHelp(metric) { return METRICS[metric?.key]?.[1] || ''; }
export function metricLabel(metric, scope = {}) {
  const name = metricName(metric);
  if (scope.operation === 'game_log' || metric?.key === 'gp' || ['ratio', 'per36'].includes(metric?.unit)) return name;
  if (scope.aggregation === 'average') return `${name} per game`;
  if (scope.aggregation === 'total') return `Total ${name.toLowerCase()}`;
  return name;
}
export function displayDate(value) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value || '')) return value || '';
  const date = new Date(`${value}T00:00:00Z`);
  return Number.isNaN(date.valueOf()) ? value : new Intl.DateTimeFormat('en-US', {month:'short', day:'numeric', year:'numeric', timeZone:'UTC'}).format(date);
}
export function scopeLabel(scope) {
  const seasons = (scope.seasons || [scope.season]).filter(Boolean).map(s => s.replace('-', '–')).join(', ');
  const phase = ({'Regular Season':'regular season', Playoffs:'playoffs', All:'all phases'})[scope.season_type] || scope.season_type || '';
  const parts = [`${seasons} ${phase}`.trim()];
  const windows = {last_n_games:'games', last_n_days:'days', last_n_months:'calendar months'};
  if (windows[scope.window]) parts.push(`Last ${scope.n} ${windows[scope.window]}`);
  if (scope.window === 'date_range' && (scope.start_date || scope.window_start)) {
    parts.push(`${displayDate(scope.start_date || scope.window_start)} – ${displayDate(scope.window_end || scope.as_of)}`);
  } else if (scope.as_of) parts.push(`Through ${displayDate(scope.as_of)}`);
  if (scope.team_abbr) parts.push(`Team: ${scope.team_abbr}`);
  if (scope.opponent_abbr) parts.push(`Against ${scope.opponent_abbr}`);
  if (scope.home_away) parts.push(scope.home_away === 'HOME' ? 'Home games' : 'Away games');
  return parts.filter(Boolean).join(' · ');
}
export function semanticText(text, payload) {
  let result = String(text || '');
  for (const section of sections(payload)) {
    const metric = section?.metric;
    if (!metric || !METRICS[metric.key] || !metric.label) continue;
    const escaped = metric.label.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    result = result.replace(new RegExp(`(?<![A-Za-z0-9])${escaped}(?![A-Za-z0-9])`, 'g'), metricName(metric).toLowerCase());
  }
  return result;
}
export function semanticTable(table, payload, index) {
  const section = sections(payload)[index];
  if (!section?.metric || !section.scope || !METRICS[section.metric.key]) return table;
  const {metric, scope, cohort} = section;
  const label = metricLabel(metric, scope);
  const sourceColumns = table.columns || [];
  if (!sourceColumns.some(c => c.key === 'value')) return table;
  const rows = table.rows || [];
  const gameLog = scope.operation === 'game_log';
  const ranking = scope.operation === 'rank';
  const lower = (cohort?.direction || scope.direction || metric.direction) === 'lower';
  const count = section.rows?.length ?? rows.length;
  const people = count === 1 ? 'player' : 'players';
  const side = payload.semantic_evidence?.current ? (index ? 'Baseline: ' : 'Current: ') : '';
  const person = payload.claim_evidence?.identities?.[String(scope.player_id)]?.player_name;
  let title = `${person ? `${person} — ` : ''}${label}`;
  if (ranking) title = lower ? `${label} — lowest first${count ? ` (${count} ${people})` : ''}` : `${count ? `Top ${count} ${people}` : 'Players ranked'} by ${label.toLowerCase()}`;
  if (gameLog) title += ' by game';
  const observed = sourceColumns.findIndex(c => c.key === 'observed');
  const valid = sourceColumns.findIndex(c => c.key === 'valid');
  const mergeGames = observed >= 0 && valid >= 0;
  const incomplete = mergeGames && rows.some(r => r[observed] !== r[valid]);
  let indices = sourceColumns.map((_, i) => i).filter(i => !mergeGames || i !== valid);
  const rankIndex = sourceColumns.findIndex(c => c.key === 'rank');
  if (ranking && rankIndex >= 0) indices = [rankIndex, ...indices.filter(i => i !== rankIndex)];
  const columns = indices.map(i => {
    const c = sourceColumns[i];
    const labels = {value:label, observed:incomplete ? 'Games used' : 'Games played', valid:'Games used', percentile:'League percentile'};
    return {...c, label:labels[c.key] || c.label};
  });
  const displayRows = rows.map(r => indices.map(i => mergeGames && incomplete && i === observed ? `${r[valid]} of ${r[observed]}` : r[i]));
  const qualifications = [];
  if (!gameLog && cohort) {
    if (cohort.min_games != null) qualifications.push(`Minimum ${cohort.min_games} games`);
    if (cohort.min_attempts != null) qualifications.push(`Minimum ${cohort.min_attempts} ${metricName({key:cohort.attempt_field}).toLowerCase()}`);
    if (cohort.size != null) qualifications.push(`${cohort.size} qualifying players`);
    if (ranking && count < scope.limit) qualifications.push(`${count} of ${scope.limit} requested results available`);
  }
  const help = [metricHelp(metric)];
  if (['ratio', 'per36'].includes(metric.unit)) help.push('A rate can be unavailable when its attempts or minutes are zero, even when all game records are present.');
  if (cohort) help.push(`League percentile compares this stat among the ${cohort.size} qualifying players for the selected period and filters. ${lower ? 'Lower' : 'Higher'} values rank first; a higher percentile means a better position in this ranking, not overall player quality.`);
  if (mergeGames) help.push(incomplete ? 'Games used shows games with all required data out of games played in the selected period. Missing data is not zero.' : 'Games played counts recorded games in the selected period. All displayed games have the data needed for this stat.');
  return {...table, title:side + title, subtitle:scopeLabel(scope), qualification:qualifications.join(' · '), columns, rows:displayRows, description:'', help:help.filter(Boolean).join(' '), presentation:'semantic'};
}
export function semanticChart(chart, payload, index) {
  const section = sections(payload)[index];
  if (!section?.metric || !METRICS[section.metric.key]) return chart;
  return {...chart, title:semanticText(chart.title, payload), y_label:metricLabel(section.metric, section.scope), series:(chart.series || []).map(s => ({...s, label:semanticText(s.label, payload)}))};
}
