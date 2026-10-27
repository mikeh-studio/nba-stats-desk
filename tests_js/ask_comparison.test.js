import {comparisonLead, comparisonStatus} from "../app/static/comparison_policy.js";
import test from "node:test";
import assert from "node:assert/strict";
import {
  comparisonPresentation,
  comparisonTables,
  comparisonAnswer,
  comparisonAssumptions,
  comparisonProfiles,
} from "../app/static/ask_comparison.js";
import { claimSchema, expectedClaim } from "../app/static/ask_claim_bindings.js";

const stat = (value, percentile) => ({
  value,
  percentile,
  cohort_size: 20,
  valid_games: 6,
  observed_games: 6,
  missing_component_games: 0,
});
const metric = (key, label, left, right) => ({ key, label, left, right, delta: left.value - right.value });
const detail = (key, label, unit, left, right) => ({ key, label, unit, left: stat(left, null), right: stat(right, null), comparison: "Server comparison" });

function fixture() {
  return {
    player_profiles: [
      ["Jalen Brunson", 180],
      ["Tyrese Maxey", 210],
    ].map(([name, minutes]) => ({
      player: { player_name: name, games_sampled: 6, team_abbr: "NYK" },
      minutes,
      minutes_per_game: minutes / 6,
    })),
    semantic_evidence: {
      kind: "player_comparison",
      scope: { phases: ["Playoffs"], start: "2026-04-01", end: "2026-06-30" },
      metrics: [
        metric("pts", "Points", stat(26, 92), stat(27.4, 95)),
        metric("reb", "Rebounds", stat(3.4, 30), stat(3.3, 28)),
        metric("ast", "Assists", stat(7.1, 93), stat(6.4, 89)),
        metric("stl", "Steals", stat(0.9, 45), stat(1.8, 96)),
        metric("blk", "Blocks", stat(0.1, 8), stat(0.4, 41)),
      ],
      comparison_sections: [
        { key: "shooting_efficiency", title: "Shooting efficiency", description: "Percentages use total makes and attempts.", metrics: [detail("ts_pct", "TS%", "percent", 58.1, 56)] },
        { key: "per36_production", title: "Per-36 production", description: "Rates use total minutes.", metrics: [detail("pts_per36", "Points per 36 minutes", "per 36 minutes", 26.6, 25.6), detail("ast_per36", "Assists per 36 minutes", "per 36 minutes", 7.3, 6)] },
      ],
      paragraphs: ["Source-backed comparison <not HTML>"],
      min_games: 5,
      data_through: "2026-06-30",
      impact: [
        { key: "ws", label: "Win Shares", left: null, right: null, reason: "Not connected", definition: "Estimated <b>contribution</b>" },
      ],
    },
  };
}

test("comparison keeps names and scope while hiding unavailable winning metrics", () => {
  const view = comparisonPresentation(fixture());
  const table = comparisonTables(view);
  assert.match(table, /Jalen Brunson/);
  assert.match(table, /Tyrese Maxey/);
  assert.match(table, /<p class="metric-comparison">Playoffs · Apr 1, 2026 – Jun 30, 2026<\/p>/);
  assert.doesNotMatch(table, /Win Shares|Impact on winning|possession/);
  assert.match(comparisonProfiles(view), /Data through Jun 30, 2026/);
  assert.doesNotMatch(comparisonAnswer(view), /<b>|<not HTML>/);
  assert.equal(view.insights.length, 3);
});

test("summary describes recorded measures without aggregate winner meters", () => {
  const view = comparisonPresentation(fixture());
  assert.deepEqual(view.areas.map((a) => a.title), ["Scoring & assists", "Rebounds, steals & blocks", "Playing time"]);
  const answer = comparisonAnswer(view);
  assert.match(answer, /Comparison summary/);
  assert.match(answer, /Small gaps and rounded ties/);
  assert.match(answer, /do not establish an overall winner or defensive ability/);
  assert.doesNotMatch(answer, /lean-meter|Favors |leads defense|>Verdict</);
  assert.ok(view.areas.every((a) => !("lean" in a)));
});

