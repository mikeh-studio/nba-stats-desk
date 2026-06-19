import test from "node:test";
import assert from "node:assert/strict";

async function loadPerformanceModule() {
  globalThis.window = { __NBA_PERFORMANCE_TEST_HOOKS__: true };
  globalThis.document = {
    addEventListener() {},
    querySelector() {
      return null;
    },
    querySelectorAll() {
      return [];
    },
  };
  await import(`../app/static/performance.js?test=${Date.now()}-${Math.random()}`);
  return globalThis.__performanceTest;
}

test("P-Rating sort uses raw high-to-low values, not absolute magnitude", async () => {
  const performance = await loadPerformanceModule();
  performance.state.sortKey = "score";
  performance.state.sortDir = "desc";

  const rows = [
    { player_name: "Large negative", performance_score: -8.5 },
    { player_name: "Small positive", performance_score: 1.2 },
    { player_name: "Large positive", performance_score: 5.4 },
  ].sort(performance.comparePlayers);

  assert.deepEqual(
    rows.map((row) => row.player_name),
    ["Large positive", "Small positive", "Large negative"]
  );
});

test("P-Rating can toggle to low-to-high with raw positive and negative values", async () => {
  const performance = await loadPerformanceModule();
  performance.state.sortKey = "score";
  performance.state.sortDir = "asc";

  const rows = [
    { player_name: "Negative", performance_score: -2.1 },
    { player_name: "Positive", performance_score: 2.1 },
    { player_name: "Near", performance_score: 0.3 },
  ].sort(performance.comparePlayers);

  assert.deepEqual(
    rows.map((row) => row.player_name),
    ["Negative", "Near", "Positive"]
  );
});
