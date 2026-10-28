// Presentation thresholds describe visible rate differences, not player quality
// or statistical significance. Canonical values and deltas are never changed.
export const COMPARISON_POLICY = Object.freeze({ absoluteRate: 0.1, relativeRate: 0.05 });
export function comparisonLead(metric) {
  if (!metric || !Number.isFinite(metric.delta) ||
      ![metric.left, metric.right].every((s) => s && Number.isFinite(s.value) && !s.missing_component_games)) return null;
  const left = metric.left.value, right = metric.right.value;
  const band = Math.max(COMPARISON_POLICY.absoluteRate,
    COMPARISON_POLICY.relativeRate * Math.max(Math.abs(left), Math.abs(right)));
  if (left.toFixed(1) === right.toFixed(1) || Math.abs(metric.delta) <= band + 1e-12) return null;
  return metric.delta > 0 ? 0 : 1;
}
export function comparisonStatus(metric) {
  if (!metric || !Number.isFinite(metric.delta) ||
      ![metric.left, metric.right].every((s) => s && Number.isFinite(s.value) && !s.missing_component_games)) return "Incomplete data";
  return comparisonLead(metric) === null ? "Similar rates" : "Different recorded rates";
}
