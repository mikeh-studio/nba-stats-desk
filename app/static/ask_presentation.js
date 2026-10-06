// Present only governed observations. Context checks are not causal findings.
const finite = (value) => typeof value === "number" && Number.isFinite(value);
export function dateLabel(value) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value || "")) return String(value || "");
  return new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
    timeZone: "UTC",
  }).format(new Date(`${value}T00:00:00Z`));
}
export function ordinal(value) {
  const n = Math.round(value),
    last = n % 100;
  return `${n}${last >= 11 && last <= 13 ? "th" : { 1: "st", 2: "nd", 3: "rd" }[n % 10] || "th"}`;
}
export function overviewPresentation(payload) {
  const evidence = payload?.semantic_evidence;
  if (!Array.isArray(evidence?.metrics) || !evidence.scope?.start) return null;
  const metrics = evidence.metrics;
  const scope = evidence.scope;
  const range = `${dateLabel(scope.start)} – ${dateLabel(scope.end)}`;
  const comparison = `${(scope.previous_phases || scope.phases).join(" + ")} · ${dateLabel(scope.previous_start)} – ${dateLabel(scope.previous_end)}`;
  const eligible = metrics.filter(
    (m) => finite(m.percentile) && !m.missing_component_games,
  );
  const strongest = [...eligible].sort(
    (a, b) => b.percentile - a.percentile,
  )[0];
  const paragraphs = [],
    candidates = [];
  const values = metrics.filter(
    (m) => finite(m.value) && !m.missing_component_games,
  );
  const name = payload.player_profile?.player?.player_name || "The player";
  const supplied = evidence.summary_paragraphs;
  const canonical =
    Array.isArray(supplied) &&
    supplied.length > 0 &&
    supplied.every((p) => typeof p === "string");
  if (canonical) paragraphs.push(...supplied);
  else {
    // Older saved responses lack the canonical summary. Keep their evidence,
    // and avoid reconstructing comparison or efficiency claims in the browser.
    const headline = values.find((m) => m.key === "pts") || strongest;
    paragraphs.push(
      headline
        ? `${name} averaged ${headline.value.toFixed(1)} ${headline.label.toLowerCase()} per game in the requested period.`
        : "Complete metric data is unavailable for an overall assessment.",
    );
    paragraphs.push(
      `This summary covers ${payload.player_profile?.player?.games_sampled || 0} recorded appearances. See Key Metrics for the detailed breakdown.`,
    );
  }
  if (strongest) {
    candidates.push({
      area: strongest.key === "ast" ? "Playmaking" : "Production",
      icon: "chart-bar",
      title:
        strongest.key === "ast"
          ? "Playmaking leads"
          : `Standout ${strongest.label.toLowerCase()}`,
      text: `${strongest.value.toFixed(1)} per game, ${ordinal(strongest.percentile)} percentile among ${strongest.cohort_size} qualified players.`,
    });
  }
  const trajectory = strongest || eligible[0];
  // Require five valid games per bucket and meaningful variation before a trend claim.
  const months = (trajectory?.monthly || []).filter(
    (p) => finite(p.y) && Number.parseInt(p.meta, 10) >= 5,
  );
  if (!trajectory?.missing_component_games && months.length >= 2) {
    const peak = months.reduce((a, b) => (a.y > b.y ? a : b));
    const latest = months.at(-1);
    const low = months.reduce((a, b) => (a.y < b.y ? a : b));
    if (peak.y - low.y >= Math.max(0.2, trajectory.value * 0.15)) {
      const other = peak.x === latest.x ? low : latest;
      const month = (p) =>
        new Intl.DateTimeFormat("en-US", {
          month: "long",
          year: "numeric",
          timeZone: "UTC",
        }).format(new Date(`${p.x}-01T00:00:00Z`));
      candidates.push({
        area: "Trajectory",
        icon: "chart-line",
        title:
          peak.x === latest.x
            ? "A stronger latest month"
            : "The average hides a swing",
        text: `${trajectory.label}: ${peak.y.toFixed(1)} per game in ${month(peak)}, versus ${other.y.toFixed(1)} in ${month(other)}. Monthly samples can differ in minutes, opponents and phase.`,
      });
    }
  }
  const defense = metrics.filter(
    (m) =>
      ["stl", "blk"].includes(m.key) &&
      finite(m.value) &&
      !m.missing_component_games,
  );
  if (defense.length)
    candidates.push({
      area: "Defense",
      icon: "shield",
      title: "Defensive activity, not a verdict",
      text: `${defense.map((m) => `${m.value.toFixed(1)} ${m.label.toLowerCase()}`).join(" and ")} per game. Box scores alone do not establish defensive impact; minutes, defensive rebounds and fouls need context.`,
    });
  if (!canonical && metrics.some((m) => m.missing_component_games)) {
    const labels = metrics
      .filter((m) => m.missing_component_games)
      .map((m) => m.label);
    paragraphs[1] += ` Partial data for ${labels.join(", ")}: incomplete metrics are excluded from this assessment. Missing values are not zero.`;
  }

  if (candidates.length < 3 && values.length)
    candidates.push({
      area: "Sample",
      icon: "chart-bar",
      title: "Read the sample alongside the averages",
      text: `${payload.player_profile?.player?.games_sampled || 0} recorded appearances across ${(scope.phases || []).join(" + ")}. Small or mixed-phase samples need care.`,
    });
  const player = payload.player_profile?.player?.player_name || "this player";
  return {
    paragraphs,
    insights: candidates.slice(0, 3),
    metrics,
    range,
    comparison,
    scope,
    minGames: evidence.min_games || 5,
    preferredMetric: strongest?.key || "pts",
    followups: [
      `Show ${player}'s points from ${scope.start} through ${scope.end} (${scope.phases.join(" + ")}).`,
      `Show ${player}'s performance from ${scope.start} through ${scope.end}, regular season.`,
    ],
  };
}