test("rounded ties and negligible rates stay neutral while exact data remain inspectable", () => {
  const data = fixture();
  data.semantic_evidence.metrics[3] = metric("stl", "Steals", stat(1.401, 93), stat(1.419, 94));
  data.semantic_evidence.metrics[4] = metric("blk", "Blocks", stat(0.779, 86), stat(0.827, 87));
  const before = JSON.stringify(data);
  const view = comparisonPresentation(data), table = comparisonTables(view);
  assert.match(view.areas[1].text, /Steal rates are similar; Block rates are similar/);
  const rows = [...table.matchAll(/<tr>(.*?)<\/tr>/g)].map((m) => m[1]);
  for (const label of ["STL", "BLK"]) {
    const row = rows.find((r) => r.includes(`h2h-label">${label}`));
    assert.match(row, /Similar rates/);
    assert.doesNotMatch(row, /is-leader|is-trailing|h2h-edge/);
  }
  assert.doesNotMatch(table, /\+0\.0|\+0\.1/);
  assert.match(table, /More precision &amp; comparison rules/);
  assert.match(table, /1\.401/);
  assert.match(table, /1\.419/);
  assert.match(table, /0\.018/);
  assert.match(table, /not a test of statistical significance/);
  assert.equal(JSON.stringify(data), before);
  const claims = claimSchema(view.claimEvidence, "comparison");
  assert.ok(!claims.some((r) => r.format === "comparison-margin" && ["stl", "blk"].includes(r.context.metricKey)));
  assert.equal(claims.filter((r) => r.format === "comparison-precision").length, 10);
  assert.equal(claims.filter((r) => r.format === "comparison-precision-gap").length, 5);
});

test("similarity boundaries cover displayed ties, absolute gaps, relative gaps and incomplete samples", () => {
  const m = (a,b) => metric("pts", "Points", stat(a,50), stat(b,51));
  assert.equal(comparisonLead(m(0.2,0.3)), null);
  assert.equal(comparisonLead(m(0.2,0.301)), 1);
  assert.equal(comparisonLead(m(10,10.5)), null);
  assert.equal(comparisonLead(m(10,10.6)), 1);
  assert.equal(comparisonLead(m(0.051,0.149)), null); // same visible 0.1
  assert.equal(comparisonLead(m(0,0)), null);
  assert.equal(comparisonStatus(m(0,0)), "Similar rates");
  const missing=m(1,3); missing.left.missing_component_games=1;
  assert.equal(comparisonLead(missing), null);
  assert.equal(comparisonStatus(missing), "Incomplete data");
});

test("per-minute and efficiency text comes only from the server detail sections", () => {
  const view = comparisonPresentation(fixture());
  assert.match(view.areas[0].text, /Per 36 minutes, Brunson scores 26\.6 and Maxey 25\.6/);
  assert.match(view.areas[0].text, /per-game scoring edge comes with more minutes/);
  assert.match(view.areas[0].text, /True shooting: 58\.1% vs 56\.0%/);
  const old = fixture();
  delete old.semantic_evidence.comparison_sections;
  const legacy = comparisonPresentation(old);
  assert.doesNotMatch(legacy.areas[0].text, /Per 36|True shooting/);
  assert.deepEqual(legacy.areas[0].rows.map((r) => r.label), ["PTS", "AST"]);
  assert.doesNotMatch(comparisonTables(legacy), /comparison-details/);
});

test("one player leading points and assists is named once", () => {
  const data = fixture();
  data.semantic_evidence.metrics[0] = metric("pts", "Points", stat(30.5, 99), stat(27.6, 95));
  assert.match(comparisonPresentation(data).areas[0].text, /^Brunson averages 2\.9 more points and 0\.7 more assists\./);
});

