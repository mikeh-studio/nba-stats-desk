// Only canonical full-sample game summaries can drive the head-to-head plots.
const finite = (v) => typeof v === "number" && Number.isFinite(v);
export const distributionNumber = (v) => String(Number(v.toFixed(2)));

export function gameSummary(side) {
  const distribution = side?.distribution, games = distribution?.across_games;
  const s = games?.summary;
  if (distribution?.version !== "ask_metric_distribution/1" ||
      distribution.quantile_method !== "linear (n-1)" || !s ||
      !Number.isInteger(s.n) || s.n < 1 || !finite(side.value) ||
      side.missing_component_games !== 0 || games.missing_games !== 0 ||
      games.zero_denominator_games !== 0 || s.n !== side.valid_games ||
      s.n !== side.observed_games || s.n !== games.observed_games) return null;
  const values = [s.min, s.q1, s.median, s.q3, s.max];
  if (!values.every(finite) || s.min < 0 ||
      values.some((v, i) => i && v < values[i - 1]) ||
      side.value < s.min || side.value > s.max) return null;
  return s;
}

export function comparisonDistribution(metric) {
  const left = gameSummary(metric.left), right = gameSummary(metric.right);
  // Both players use the same increasing scale. Zero-only samples retain zero
  // spread; the one-unit axis is padding, never an invented observation.
  return {left, right, lo: 0, hi: Math.max(1, left?.max ?? 0, right?.max ?? 0)};
}

export function boxPosition(value, scale) {
  return Number((5 + 90 * (value - scale.lo) / (scale.hi - scale.lo)).toFixed(3));
}

export function meanDiamond(value, scale) {
  const x = boxPosition(value, scale);
  return `${x},6 ${Number((x + 1.8).toFixed(3))},12 ${x},18 ${Number((x - 1.8).toFixed(3))},12`;
}
