function show(result, payload) {
  const scope = payload.scope;
  const summary = payload.data.summary;
  const metrics = summary.metrics.filter(m => ['pts', 'reb', 'ast', 'min'].includes(m.metric) && ['total', 'per_game'].includes(m.aggregation));
  result.textContent = `${payload.data.identity.player_name}\n${scope.season} ${scope.phase}, ${scope.first} through ${scope.last}\nPublication ${payload.publication_id}\n${summary.observed_games} observed games\n\n${metrics.map(m => `${m.metric.toUpperCase()} ${m.aggregation.replaceAll('_', ' ')}: ${m.value == null ? 'unavailable' : m.value} (${m.valid_games}/${m.observed_games} valid games)`).join('\n')}`;
}

async function loadPlayer() {
  const card = document.querySelector('[data-publication-player]');
  if (!card || card.dataset.publicationLoading) return;
  card.dataset.publicationLoading = 'true';
  const result = card.querySelector('[data-publication-answer]');
  try {
    const response = await fetch(`/publication-core/api/players/${encodeURIComponent(card.dataset.publicationPlayer)}`);
    if (response.status === 404) { result.textContent = 'This player is not in the current publication.'; return; }
    if (!response.ok) throw new Error('publication unavailable');
    show(result, await response.json());
  } catch { result.textContent = 'Publication unavailable. Other player fields use the existing app data.'; }
}
loadPlayer();
