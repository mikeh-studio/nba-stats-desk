import { test, expect } from "@playwright/test";
import fs from "node:fs";
import { spawn } from "node:child_process";
import {
  overviewFixture,
  comparisonFixture,
} from "../tests_js/ask_claim_fixture.js";
const staticVersion = fs
  .readFileSync("app/main.py", "utf8")
  .match(/STATIC_VERSION = "([^"]+)"/)[1];
test.beforeEach(async ({ page }) => {
  await page.addInitScript((version) => {
    window.__claimAssetVersion = version;
    window.__NBA_ASK_TEST_HOOKS__ = true;
  }, staticVersion);
});
let logServer,
  serverEvents = "";
const logUrl = "http://127.0.0.1:8137";
test.beforeAll(async () => {
  logServer = spawn(
    process.env.ASK_TEST_PYTHON || "python",
    [
      "-m",
      "uvicorn",
      "scripts.ask_eval.server:app",
      "--host",
      "127.0.0.1",
      "--port",
      "8137",
      "--no-proxy-headers",
    ],
    {
      env: {
        ...process.env,
        ASK_EVAL_LIVE: "0",
        ASK_EVAL_CLAIM_ENFORCEMENT: "log",
      },
    },
  );
  logServer.stderr.on("data", (chunk) => {
    serverEvents = (serverEvents + chunk.toString()).slice(-65536);
  });
  await expect
    .poll(
      async () => {
        try {
          return (await fetch(logUrl + "/ask")).status;
        } catch {
          return 0;
        }
      },
      { timeout: 30000 },
    )
    .toBe(200);
});
test.afterAll(() => logServer?.kill());
for (const mode of ["log", "enforce"]) {
  test(`current ${mode} mode validates DOM, preserves first-invalid transition and real ingestion`, async ({
    page,
  }) => {
    await page.goto(mode === "log" ? logUrl + "/ask" : "/ask");
    const deliveries = [];
    page.on("response", async (r) => {
      if (r.url().endsWith("/claim-observations"))
        deliveries.push({ status: r.status(), body: await r.json() });
    });
    const result = await page.evaluate(
      async ({ payload, mode }) => {
        payload.claim_validation = {
          status: "incomplete",
          mode: mode === "log" ? "enforce" : "log",
        };
        payload.ask_route = { route: "player_overview" };
        const c = await import(
          "/static/ask_claim_bindings.js?v=" + window.__claimAssetVersion
        );
        const p = await import(
          "/static/ask_presentation.js?v=" + window.__claimAssetVersion
        );
        const rollout = await import(
          "/static/ask_claim_rollout.js?v=" + window.__claimAssetVersion
        );
        const root = document.createElement("div");
        document.body.append(root);
        root.innerHTML = p.overviewMetricsTable(
          p.overviewPresentation(payload),
        );
        c.bindRenderedClaims(root, payload, "overview");
        rollout.flushClaimObservations();
        root.querySelector("[data-ui-claim]").textContent = "999";
        const report = c.enforceRenderedClaims(root, payload);
        // Repeated redraw/hover of this surface must not reset its denominator.
        for (let i = 0; i < 5; i++) c.enforceRenderedClaims(root, payload);
        rollout.flushClaimObservations();
        return {
          mode: rollout.claimMode(),
          status: report.status,
          text: root.textContent,
          attempts: Object.values(document.uiClaimFailureCounts || {}).reduce(
            (a, b) => a + b,
            0,
          ),
        };
      },
      { payload: overviewFixture(), mode },
    );
    expect(result.mode).toBe(mode);
    expect(result.status).toBe("invalid");
    expect(result.attempts).toBe(6);
    expect(result.text.includes("999")).toBe(mode === "log");
    await expect.poll(() => deliveries.length).toBe(2);
    expect(deliveries.map((d) => d.status)).toEqual([200, 200]);
    expect(deliveries.map((d) => d.body.accepted)).toEqual([1, 1]);
    if (mode === "log") {
      await expect
        .poll(() => serverEvents.includes('"kind": "first_invalid"'))
        .toBe(true);
      expect(serverEvents).toContain('"route": "player_overview"');
      const records = serverEvents
        .split("\n")
        .filter((line) => line.startsWith("ask_claim_observation "))
        .map((line) => JSON.parse(line.slice("ask_claim_observation ".length)));
      expect(
        records.every((e) => !JSON.stringify(e).includes("SAVED_BAD_ANSWER")),
      ).toBe(true);
      expect(
        records.every(
          (e) =>
            Object.keys(e).sort().join(",") ===
            [
              "version",
              "origin",
              "kind",
              "route",
              "surface",
              "status",
              "reason",
              "contract",
              "mode",
              "assessed",
              "invalid",
            ]
              .sort()
              .join(","),
        ),
      ).toBe(true);
    }
  });
  test(`restored known backend-invalid payload obeys current ${mode} not saved mode`, async ({
    page,
  }) => {
    await page.route("**/api/agent/ask**", async (route) => {
      const req = route.request();
      await route.continue({
        url: new URL("/api/agent/ask/stream?season=2024-25", req.url()).href,
        headers: {
          ...req.headers(),
          "X-Evaluation-Case": "points_mean",
          "X-Evaluation-Turn": "0",
        },
      });
    });
    await page.goto(mode === "log" ? logUrl + "/ask" : "/ask");
    await page
      .locator("[data-agent-question]")
      .fill("Avery Example points per game in 2024-25");
    await page.locator("[data-agent-submit]").click();
    await expect(page.locator("[data-agent-tables] table")).toHaveCount(1);
    await page.evaluate(
      ({ mode }) => {
        const key = "askChatHistory:v1",
          history = JSON.parse(localStorage.getItem(key));
        const payload = history.conversations[0].turns[0].payload;
        payload.status = "error";
        payload.answer = "SAVED_BAD_ANSWER";
        payload.tables[0].rows[0][1] = "SAVED_BAD_TABLE";
        payload.assumptions = ["SAVED_BAD_ASSUMPTION"];
        payload.scope_assumptions = ["SAVED_BAD_ASSUMPTION"];
        payload.claim_validation = {
          status: "invalid",
          mode: mode === "log" ? "enforce" : "log",
        };
        localStorage.setItem(key, JSON.stringify(history));
      },
      { mode },
    );
    await page.reload();
    await expect(page.locator("[data-agent-answer]")).toContainText(
      mode === "log" ? "SAVED_BAD_ANSWER" : "claim_binding_mismatch",
    );
    const text = await page.locator("main").innerText();
    expect(text.includes("SAVED_BAD_TABLE")).toBe(mode === "log");
    expect(text.includes("SAVED_BAD_ASSUMPTION")).toBe(mode === "log");
    expect(text.includes("SAVED_BAD_ANSWER")).toBe(mode === "log");
    if (mode === "enforce") {
      expect(text).toContain("This answer could not be verified");
      expect(text).not.toContain("This saved answer");
    }
  });
}

