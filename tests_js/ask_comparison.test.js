import test from "node:test";
import assert from "node:assert/strict";
import {
  comparisonPresentation,
  comparisonTables,
  comparisonChart,
  comparisonAnswer,
} from "../app/static/ask_comparison.js";

function fixture() {
  const stat = (value, percentile) => ({
    value,
    percentile,
    cohort_size: 20,
    valid_games: 6,
    observed_games: 6,
    missing_component_games: 0,
  });
  return {
    player_profiles: ["Brunson", "Wembanyama"].map((name) => ({
      player: { player_name: name, games_sampled: 6 },
    })),
    semantic_evidence: {
      kind: "player_comparison",
      scope: { phases: ["Playoffs"], start: "2026-04-01", end: "2026-06-30" },
      metrics: [
        {
          key: "pts",
          label: "Points",
          left: stat(30, 99),
          right: stat(25, 95),
          delta: 5,
        },
      ],
      paragraphs: ["Source-backed comparison <not HTML>"],
      min_games: 5,
      impact: [
        {
          key: "ws",
          label: "Win Shares",
          left: null,
          right: null,
          reason: "Not connected",
          definition: "Estimated contribution",
        },
      ],
    },
  };
}

test("comparison preserves both names, explicit scope and unavailable winning metrics", () => {
  const view = comparisonPresentation(fixture());
  const table = comparisonTables(view);
  assert.match(table, /Brunson by 5.0/);
  assert.match(table, /Wembanyama/);
  assert.match(table, /Playoffs/);
  assert.match(table, /Win Shares/);
  assert.match(table, /Unavailable/);
  assert.doesNotMatch(table, /Previous period|Baseline/);
  assert.match(comparisonAnswer(view), /&lt;not HTML&gt;/);
  assert.equal(view.insights.length, 3);
});

test("chart exposes missing impact state without plotting invented values", () => {
  const view = comparisonPresentation(fixture());
  assert.match(comparisonChart(view, "impact"), /Net-rating chart unavailable/);
  assert.doesNotMatch(comparisonChart(view, "impact"), /comparison-bar-track/);
  assert.match(comparisonChart(view, "pts"), /Brunson: 30.0/);
  view.metrics[0].left.missing_component_games = 1;
  assert.match(comparisonChart(view, "pts"), /Brunson: Unavailable/);
  view.metrics[0].left.missing_component_games = 0;
  view.metrics[0].left.value = 0;
  assert.match(comparisonChart(view, "pts"), /Brunson: 0.0/);
});

test("old single-player payloads do not enter comparison rendering", () => {
  assert.equal(comparisonPresentation({}), null);
});
