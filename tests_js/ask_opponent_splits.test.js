import assert from "node:assert/strict";
import test from "node:test";
import { claimSchema, expectedClaim, prepareClaimEvidence, validateRenderedClaims } from "../app/static/ask_claim_bindings.js";
import { opponentSplitsPresentation } from "../app/static/ask_presentation.js";

const payload = {
  opponent_splits: {
    version: "opponent_splits/1",
    player: { player_id: 101, player_name: "Avery Example" },
    scope: { season: "2024-25" },
    display_metrics: ["ts_pct"],
    aggregation: "average",
    metric_definitions: { ts_pct: { label: "TS%", unit: "ratio" } },
    opponents: [{
      opponent_abbr: "BOS", games: 2, wins: 1, losses: 1, unknown_outcomes: 0,
      struggle_score: 0.125, sample_label: "Small sample",
      metrics: { ts_pct: { value: 0.60125, difference: -12.32 } },
    }],
    toughest_opponent_games: [{
      game_date: "2025-01-05", game_id: "02", team_abbr: "NYK",
      opponent_abbr: "BOS", game_result: "L", metrics: { ts_pct: { value: null } },
    }],
  },
  tables: [
    { title: "Opponent splits", columns: ["Opponent", "Games", "W–L", "Struggle score", "Sample", "TS%"].map(label => ({label})),
      rows: [["BOS", 2, "1–1", "0.12", "Small sample", "60.12%"]] },
    { title: "Change from the player's baseline", columns: [{ label: "Opponent" }, { label: "TS%" }], rows: [["BOS", "-12.32 pp"]] },
    { title: "Games against the toughest opponent", columns: ["Date", "Game ID", "Team", "Opponent", "Result", "TS%"].map(label => ({label})),
      rows: [["2025-01-05", "02", "NYK", "BOS", "L", "Unavailable"]] },
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
  const claim = (ti, ri, ci) => schema.find(r =>
    r.selector === `[data-opponent-split-table="${ti}"] tbody tr:nth-child(${ri + 1}) td:nth-child(${ci + 1})`);
  assert.equal(expectedClaim(snapshot, claim(0, 0, 2)), "1–1");
  assert.equal(expectedClaim(snapshot, claim(0, 0, 3)), "0.12");
  assert.equal(expectedClaim(snapshot, claim(1, 0, 1)), "-12.32 pp");
  assert.equal(expectedClaim(snapshot, claim(2, 0, 4)), "L");
  assert.deepEqual(claim(2, 0, 4).source, ["opponent_splits", "toughest_opponent_games", 0, "game_result"]);
});