test("missing components withhold edges, meters and leans instead of becoming zero", () => {
  const data = fixture();
  for (const m of data.semantic_evidence.metrics) {
    m.left = { ...m.left, missing_component_games: 1, percentile: null, valid_games: 5 };
    m.delta = null;
  }
  data.player_profiles[0].minutes = null;
  data.player_profiles[0].minutes_per_game = null;
  const view = comparisonPresentation(data);
  assert.ok(view.areas.every((a) => !("lean" in a)));
  const table = comparisonTables(view);
  assert.match(table, /<span class="h2h-num">26\.0<small>Partial: 5\/6 games<\/small><\/span>/);
  assert.doesNotMatch(table, /h2h-edge|is-leader/);
  assert.match(table, /Not ranked/);
});

test("head-to-head markup carries the exact text each claim expects", () => {
  const view = comparisonPresentation(fixture());
  const table = comparisonTables(view);
  const schema = claimSchema(view.claimEvidence, "comparison");
  const edges = schema.filter((r) => r.format === "comparison-margin");
  // Only gaps outside the similarity band have a visible bound margin.
  assert.equal(edges.length, 4);
  assert.equal(expectedClaim(view.claimEvidence, edges[0]), "+1.4");
  assert.equal((table.match(/class="h2h-edge"/g) || []).length, 4);
  assert.match(table, /<td class="h2h-value h2h-value-b is-leader"><span class="h2h-num">27\.4<\/span><small class="h2h-edge">\+1\.4<\/small>/);
  for (const r of schema.filter((d) => d.format === "comparison-value"))
    assert.ok(table.includes(`<span class="h2h-num">${expectedClaim(view.claimEvidence, r)}</span>`));
  for (const r of schema.filter((d) => d.format === "meter"))
    assert.ok(table.includes(`value="${expectedClaim(view.claimEvidence, r)}"`));
  assert.ok(schema.some((r) => r.selector === ".h2h-table thead th.h2h-name-b"));
  assert.ok(schema.every((r) => !r.selector.includes("comparison-table")));
});

test("saved missing-feed prose is hidden without discarding ordinary caveats", () => {
  const payload = fixture();
  const ordinary = "A small sample limits the comparison.";
  payload.assumptions = [ordinary, "Win Shares and on/off possession data are not connected. Raw plus/minus is not substituted for net rating. No illustrative values are used."];
  const before = JSON.stringify(payload);
  const view = comparisonPresentation(payload);
  assert.doesNotMatch(comparisonAnswer(view), /Win Shares|possession/);
  assert.deepEqual(comparisonAssumptions(payload), [ordinary]);
  assert.equal(JSON.stringify(payload), before);
  assert.deepEqual(comparisonAssumptions({ ...payload, semantic_evidence: { kind: "unsupported" } }), payload.assumptions);
});

test("impact rows return only with numeric data for both players, including valid zero", () => {
  const payload = fixture();
  payload.semantic_evidence.impact[0].left = 0;
  assert.doesNotMatch(comparisonTables(comparisonPresentation(payload)), /Win Shares/);
  payload.semantic_evidence.impact[0].right = 2;
  const table = comparisonTables(comparisonPresentation(payload));
  assert.match(table, /Win Shares/);
  assert.match(table, /<td>0.0<\/td><td>2.0<\/td>/);
  assert.match(table, /Estimated &lt;b&gt;contribution/);
});

test("server detail sections stay available behind a disclosure", () => {
  const payload = fixture();
  payload.semantic_evidence.comparison_sections[0].metrics[0].left.missing_component_games = 1;
  payload.semantic_evidence.comparison_sections[0].metrics[0].comparison = "Comparison withheld: incomplete inputs";
  const html = comparisonTables(comparisonPresentation(payload));
  assert.match(html, /<details class="comparison-details"><summary>Shooting, ball security &amp; per-36 detail/);
  assert.match(html, /data-comparison-section-index="1"/);
  assert.match(html, /partial inputs/);
  assert.match(html, /Comparison withheld/);
});

test("identical initials fall back to surnames in compact labels", () => {
  const data = fixture();
  data.player_profiles[1].player.player_name = "Jaylen Brown";
  assert.deepEqual(comparisonPresentation(data).tags, ["Brunson", "Brown"]);
});

test("old single-player payloads do not enter comparison rendering", () => {
  assert.equal(comparisonPresentation({}), null);
});

// Preserve detailed units/component coverage from the original public regression.
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
