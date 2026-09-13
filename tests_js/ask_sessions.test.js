import test from "node:test";
import assert from "node:assert/strict";
import {
  normalizeTabs,
  openTab,
  closeTab,
  matchesSession,
} from "../app/static/ask_sessions.js";
import {
  overviewPresentation,
  ordinal,
} from "../app/static/ask_presentation.js";

test("five open tabs overflow without mutating saved sessions or reordering switches", () => {
  let state = normalizeTabs();
  for (let i = 0; i < 7; i++) state = openTab(state, `c${i}`);
  assert.deepEqual(state.open, ["c2", "c3", "c4", "c5", "c6"]);
  const switched = openTab(state, "c3");
  assert.deepEqual(switched.open, state.open);
  assert.equal(switched.active, "c3");
  assert.deepEqual(closeTab(switched, "c3").open, ["c2", "c4", "c5", "c6"]);
  assert.equal(closeTab(switched, "c3").active, "c2");
  assert.equal(closeTab(openTab({}, "one"), "one").active, null);
});
test("tab normalization and search tolerate malformed data and match follow-ups", () => {
  assert.deepEqual(
    normalizeTabs({ open: ["a", null, "a", 4], active: "missing" }).open,
    ["a"],
  );
  const chat = {
    title: "Jalen Johnson",
    turns: [{ question: "How about assists?" }],
  };
  assert.equal(matchesSession(chat, "JOHNSON assists"), true);
  assert.equal(matchesSession(chat, "Curry"), false);
});
const metric = (key, value, percentile) => ({
  key,
  label: {
    pts: "Points",
    reb: "Rebounds",
    ast: "Assists",
    stl: "Steals",
    blk: "Blocks",
  }[key],
  value,
  percentile,
  cohort_size: 10,
  valid_games: 12,
  missing_component_games: 0,
  change: 1,
  monthly: [
    { x: "2025-11", y: value * 2, meta: "6 / 6 games" },
    { x: "2025-12", y: value, meta: "6 / 6 games" },
  ],
});
const payload = () => ({
  player_profile: {
    player: { player_name: "Fixture Player", games_sampled: 12 },
  },
  semantic_evidence: {
    scope: {
      start: "2025-09-13",
      end: "2026-09-12",
      previous_start: "2024-09-13",
      previous_end: "2025-09-12",
      phases: ["Regular Season", "Playoffs"],
    },
    metrics: [
      metric("pts", 20, 90),
      metric("reb", 10, 85),
      metric("ast", 8, 99),
      metric("stl", 1, 60),
      metric("blk", 0.5, 45),
    ],
  },
});
test("overview provides three evidence-backed areas, explicit scope and no causal claim", () => {
  const overview = overviewPresentation(payload());
  assert.equal(overview.insights.length, 3);
  assert.deepEqual(
    overview.insights.map((i) => i.area),
    ["Playmaking", "Trajectory", "Defense"],
  );
  assert.match(overview.comparison, /Sep 13, 2024/);
  assert.match(overview.paragraphs.join(" "), /have not been evaluated/);
  assert.match(overview.followups[0], /Fixture Player.*2025-09-13.*2026-09-12/);
  assert.equal(overview.preferredMetric, "ast");
});
test("partial metric data never creates a percentile or trend insight", () => {
  const data = payload();
  data.semantic_evidence.metrics[2].missing_component_games = 1;
  const overview = overviewPresentation(data);
  assert.notEqual(overview.preferredMetric, "ast");
  assert.match(overview.paragraphs.join(" "), /Partial data for Assists/);
  assert.equal(overviewPresentation({ answer: "generic" }), null);
  assert.equal(ordinal(11), "11th");
  assert.equal(ordinal(21), "21st");
});