function validateTables(renderedPayload, {
  extraRow = false,
  extraCell = false,
  extraTable = false,
  extraTfootRow = false,
  extraDirectTr = false,
  extraThInTbody = false,
} = {}) {
  const component = "opponent_splits";
  const evidence = prepareClaimEvidence(renderedPayload);
  const descriptors = claimSchema(evidence, component);
  const nodes = new Map();
  const counts = new Map();
  for (const descriptor of descriptors) {
    const [, tableIndex, selector] = descriptor.selector.match(/^\[data-opponent-split-table="(\d+)"\] (.+)$/);
    const table = renderedPayload.tables[tableIndex];
    const column = selector.match(/^thead th:nth-child\((\d+)\)$/);
    const cell = selector.match(/^tbody tr:nth-child\((\d+)\) td:nth-child\((\d+)\)$/);
    const value = cell ? table.rows[cell[1] - 1][cell[2] - 1]
      : column ? table.columns[column[1] - 1].label : table.title;
    nodes.set(descriptor.selector, {
      textContent: String(value),
      getAttribute: () => descriptor.id,
    });
  }
  const tables = renderedPayload.tables;
  const totalBodyRows = tables.reduce((sum, table) => sum + table.rows.length, 0);
  const totalCols = tables.reduce((sum, table) => sum + table.columns.length, 0);
  const totalBodyCells = tables.reduce((sum, table) => sum + table.columns.length * table.rows.length, 0);
  counts.set("[data-opponent-split-table]", tables.length + (extraTable ? 1 : 0));
  counts.set("table", tables.length + (extraTable ? 1 : 0));
  counts.set("tr", tables.length + totalBodyRows + (extraRow || extraTfootRow || extraDirectTr ? 1 : 0));
  counts.set("tbody tr", totalBodyRows + (extraRow ? 1 : 0));
  counts.set("tfoot, tfoot tr", extraTfootRow ? 1 : 0);
  counts.set("th", totalCols + (extraThInTbody ? 1 : 0));
  counts.set("td", totalBodyCells + (extraCell || extraTfootRow ? 1 : 0));
  tables.forEach((table, ti) => {
    const panel = `[data-opponent-split-table="${ti}"]`;
    const isZero = ti === 0;
    const tableCounts = new Map([
      ["tr", 1 + table.rows.length + (isZero && (extraRow || extraTfootRow || extraDirectTr) ? 1 : 0)],
      ["tfoot, tfoot tr", isZero && extraTfootRow ? 1 : 0],
      ["thead", 1],
      ["tbody", 1],
      ["th", table.columns.length + (isZero && extraThInTbody ? 1 : 0)],
      ["td", table.columns.length * table.rows.length + (isZero && (extraCell || extraTfootRow) ? 1 : 0)],
    ]);
    counts.set(`${panel} table`, [{
      querySelectorAll: selector => Array.from({ length: tableCounts.get(selector) ?? 0 }, () => ({})),
    }]);
    counts.set(`${panel} table tr`, tableCounts.get("tr"));
    counts.set(`${panel} tfoot, ${panel} tfoot tr`, tableCounts.get("tfoot, tfoot tr"));
    counts.set(`${panel} thead`, 1);
    counts.set(`${panel} tbody`, 1);
    counts.set(`${panel} thead tr`, 1);
    counts.set(`${panel} thead th`, table.columns.length);
    counts.set(`${panel} thead td`, 0);
    counts.set(`${panel} tbody tr`, table.rows.length + (isZero && extraRow ? 1 : 0));
    counts.set(`${panel} table th`, tableCounts.get("th"));
    counts.set(`${panel} table td`, tableCounts.get("td"));
    table.rows.forEach((_, ri) => {
      counts.set(
        `${panel} tbody tr:nth-child(${ri + 1}) td`,
        table.columns.length + (extraCell && isZero && ri === 0 ? 1 : 0),
      );
      counts.set(`${panel} tbody tr:nth-child(${ri + 1}) th`, extraThInTbody && isZero && ri === 0 ? 1 : 0);
    });
  });
  const root = {
    querySelectorAll: selector => {
      if (selector === "[data-ui-claim]") return [...nodes.values()];
      if (counts.has(selector)) {
        const entry = counts.get(selector);
        return Array.isArray(entry) ? entry : Array.from({ length: entry }, () => ({}));
      }
      return nodes.has(selector) ? [nodes.get(selector)] : [];
    },
  };
  return validateRenderedClaims(root, renderedPayload, {component, evidence, descriptors});
}

test("opponent split validation rejects a corrupt preformatted games cell", () => {
  assert.equal(validateTables(payload).status, "complete");
  const corrupted = structuredClone(payload);
  corrupted.tables[0].rows[0][1] = 999;
  const report = validateTables(corrupted);
  assert.equal(report.status, "invalid");
  assert.equal(report.errors.length, 1);
  assert.deepEqual(report.errors[0].source, ["opponent_splits", "opponents", 0, "games"]);
  assert.equal(report.errors[0].reason, "Rendered destination contradicts canonical evidence");
});

test("opponent split validation rejects a corrupt Table 1 opponent cell", () => {
  const corrupted = structuredClone(payload);
  corrupted.tables[1].rows[0][0] = "LAL";
  const report = validateTables(corrupted);
  assert.equal(report.status, "invalid");
  assert.equal(report.errors.length, 1);
  assert.deepEqual(report.errors[0].source, ["opponent_splits", "opponents", 0, "opponent_abbr"]);
  assert.equal(report.errors[0].reason, "Rendered destination contradicts canonical evidence");
});

test("opponent split validation rejects extra unbound DOM rows, cells, tfoot rows, or tables", () => {
  for (const mutation of [
    { extraRow: true },
    { extraCell: true },
    { extraTable: true },
    { extraTfootRow: true },
    { extraDirectTr: true },
    { extraThInTbody: true },
  ]) {
    const report = validateTables(payload, mutation);
    assert.equal(report.status, "invalid");
    assert.match(report.errors[0].reason, /table structure contradicts canonical evidence/i);
  }
});
