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

test("areas lean by percentile gaps and minutes, with the documented even band", () => {
  const view = comparisonPresentation(fixture());
  const [offense, defense, availability] = view.areas;
  assert.deepEqual(view.areas.map((a) => a.title), ["Offense", "Defense", "Availability"]);
  // Offense: (92-95 + 93-89) / 2 = 0.5 percentile points, inside the even band.
  assert.equal(offense.lean.value, 0);
  // Defense: (2 - 51 - 33) / 3 ≈ -27.3, strongly favoring the second player.
  assert.ok(defense.lean.value > 0.8);
  assert.match(defense.lean.label, /Favors Maxey, strongly/);
  // Availability: 30 of 210 minutes ≈ 14%, a partial lean to the second player.
  assert.ok(availability.lean.value > 0 && availability.lean.value < 1);
  assert.match(view.verdict.text, /Maxey leads defense and availability; offense is even/);
  const answer = comparisonAnswer(view);
  assert.match(answer, /role="img" aria-label="Favors Maxey, strongly"/);
  assert.match(answer, /overall winner/);
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
  data.semantic_evidence.metrics[0] = metric("pts", "Points", stat(28.5, 99), stat(27.6, 95));
  assert.match(comparisonPresentation(data).areas[0].text, /^Brunson averages 0\.9 more points and 0\.7 more assists\./);
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
  assert.equal(view.areas[0].lean.value, null);
  assert.equal(view.areas[1].lean.value, null);
  assert.equal(view.areas[2].lean.value, 0); // equal games played
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
  // Every non-zero delta has exactly one margin destination, shown on the leader.
  assert.equal(edges.length, 5);
  assert.equal(expectedClaim(view.claimEvidence, edges[0]), "+1.4");
  assert.equal((table.match(/class="h2h-edge"/g) || []).length, 5);
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
