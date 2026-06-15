import test from "node:test";
import assert from "node:assert/strict";

import {
  TRACKING_CAP,
  TRACKING_KEY,
  addTrackedPlayer,
  buildCompareHref,
  formatHealthStatusText,
  formatSeasonCoverage,
  loadTrackedPlayers,
  normalizeTrackedPayload,
  parseTrackedPayload,
  removeTrackedPlayer,
  serializeTrackedPayload,
} from "../app/static/workbench.js";

function createStorage(initialValue = null) {
  const store = new Map();
  if (initialValue !== null) {
    store.set(TRACKING_KEY, initialValue);
  }
  return {
    getItem(key) {
      return store.has(key) ? store.get(key) : null;
    },
    setItem(key, value) {
      store.set(key, value);
    },
  };
}

test("normalizeTrackedPayload keeps valid payloads", () => {
  const payload = normalizeTrackedPayload({ version: 1, player_ids: [7, 9] });
  assert.deepEqual(payload, { version: 1, player_ids: [7, 9] });
});

test("parseTrackedPayload resets invalid version", () => {
  const payload = parseTrackedPayload(JSON.stringify({ version: 2, player_ids: [7] }));
  assert.deepEqual(payload, { version: 1, player_ids: [] });
});

test("parseTrackedPayload resets oversized payloads", () => {
  const payload = parseTrackedPayload(
    JSON.stringify({ version: 1, player_ids: Array.from({ length: TRACKING_CAP + 1 }, (_, idx) => idx + 1) })
  );
  assert.deepEqual(payload, { version: 1, player_ids: [] });
});

test("loadTrackedPlayers rewrites invalid storage to empty v1 payload", () => {
  const storage = createStorage("{bad json");
  const payload = loadTrackedPlayers(storage);
  assert.deepEqual(payload, { version: 1, player_ids: [] });
  assert.equal(storage.getItem(TRACKING_KEY), serializeTrackedPayload(payload));
});

test("addTrackedPlayer dedupes repeated IDs", () => {
  const payload = { version: 1, player_ids: [7] };
  assert.deepEqual(addTrackedPlayer(payload, 7).payload, payload);
});

test("addTrackedPlayer enforces cap", () => {
  const payload = { version: 1, player_ids: Array.from({ length: TRACKING_CAP }, (_, idx) => idx + 1) };
  const result = addTrackedPlayer(payload, 99);
  assert.equal(result.error, "cap_reached");
  assert.equal(result.payload.player_ids.length, TRACKING_CAP);
});

test("removeTrackedPlayer removes only the selected ID", () => {
  const payload = removeTrackedPlayer({ version: 1, player_ids: [7, 9, 12] }, 9);
  assert.deepEqual(payload, { version: 1, player_ids: [7, 12] });
});

test("buildCompareHref preserves window and focus in compare links", () => {
  const href = buildCompareHref(7, 9, "last_7", "scoring");
  assert.equal(href, "/compare?player_a_id=7&player_b_id=9&window=last_7&focus=scoring");
});

test("formatSeasonCoverage labels full 2025-26 season coverage", () => {
  assert.equal(
    formatSeasonCoverage({
      season: "2025-26",
      season_types: ["Regular Season", "Playoffs"],
      is_full_season: true,
    }),
    "2025-26 full season"
  );
});

test("formatHealthStatusText combines last refresh and season coverage", () => {
  const payload = {
    status: "fresh",
    last_successful_finished_at_utc: new Date(Date.now() - 25 * 60 * 60 * 1000).toISOString(),
    season_coverage: {
      season: "2025-26",
      season_types: ["Regular Season", "Playoffs"],
      is_full_season: true,
    },
  };

  assert.match(
    formatHealthStatusText(payload, "season-coverage"),
    /^Last refresh 1 day ago - 2025-26 full season$/
  );
});