test("log mode retains invalid drawer interaction, game selection and close", async ({
  page,
}) => {
  await page.goto(logUrl + "/ask");
  await page.evaluate(async (payload) => {
    const p = await import(
      "/static/ask_presentation.js?v=" + window.__claimAssetVersion
    );
    const d = await import(
      "/static/metric_drawer.js?v=" + window.__claimAssetVersion
    );
    const a = await import(
      "/static/ask_claim_bindings.js?v=" + window.__claimAssetVersion
    );
    const root = document.createElement("div");
    root.id = "rollout-drawer";
    document.body.append(root);
    const presentation = p.overviewPresentation(payload);
    root.innerHTML = p.overviewMetricsTable(presentation);
    a.bindRenderedClaims(
      root,
      payload,
      "overview",
      {},
      presentation.claimEvidence,
    );
    d.bindMetricDrawer(root, presentation);
    // Retain the old canonical snapshot while changing current payload evidence.
    payload.semantic_evidence.metrics[0].value = 999;
  }, overviewFixture());
  await page
    .locator('#rollout-drawer [data-metric-distribution="pts"]')
    .click();
  const dialog = page.locator("dialog");
  await expect(dialog).toBeVisible();
  await expect
    .poll(() => dialog.evaluate((d) => d.uiClaimReport.status))
    .toBe("invalid");
  await dialog.locator('[data-distribution-view="across_games"]').click();
  await dialog.locator("circle").first().focus();
  await expect(dialog.locator("[data-game-point-info]")).toContainText("0.0");
  await dialog.locator("[data-drawer-close]").click();
  await expect(dialog).not.toBeVisible();
});

test("log comparison chart retains metric selection after a contradiction", async ({
  page,
}) => {
  await page.goto(logUrl + "/ask");
  await page.evaluate((payload) => {
    window.__rolloutComparison = payload;
    const root = document.createElement("article");
    root.innerHTML = window.__askAgentTest.turnEvidenceMarkup();
    document.body.append(root);
    window.__askAgentTest.renderAuxiliaryPayload(payload, root, false);
  }, comparisonFixture());
  await expect(page.locator("[data-comparison-metric]").first()).toBeVisible();
  const status = await page
    .evaluate(async () => {
      const root = document.querySelector("[data-agent-charts]");
      const claim = root.querySelector("[data-ui-claim]");
      claim.textContent = "contradicted chart label";
      const api = await import(
        "/static/ask_claim_bindings.js?v=" + window.__claimAssetVersion
      );
      // The session contains the evidence snapshot established by actual rendering.
      return api.enforceRenderedClaims(
        root,
        window.__rolloutComparison,
        root.uiClaimSession,
      ).status;
    })
    .catch(() => "fixture_setup_failed");
  expect(status).toBe("invalid");
  await page.locator("[data-comparison-metric]").last().click();
  await expect(page.locator("[data-comparison-metric]").last()).toHaveAttribute(
    "aria-pressed",
    "true",
  );
});

test("telemetry projection exception cannot prevent enforced DOM suppression", async ({
  page,
}) => {
  await page.goto("/ask");
  const result = await page.evaluate(async (payload) => {
    const a = await import(
      "/static/ask_claim_bindings.js?v=" + window.__claimAssetVersion
    );
    const p = await import(
      "/static/ask_presentation.js?v=" + window.__claimAssetVersion
    );
    const root = document.createElement("div");
    document.body.append(root);
    root.innerHTML = p.overviewMetricsTable(p.overviewPresentation(payload));
    a.bindRenderedClaims(root, payload, "overview");
    root.querySelector("[data-ui-claim]").textContent = "wrong";
    Object.defineProperty(payload, "ask_route", {
      get() {
        throw Error("synthetic telemetry failure");
      },
    });
    const report = a.enforceRenderedClaims(root, payload);
    return { status: report.status, text: root.textContent };
  }, overviewFixture());
  expect(result.status).toBe("invalid");
  expect(result.text).toContain("could not be verified");
});
