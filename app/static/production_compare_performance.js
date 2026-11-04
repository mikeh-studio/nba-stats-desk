// A publication is pinned for every multi-request performance selection.
export async function publicationRead(path, token = null, fetcher = fetch) {
  const response = await fetcher(`/publication-core${path}`, {
    cache: 'no-store', headers: token ? {'X-Publication-ID': token} : {},
  });
  if (!response.ok) throw new Error('Publication unavailable');
  const payload = await response.json();
  if (!/^pub_[0-9a-f]{16}$/.test(payload.publication_id) ||
      (token && payload.publication_id !== token)) throw new Error('Publication changed');
  return payload;
}

export function comparisonWindow(window) {
  return ({regular_season: 'scope', last_5: 'last_5', prior_5: 'prior_5', last_10: 'last_10'})[window] || null;
}

function scopeText(p) {
  return `${p.scope.season} ${p.scope.phase}, ${p.scope.first} through ${p.scope.last} · Publication ${p.publication_id}`;
}

function table(container, headings, rows) {
  const table = document.createElement('table');
  table.className = 'data-table compact';
  const head = document.createElement('thead');
  const header = document.createElement('tr');
  for (const heading of headings) {
    const cell = document.createElement('th'); cell.textContent = heading; header.appendChild(cell);
  }
  head.appendChild(header); table.appendChild(head);
  const body = document.createElement('tbody');
  for (const row of rows) {
    const tr = document.createElement('tr');
    for (const value of row) {
      const cell = document.createElement('td');
      cell.textContent = value == null ? 'Unavailable' : String(value);
      tr.appendChild(cell);
    }
    body.appendChild(tr);
  }
  table.appendChild(body); container.replaceChildren(table);
}

export async function initCompare(card) {
  const status = card.querySelector('[data-publication-status]');
  const results = card.querySelector('[data-publication-results]');
  const {playerA, playerB, window} = card.dataset;
  const selectedWindow = comparisonWindow(window);
  if (!playerA || !playerB || playerA === playerB) {
    status.textContent = 'Select two different players in the comparison above.'; return;
  }
  if (!selectedWindow) {
    status.textContent = 'This window is unavailable in the publication; the existing comparison remains below.'; return;
  }
  try {
    const params = new URLSearchParams({player_a_id: playerA, player_b_id: playerB, window: selectedWindow});
    const payload = await publicationRead(`/api/compare?${params}`);
    status.textContent = `${scopeText(payload)} · ${window.replaceAll('_', ' ')}. Differences are player A minus player B.`;
    table(results, ['Metric', 'Aggregation', 'A − B', 'Unit'], payload.data.differences.map(m => [m.metric, m.aggregation, m.difference, m.unit]));
  } catch { status.textContent = 'Published comparison unavailable. The existing comparison remains below.'; }
}

export async function initPerformance(card) {
  const date = card.querySelector('[data-publication-date]');
  const game = card.querySelector('[data-publication-game]');
  const status = card.querySelector('[data-publication-status]');
  const results = card.querySelector('[data-publication-results]');
  let token, generation = 0;
  function reset(select, text) { select.replaceChildren(new Option(text, '')); select.disabled = true; }
  try {
    const payload = await publicationRead('/api/performance/dates');
    token = payload.publication_id;
    reset(date, 'Select a date');
    for (const value of payload.data) date.add(new Option(value, value));
    date.disabled = false; status.textContent = scopeText(payload);
  } catch { status.textContent = 'Published performance unavailable. The existing playoff view remains below.'; return; }
  date.addEventListener('change', async () => {
    const selected = ++generation;
    reset(game, 'Select a game'); results.replaceChildren();
    if (!date.value) return;
    try {
      const payload = await publicationRead(`/api/performance/games?game_date=${encodeURIComponent(date.value)}`, token);
      if (selected !== generation) return;
      for (const g of payload.data) game.add(new Option(`${g.away_team_abbr} at ${g.home_team_abbr}`, g.game_id));
      game.disabled = false; status.textContent = scopeText(payload);
    } catch { if (selected === generation) status.textContent = 'Publication changed or became unavailable. Reload to start again.'; }
  });
  game.addEventListener('change', async () => {
    const selected = ++generation; results.replaceChildren();
    if (!game.value) return;
    try {
      const payload = await publicationRead(`/api/performance/players?game_id=${encodeURIComponent(game.value)}`, token);
      if (selected !== generation) return;
      const keys = ['min', 'pts', 'reb', 'ast', 'fg3m', 'stl', 'blk'];
      table(results, ['Player', 'Team', ...keys.map(k => k.toUpperCase())], payload.data.map(p => [p.player_name, p.team_abbr, ...keys.map(k => p.metrics.find(m => m.metric === k && m.aggregation === 'total')?.value)]));
      status.textContent = scopeText(payload);
    } catch { if (selected === generation) status.textContent = 'Publication changed or became unavailable. Reload to start again.'; }
  });
}

if (typeof document !== 'undefined') {
  const comparison = document.querySelector('[data-publication-compare]');
  const performance = document.querySelector('[data-publication-performance]');
  if (comparison) initCompare(comparison);
  if (performance) initPerformance(performance);
}
