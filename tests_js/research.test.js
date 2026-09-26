import test from "node:test";
import assert from "node:assert/strict";
import { METRICS, displayValue, readScope } from "../app/static/research.js";

test("missing results are not zero and percentages are not rescaled twice", () => {
  assert.equal(displayValue(null), "Unavailable");
  assert.equal(displayValue(0), "0.0");
  assert.equal(displayValue(45.5), "45.5");
});
test("all key metrics and explicit filters travel in the shared request", () => {
  const fields = {
    player: "2544",
    compare: "1628973",
    phase: "Regular Season",
    start: "2025-11-01",
    end: "2025-12-01",
    opponent: "bos",
    home_away: "away",
    rest: "one_day",
    aggregation: "average",
    teammate: "",
    teammate_status: "",
  };
  const scope = readScope(
    { elements: { namedItem: (k) => ({ value: fields[k] }) } },
    "2025-26",
  );
  assert.deepEqual(scope.player_ids, [2544, 1628973]);
  assert.equal(scope.opponent, "BOS");
  assert.equal(scope.start, "2025-11-01");
  assert.equal(scope.rest, "one_day");
  assert.equal(scope.teammate_id, null);
  assert.equal(METRICS.length, 19);
});
