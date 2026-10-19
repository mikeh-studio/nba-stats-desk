import test from "node:test";
import assert from "node:assert/strict";
const turn = () => new Promise((resolve) => setTimeout(resolve, 10));

test("browser buffer and in-flight work stay bounded; delivery failures count local drops", async () => {
  const original = globalThis.fetch;
  let concurrent = 0,
    maximum = 0;
  const deliveries = [];
  globalThis.fetch = (_url, options) => {
    concurrent++;
    maximum = Math.max(maximum, concurrent);
    return new Promise((resolve, reject) =>
      deliveries.push({
        body: JSON.parse(options.body),
        resolve(value) {
          concurrent--;
          resolve(value);
        },
        reject() {
          concurrent--;
          reject(Error("offline"));
        },
      }),
    );
  };
  const api = await import("../app/static/ask_claim_rollout.js?bounded-test");
  const view = () =>
    api.observeClaims(
      {},
      {},
      { component: "overview" },
      { status: "incomplete", checked: 1, gaps: [] },
      "none",
    );
  try {
    for (let i = 0; i < 20; i++) view();
    assert.equal(api.claimObservationDrops(), 4);
    api.flushClaimObservations();
    await turn();
    assert.equal(deliveries.length, 1);
    assert.equal(deliveries[0].body.length, 16);
    for (let i = 0; i < 40; i++) view();
    api.flushClaimObservations();
    await turn();
    assert.equal(deliveries.length, 1);
    deliveries[0].resolve({ ok: false });
    await turn();
    assert.equal(deliveries.length, 2);
    deliveries[1].reject();
    await turn();
    assert.equal(maximum, 1);
    assert.equal(api.claimObservationDrops(), 60);
    // Even a hostile diagnostic accessor cannot throw into the renderer.
    const report = {
      get status() {
        throw Error("diagnostic failure");
      },
    };
    assert.doesNotThrow(() => api.observeClaims({}, {}, {}, report, "none"));
    assert.equal(api.claimObservationDrops(), 61);
  } finally {
    globalThis.fetch = original;
  }
});

test("consecutive display instances reset only on view changes and retain safe context", async () => {
  const original = globalThis.fetch,
    events = [];
  globalThis.fetch = async (_url, options) => {
    events.push(...JSON.parse(options.body));
    return { ok: true };
  };
  try {
    const api =
      await import("../app/static/ask_claim_rollout.js?instance-test");
    const root = {},
      payload = { ask_route: { route: "player_comparison" } };
    const view = (key, invalid = false) =>
      api.observeClaims(
        root,
        payload,
        { component: "drawer", options: { key } },
        { status: invalid ? "invalid" : "complete" },
        invalid ? "destination_mismatch" : "none",
      );
    view("A");
    view("B");
    view("A");
    view("A");
    view("A", true);
    view("A", true);
    api.flushClaimObservations();
    await turn();
    assert.deepEqual(
      events.map((e) => e.assessed),
      [1, 1, 1, 0],
    );
    assert.deepEqual(
      events.map((e) => e.invalid),
      [0, 0, 0, 1],
    );
    assert.ok(
      events.every(
        (e) =>
          e.route === "player_comparison" &&
          !("bound" in e) &&
          !("unbound" in e),
      ),
    );
    const safe = api.withheldClaimPayload({
      ...payload,
      conversation_id: "context",
      query_plan: { metric: "pts" },
      agent_plan: { route: "metrics" },
      tables: ["bad"],
      secret: "bad",
    });
    assert.equal(safe.conversation_id, "context");
    assert.deepEqual(safe.query_plan, { metric: "pts" });
    assert.deepEqual(safe.agent_plan, { route: "metrics" });
    assert.deepEqual(safe.ask_route, payload.ask_route);
    assert.deepEqual(safe.tables, []);
    assert.ok(!("secret" in safe));
    assert.match(safe.answer, /^This answer/);
  } finally {
    globalThis.fetch = original;
  }
});
