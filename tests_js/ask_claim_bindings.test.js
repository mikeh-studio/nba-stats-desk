import test from "node:test";
import assert from "node:assert/strict";
import { overviewFixture, comparisonFixture } from "./ask_claim_fixture.js";
import {
  claimSchema,
  prepareClaimEvidence,
  formatClaim,
  expectedClaim,
  claimSource,
} from "../app/static/ask_claim_bindings.js";
test("null, zero, percentage units and denominator counts have explicit distinct formats", () => {
  assert.equal(
    formatClaim(0, "overview-value", { metric: { unit: "per game" } }),
    "0.0",
  );
  assert.equal(
    formatClaim(null, "overview-value", { metric: { unit: "percent" } }),
    "—",
  );
  assert.equal(
    formatClaim(25, "overview-value", { metric: { unit: "percent" } }),
    "25.0%",
  );
  assert.equal(
    formatClaim(-0.001, "change", { metric: { unit: "per game" } }),
    "0.0",
  );
  assert.equal(
    formatClaim(10, "comparison-value", {
      metric: { missing_component_games: 1, valid_games: 1, observed_games: 2 },
    }),
    "10.0Partial: 1/2 games",
  );
  assert.equal(
    formatClaim(0, "comparison-value", {
      metric: { missing_component_games: 0, valid_games: 2, observed_games: 2 },
    }),
    "0.0",
  );
  assert.throws(() => formatClaim(1, "arbitrary-code"));
  assert.throws(() => claimSource({}, ["__proto__"]));
});
test("schema anchors reordered metrics and equal-valued player sides independently", () => {
  const p = overviewFixture(),
    s = claimSchema(p, "overview");
  assert.deepEqual(s.find((r) => r.format === "overview-value").source, [
    "semantic_evidence",
    "metrics",
    1,
    "value",
  ]);
  const q = comparisonFixture(),
    c = claimSchema(q, "comparison");
  const values = c.filter((r) => r.format === "comparison-value");
  assert.deepEqual(values[0].source, [
    "semantic_evidence",
    "metrics",
    0,
    "left",
    "value",
  ]);
  assert.deepEqual(values[1].source, [
    "semantic_evidence",
    "metrics",
    0,
    "right",
    "value",
  ]);
  assert.equal(expectedClaim(q, values[0]), "5.0");
  assert.equal(expectedClaim(q, values[1]), "5.0");
  assert.equal(values[0].context.side, "left");
  assert.equal(values[1].context.side, "right");
});
test("snapshot excludes binding metadata; descriptor size does not multiply full evidence", () => {
  const p = overviewFixture();
  p.claim_bindings = { large: "x".repeat(1200000) };
  const small = claimSchema(p, "overview");
  p.semantic_evidence.metrics[0].distribution.across_games.points = Array.from(
    { length: 10000 },
    (_, i) => ({ date: "2025-01-01", value: i }),
  );
  const large = claimSchema(p, "overview");
  assert.equal(JSON.stringify(small).length, JSON.stringify(large).length);
  assert.ok(JSON.stringify(large).length < 30000);
  assert.equal(prepareClaimEvidence(p).claim_bindings, undefined);
  for (const r of large) {
    assert.equal(r.context.evidence, undefined);
    assert.equal(r.context.metric, undefined);
    assert.equal(r.context.summary, undefined);
  }
});
test("original source and review metadata cannot be changed by snapshot creation", () => {
  const p = overviewFixture(),
    before = JSON.stringify(p),
    s = prepareClaimEvidence(p);
  s.semantic_evidence.metrics[0].value = 999;
  assert.equal(JSON.stringify(p), before);
});
