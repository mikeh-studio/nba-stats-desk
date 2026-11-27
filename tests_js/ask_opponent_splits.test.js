import assert from "node:assert/strict";
import test from "node:test";
import { claimSchema, expectedClaim, prepareClaimEvidence } from "../app/static/ask_claim_bindings.js";
import { opponentSplitsPresentation } from "../app/static/ask_presentation.js";

const payload = {
  opponent_splits: {
    version: "opponent_splits/1",
    player: { player_id: 101, player_name: "Avery Example" },
    scope: { season: "2024-25" },
  },
  tables: [
    { title: "Opponent splits", columns: [{ label: "Opponent" }, { label: "W–L" }], rows: [["BOS", "1–1"]] },
    { title: "Change from the player's baseline", columns: [{ label: "TS%" }], rows: [["-12.32 pp"]] },
    { title: "Games against the toughest opponent", columns: [{ label: "Date" }, { label: "Result" }], rows: [["2025-01-05", "L"]] },
  ],
};

test("opponent splits keep bound text and disclose deltas and game logs on demand", () => {
  const input = [];
  const markup = opponentSplitsPresentation(payload, table => {
    input.push(table);
    return "<table></table>";
  });
  assert.match(markup, /data-opponent-split-table="0"/);
  assert.match(markup, /data-opponent-split-table="2"/);
  assert.deepEqual(input.map(table => table.collapsible), [false, true, true]);
  assert.deepEqual(input.map(table => table.rows), payload.tables.map(table => table.rows));
  assert.equal(opponentSplitsPresentation({ tables: [] }, () => ""), null);
});

test("opponent split claim selectors preserve table and cell coordinates", () => {
  const snapshot = prepareClaimEvidence(payload);
  const schema = claimSchema(snapshot, "opponent_splits");
  const claim = (ti, ri, ci) => schema.find(r => r.source.join(".") === `tables.${ti}.rows.${ri}.${ci}`);
  assert.equal(expectedClaim(snapshot, claim(0, 0, 1)), "1–1");
  assert.equal(expectedClaim(snapshot, claim(1, 0, 0)), "-12.32 pp");
  assert.equal(expectedClaim(snapshot, claim(2, 0, 1)), "L");
  assert.equal(claim(2, 0, 1).selector, '[data-opponent-split-table="2"] tbody tr:nth-child(1) td:nth-child(2)');
  payload.tables[2].rows[0][1] = "W";
  assert.equal(expectedClaim(snapshot, claim(2, 0, 1)), "L");
});
