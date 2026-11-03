// Shared display policy for renderers and their canonical claim destinations.
const finite = Number.isFinite;
export function metricAppearances(metric) {
  return finite(metric.valid_games) && finite(metric.observed_games) && metric.valid_games !== metric.observed_games
    ? `${metric.valid_games} of ${metric.observed_games} appearances` : '';
}
export const hasAvailableValue = value => value != null
  && (typeof value !== 'number' || finite(value))
  && !/^(?:\s*|[-–—]|unavailable|not available|not ranked|none|null|n\/a)$/i.test(String(value).trim());

export function availableColumns(keys, rows) {
  return keys.filter((_, i) => rows.some(row => hasAvailableValue(row[i])));
}
export function overviewTableColumns(metrics) {
  return availableColumns(['value', 'percentile', 'change'], metrics.map(m => [
    finite(m.value) ? m.value : null,
    finite(m.percentile) && m.sample?.status !== 'limited' ? m.percentile : null,
    finite(m.sample ? m.display_change : m.change) ? (m.sample ? m.display_change : m.change) : null,
  ]));
}
export function comparisonTableColumns(metrics) {
  return availableColumns(['left', 'left_percentile', 'right_percentile', 'right'], metrics.map(m => [
    m.left?.value, m.left?.percentile, m.right?.percentile, m.right?.value,
  ].map(v => finite(v) ? v : null)));
}
const usable = side => finite(side?.value) && !side.missing_component_games;
export function comparisonPrecisionColumns(metrics) {
  return availableColumns(['left', 'right', 'gap'], metrics.map(m => [
    usable(m.left) ? m.left.value : null,
    usable(m.right) ? m.right.value : null,
    usable(m.left) && usable(m.right) && finite(m.delta) ? m.delta : null,
  ]));
}
export function comparisonDetailColumns(metrics) {
  return availableColumns(['left', 'right', 'comparison'], metrics.map(m => [
    finite(m.left?.value) ? m.left.value : null,
    finite(m.right?.value) ? m.right.value : null,
    m.comparison,
  ]));
}
