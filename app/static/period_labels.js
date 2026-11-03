// July–June bounds select warehouse seasons; they are not playing dates.
// Official NBA schedule releases; provenance links are in docs/ask-workspace.md.
const REGULAR_SEASON_STARTS = {
  2023: '2023-10-24',
  2024: '2024-10-22',
  2025: '2025-10-21',
  2026: '2026-10-20',
};
export function periodDate(value) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value || '')) return String(value || '');
  return new Intl.DateTimeFormat('en-US', {month:'short',day:'numeric',year:'numeric',timeZone:'UTC'}).format(new Date(`${value}T00:00:00Z`));
}
export function periodRange(scope, baseline = false) {
  const start = baseline ? scope.previous_start : scope.start;
  const end = baseline ? scope.previous_end : scope.end;
  const year = Number(String(start).slice(0, 4));
  const seasonBounds = start === `${year}-07-01` && end >= start && end <= `${year + 1}-06-30`;
  // Saved records predate window_kind. Only recognize an intact annual boundary;
  // new explicit ranges and resolved appearance windows retain their exact dates.
  if (seasonBounds && (!scope.window_kind || scope.window_kind === 'season')) {
    const phases = baseline ? scope.previous_phases || scope.phases : scope.phases;
    const opening = phases?.includes('Regular Season') ? REGULAR_SEASON_STARTS[year] : null;
    if (opening) {
      if (end < opening) return `Season starts ${periodDate(opening)} · As of ${periodDate(end)}`;
      return `${periodDate(opening)} – ${periodDate(end)}`;
    }
    const season = `${year}–${String(year + 1).slice(-2)} season`;
    return end === `${year + 1}-06-30` ? season : `${season} · Through ${periodDate(end)}`;
  }
  return `${periodDate(start)} – ${periodDate(end)}`;
}
