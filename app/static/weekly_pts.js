const form = document.querySelector('#weekly-pts-form');
const result = document.querySelector('#weekly-pts-result');
const search = document.querySelector('#weekly-player-search');
const players = document.querySelector('#weekly-player-results');
players?.addEventListener('change', () => {
  form.querySelector('[name="player_id"]').value = players.value;
});
document.querySelector('#weekly-player-find')?.addEventListener('click', async () => {
  const query = search.value.trim();
  if (!query) { result.textContent = 'Enter a player name.'; return; }
  result.textContent = 'Finding players…';
  try {
    const response = await fetch(`/api/players/search?q=${encodeURIComponent(query)}`);
    if (!response.ok) throw new Error('search failed');
    const data = await response.json();
    players.replaceChildren(new Option('Choose a player', ''));
    for (const player of data.items || []) {
      if (Number.isInteger(player.player_id) && typeof player.player_name === 'string') {
        players.add(new Option(player.player_name, String(player.player_id)));
      }
    }
    result.textContent = players.options.length > 1 ? 'Choose a player.' : 'No players found.';
  } catch { result.textContent = 'Player search unavailable.'; }
});
form?.addEventListener('submit', async event => {
  event.preventDefault();
  result.textContent = 'Loading weekly snapshot…';
  try {
    const data = new FormData(form);
    const response = await fetch('/api/weekly-pts/ask', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({player_id: Number(data.get('player_id')), aggregation: data.get('aggregation')})
    });
    if (!response.ok) { result.textContent = 'Weekly PTS snapshot unavailable.'; return; }
    const payload = await response.json();
    const row = payload.tables[0].rows[0];
    const scope = payload.binding.scope;
    const label = data.get('aggregation') === 'total' ? 'observed points' : 'points per valid appearance';
    const value = row[1] == null ? 'unavailable' : `${row[1]} ${label}`;
    result.textContent = `Player ${row[0]}: ${value}; ${row[2]} appearances, ${row[3]} with PTS, ${row[4]} missing PTS. ${scope.season} ${scope.phase}, ${scope.start_date} through ${scope.end_date}. Snapshot ${payload.binding.snapshot_id}.`;
  } catch { result.textContent = 'Weekly PTS snapshot unavailable.'; }
});
