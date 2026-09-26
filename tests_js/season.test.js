import test from "node:test";
import assert from "node:assert/strict";
import { withSeason, selectedSeason, seasonFetch } from "../app/static/season.js";

test("historical navigation preserves filters and leaves external links alone", () => {
  assert.equal(withSeason("/compare?player_a_id=1#chart", "2023-24"), "/compare?player_a_id=1&season=2023-24#chart");
  assert.equal(withSeason("https://nba.com/player/1", "2023-24"), "https://nba.com/player/1");
  assert.equal(withSeason("#chart", "2023-24"), "#chart");
  assert.equal(withSeason("/api/health", "2025-26"), "/api/health");
});

test("tabs and API fetch default to latest despite stale historical URLs", async () => {
  const previousLocation = globalThis.location;
  const previousFetch = globalThis.fetch;
  globalThis.location = { href: "http://localhost/performance?season=2024-25" };
  globalThis.fetch = async (url, options) => ({ url, options });
  try {
    assert.equal(selectedSeason(), "2025-26");
    const response = await seasonFetch("/api/performance/initial", { cache: "default" });
    assert.equal(response.url, "/api/performance/initial");
    assert.equal(withSeason("/ask?season=2023-24&chat=abc"), "/ask?chat=abc");
    assert.deepEqual(response.options, { cache: "default" });
  } finally {
    globalThis.location = previousLocation;
    globalThis.fetch = previousFetch;
  }
});
