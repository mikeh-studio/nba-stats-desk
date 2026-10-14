import test from "node:test";
import assert from "node:assert/strict";
import {
  comparisonPresentation,
  comparisonTables,
  comparisonChart,
  comparisonAnswer,
  comparisonAssumptions,
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

test("comparison preserves names and scope while hiding unavailable winning metrics", () => {
  const view = comparisonPresentation(fixture());
  const table = comparisonTables(view);
  assert.match(table, /Brunson by 5.0/);
  assert.match(table, /Wembanyama/);
  assert.match(table, /Playoffs/);
  assert.doesNotMatch(table, /Win Shares|Impact on winning|possession/);
  assert.doesNotMatch(table, /Previous period|Baseline/);
  assert.match(comparisonAnswer(view), /&lt;not HTML&gt;/);
  assert.equal(view.insights.length, 3);
});

test("chart hides unavailable impact controls and falls back to a supported stat", () => {
  const view = comparisonPresentation(fixture());
  assert.doesNotMatch(comparisonChart(view, "impact"), /Team net rating|Net-rating chart unavailable|data-comparison-metric="impact"/);
  assert.match(comparisonChart(view, "impact"), /data-comparison-metric="pts" aria-pressed="true"/);
  assert.match(comparisonChart(view, "pts"), /Brunson: 30.0/);
  view.metrics[0].left.missing_component_games = 1;
  assert.match(comparisonChart(view, "pts"), /Brunson: Unavailable/);
  view.metrics[0].left.missing_component_games = 0;
  view.metrics[0].left.value = 0;
  assert.match(comparisonChart(view, "pts"), /Brunson: 0.0/);
});

test("saved missing-feed prose is hidden without discarding ordinary caveats", () => {
  const payload = fixture();
  const ordinary = "A small sample limits the comparison.";
  payload.semantic_evidence.paragraphs = [ordinary, "These category differences do not establish an overall winner. Win Shares and possession-based on/off ratings are unavailable in the governed source, so total versus per-minute winning contribution cannot yet be assessed."];
  payload.assumptions = [ordinary, "Win Shares and on/off possession data are not connected. Raw plus/minus is not substituted for net rating. No illustrative values are used."];
  const before = JSON.stringify(payload);
  const view = comparisonPresentation(payload);
  assert.doesNotMatch(comparisonAnswer(view), /Win Shares|possession|Winning-impact metrics/);
  assert.match(comparisonAnswer(view), /small sample|overall winner|Playing time and workload/);
  assert.deepEqual(comparisonAssumptions(payload), [ordinary]);
  assert.equal(JSON.stringify(payload), before);
  assert.deepEqual(comparisonAssumptions({...payload, semantic_evidence:{kind:"unsupported"}}), payload.assumptions);
});

test("impact rows return only with numeric data for both players, including valid zero", () => {
  const payload = fixture();
  payload.semantic_evidence.impact[0].left = 0;
  assert.doesNotMatch(comparisonTables(comparisonPresentation(payload)), /Win Shares/);
  payload.semantic_evidence.impact[0].right = 2;
  const table = comparisonTables(comparisonPresentation(payload));
  assert.match(table, /Win Shares/);
  assert.match(table, /<td>0.0<\/td><td>2.0<\/td>/);
});

test("old single-player payloads do not enter comparison rendering", () => {
  assert.equal(comparisonPresentation({}), null);
});

test("additional comparisons render server differences, units and partial samples", () => {
  const payload = fixture();
  payload.semantic_evidence.comparison_sections = [{key:'shooting_efficiency',title:'Shooting efficiency',
    description:'Percentages use total makes and attempts.',metrics:[{key:'ts_pct',label:'TS%',unit:'percent',
      left:{value:54.095,valid_games:69,observed_games:69,missing_component_games:0,components:{fga:729,fta:47,fg3a:527}},
      right:{value:53.981,valid_games:68,observed_games:68,missing_component_games:0,components:{fga:491,fta:168}},
      comparison:'Klay Thompson is higher by 0.11 percentage points'}]}];
  let html = comparisonTables(comparisonPresentation(payload));
  assert.match(html,/54.1%/);
  assert.match(html,/53.98|54.0%/);
  assert.match(html,/0.11 percentage points/);
  assert.match(html,/69 of 69 games/);
  assert.match(html,/729 FGA/);
  assert.match(html,/527 3PA/);
  payload.semantic_evidence.comparison_sections[0].metrics[0].left.missing_component_games=1;
  payload.semantic_evidence.comparison_sections[0].metrics[0].comparison='Comparison withheld: incomplete inputs';
  html=comparisonTables(comparisonPresentation(payload));
  assert.match(html,/partial inputs/);
  assert.match(html,/Comparison withheld/);
});
