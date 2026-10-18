import { test, expect } from "@playwright/test";
import {
  overviewFixture,
  comparisonFixture,
} from "../tests_js/ask_claim_fixture.js";

async function setup(page) {
  await page.goto("/ask");
  await page.evaluate(
    async ({ overview, comparison }) => {
      const a = await import("/static/ask_claim_bindings.js"),
        p = await import("/static/ask_presentation.js"),
        c = await import("/static/ask_comparison.js"),
        d = await import("/static/metric_drawer.js");
      const root = document.createElement("div");
      root.id = "claim-test";
      document.body.replaceChildren(root);
      window.claimTest = { a, p, c, d, root, overview, comparison };
      window.paintClaim = (kind = "overview", options = {}) => {
        const t = window.claimTest,
          payload = kind.startsWith("comparison") ? comparison : overview;
        if (kind === "overview")
          root.innerHTML = p.overviewMetricsTable(
            p.overviewPresentation(payload),
          );
        if (kind === "comparison")
          root.innerHTML = c.comparisonTables(
            c.comparisonPresentation(payload),
          );
        if (kind === "comparison-chart")
          root.innerHTML = c.comparisonChart(
            c.comparisonPresentation(payload),
            options.key || "pts",
          );
        if (kind === "drawer")
          root.innerHTML = d.drawerContent(
            overview.semantic_evidence.metrics[0],
            p.overviewPresentation(overview),
            options.view || "among_players",
          );
        t.payload = payload;
        return a.bindRenderedClaims(root, payload, kind, options);
      };
    },
    { overview: overviewFixture(), comparison: comparisonFixture() },
  );
}
for (const component of [
  "overview",
  "comparison",
  "comparison-chart",
  "drawer",
])
  test(`actual ${component} leaves and source contexts are independently checked`, async ({
    page,
  }) => {
    await setup(page);
    const options =
      component === "drawer"
        ? { key: "pts", view: "among_players" }
        : component === "comparison-chart"
          ? { key: "pts" }
          : {};
    const report = await page.evaluate(
      ({ component, options }) => paintClaim(component, options),
      { component, options },
    );
    expect(report.errors).toEqual([]);
    expect(report.checked).toBeGreaterThan(8);
    expect(report.status).toBe("incomplete");
    const checks = await page.evaluate(
      ({ component, options }) => {
        const t = claimTest,
          results = [];
        const reset = () => paintClaim(component, options),
          verify = () =>
            t.a.validateRenderedClaims(t.root, t.payload, t.root.uiClaimSession)
              .status;
        let r = t.root.uiClaimSession.descriptors.find(
          (r) =>
            !r.attribute &&
            [
              "overview-value",
              "comparison-value",
              "chart-value",
              "drawer-value",
            ].includes(r.format),
        );
        t.root.querySelector(r.selector).textContent = "999";
        results.push(verify());
        reset();
        t.root.uiClaimSession.descriptors[0].source = [
          "semantic_evidence",
          "metrics",
          1,
          "value",
        ];
        results.push(verify());
        reset();
        t.root.uiClaimSession.descriptors[0].context.metricKey = "wrong";
        results.push(verify());
        reset();
        t.root.uiClaimSession.descriptors[0].scope.start = "1999-01-01";
        results.push(verify());
        reset();
        t.root.uiClaimSession.descriptors[0].identity = "different player";
        results.push(verify());
        reset();
        const target = t.root.querySelector("[data-ui-claim]");
        target.removeAttribute("data-ui-claim");
        results.push(verify());
        reset();
        const extra = document.createElement("span");
        extra.setAttribute("data-ui-claim", "unregistered");
        t.root.append(extra);
        results.push(verify());
        reset();
        const duplicate = t.root
          .querySelector("[data-ui-claim]")
          .cloneNode(true);
        t.root.append(duplicate);
        results.push(verify());
        reset();
        const old = t.payload.semantic_evidence.scope.start;
        t.payload.semantic_evidence.scope.start = "1999-01-01";
        results.push(verify());
        t.payload.semantic_evidence.scope.start = old;
        reset();
        return results;
      },
      { component, options },
    );
    expect(checks).toEqual(Array(9).fill("invalid"));
  });
test("equal-valued side swaps, labels, visual widths and fail-closed replacement", async ({
  page,
}) => {
  await setup(page);
  const result = await page.evaluate(() => {
    const t = claimTest;
    paintClaim("comparison");
    let s = t.root.uiClaimSession;
    const record = s.descriptors.find((r) => r.format === "comparison-value");
    record.source = ["semantic_evidence", "metrics", 0, "right", "value"];
    const swapped = t.a.validateRenderedClaims(t.root, t.payload, s).status;
    paintClaim("comparison");
    t.root.querySelector(".comparison-table thead th").textContent =
      "Wrong player";
    const identity = t.a.validateRenderedClaims(
      t.root,
      t.payload,
      t.root.uiClaimSession,
    ).status;
    paintClaim("comparison-chart", { key: "pts" });
    t.root.querySelector(".comparison-bar").style.width = "1%";
    const width = t.a.enforceRenderedClaims(t.root, t.payload).status;
    return { swapped, identity, width, text: t.root.textContent };
  });
  expect(result.swapped).toBe("invalid");
  expect(result.identity).toBe("invalid");
  expect(result.width).toBe("invalid");
  expect(result.text).toContain("could not be verified");
});
test("zero, null, partial denominator and drawer repaint remain usable", async ({
  page,
}) => {
  await setup(page);
  await page.evaluate(() => paintClaim());
  await expect(page.locator('[data-metric-key="fga"] td').first()).toHaveText(
    "0.0",
  );
  await page.locator("[data-additional-metrics] summary").click();
  await expect(
    page.locator('[data-metric-key="ft_pct"] td').first(),
  ).toHaveText("—");
  await page.evaluate(() => {
    const t = claimTest;
    t.d.bindMetricDrawer(t.root, t.p.overviewPresentation(t.overview));
  });
  await page.locator('[data-metric-distribution="pts"]').click();
  const dialog = page.locator("dialog");
  await expect(dialog).toBeVisible();
  expect(await dialog.evaluate((d) => d.uiClaimReport.errors)).toEqual([]);
  await dialog.locator('[data-distribution-view="across_games"]').click();
  expect(await dialog.evaluate((d) => d.uiClaimReport.errors)).toEqual([]);
  await expect(dialog.locator(".drawer-stats")).toContainText("2");
  await expect(dialog.locator("circle")).toHaveCount(2);
  await dialog.locator("[data-drawer-close]").click();
  await expect(dialog).not.toBeVisible();
});

test("invalid drawer retains accessible close and returns focus", async ({
  page,
}) => {
  await setup(page);
  await page.evaluate(() => {
    paintClaim();
    const t = claimTest;
    t.d.bindMetricDrawer(t.root, t.p.overviewPresentation(t.overview));
  });
  const trigger = page.locator('[data-metric-distribution="pts"]');
  await trigger.click();
  await page.evaluate(() => {
    const t = claimTest,
      d = document.querySelector("dialog");
    d.querySelector(".drawer-value strong").textContent = "999";
    t.a.enforceRenderedClaims(d, t.overview);
  });
  const dialog = page.locator("dialog");
  await expect(dialog).toContainText("could not be verified");
  await dialog
    .getByRole("button", { name: "Close metric details", exact: true })
    .click();
  await expect(dialog).not.toBeVisible();
  await expect(trigger).toBeFocused();
});

test("actual endpoint multi-answer and multi-tab history survives real browser storage", async ({
  page,
}, testInfo) => {
  await page.route("**/api/agent/ask**", async (route) => {
    const request = route.request();
    await route.continue({
      url: new URL(
        "/api/agent/" +
          (request.url().includes("/stream") ? "ask/stream" : "ask") +
          "?season=2024-25",
        request.url(),
      ).href,
      headers: {
        ...request.headers(),
        "X-Evaluation-Case": "balanced_overview",
        "X-Evaluation-Turn": "0",
      },
    });
  });
  await page.goto("/ask");
  for (let chat = 0; chat < 3; chat++) {
    if (chat)
      await page.getByRole("button", { name: "New chat", exact: true }).click();
    for (let turn = 0; turn < 2; turn++) {
      const question =
        "Give an overview of Avery Example in the 2024-25 regular season.";
      const input = page.locator(
        turn ? "[data-followup-question]" : "[data-agent-question]",
      );
      await input.fill(question);
      const response = page.waitForResponse((r) =>
        r.url().includes("/api/agent/ask/stream"),
      );
      await input.press("Enter");
      expect((await response).status()).toBe(200);
      await expect(page.locator("[data-agent-submit]")).toBeEnabled();
    }
  }
  const before = await page.evaluate(() => {
    const raw = localStorage.getItem("askChatHistory:v1"),
      history = JSON.parse(raw);
    return {
      bytes: new TextEncoder().encode(raw).length,
      counts: history.conversations.map((c) => c.turns.length),
      tabs: JSON.parse(
        localStorage.getItem(
          Object.keys(localStorage).find((k) => k.startsWith("askOpenTabs:v1")),
        ),
      ).open.length,
    };
  });
  await testInfo.attach("actual-localStorage-scenario", {
    body: JSON.stringify({
      scenario:
        "six balanced_overview endpoint answers, three app chats, second real browser page",
      ...before,
    }),
    contentType: "application/json",
  });
  expect(before.counts).toEqual([2, 2, 2]);
  expect(before.tabs).toBe(3);
  expect(before.bytes).toBeLessThan(4 * 1024 * 1024);
  await page.reload();
  await expect(page.locator("[data-chat-tabs] [role=tab]")).toHaveCount(3);
  await expect(page.locator("[data-agent-tables]").last()).toContainText("PTS");
  const after = await page.evaluate(() =>
    JSON.parse(localStorage.getItem("askChatHistory:v1")).conversations.map(
      (c) => c.turns.length,
    ),
  );
  expect(after).toEqual([2, 2, 2]);
  const second = await page.context().newPage();
  await second.goto("/ask");
  await expect(second.locator("[data-chat-tabs] [role=tab]")).toHaveCount(3);
  expect(
    await second.evaluate(() =>
      JSON.parse(localStorage.getItem("askChatHistory:v1")).conversations.map(
        (c) => c.turns.length,
      ),
    ),
  ).toEqual([2, 2, 2]);
  await second.close();
});

test("drawer axes, point glyphs, opportunities and selection text reject independent mutations", async ({
  page,
}) => {
  await setup(page);
  const outcomes = await page.evaluate(() => {
    const t = claimTest,
      options = { key: "pts", view: "across_games" },
      bad = [];
    const reset = () => paintClaim("drawer", options),
      verify = () =>
        t.a.validateRenderedClaims(t.root, t.payload, t.root.uiClaimSession)
          .status;
    reset();
    t.root.querySelector(".distribution-chart > text").textContent = "999";
    bad.push(verify());
    reset();
    t.root
      .querySelector(".distribution-chart > line:last-of-type")
      .setAttribute("x1", "999");
    bad.push(verify());
    reset();
    t.root
      .querySelector(".distribution-chart > rect")
      .setAttribute("width", "999");
    bad.push(verify());
    reset();
    t.root.querySelector("circle").setAttribute("cx", "999");
    bad.push(verify());
    reset();
    t.root
      .querySelector("circle")
      .setAttribute("aria-label", "2025-01-01: 999 points");
    bad.push(verify());
    reset();
    t.root.querySelector("circle title").textContent = "wrong point";
    bad.push(verify());
    reset();
    t.root.querySelector(".drawer-note").textContent = "0 observations";
    bad.push(verify());
    reset();
    t.root.querySelector("circle").setAttribute("data-game-index", "1");
    bad.push(verify());
    return bad;
  });
  expect(outcomes).toEqual(Array(8).fill("invalid"));
  await page.evaluate(() => {
    paintClaim();
    const t = claimTest;
    t.d.bindMetricDrawer(t.root, t.p.overviewPresentation(t.overview));
  });
  await page.locator('[data-metric-distribution="pts"]').click();
  const d = page.locator("dialog");
  await d.locator('[data-distribution-view="across_games"]').click();
  await d.locator("circle").first().focus();
  expect(await d.evaluate((d) => d.uiClaimReport.errors)).toEqual([]);
  await expect(d.locator("[data-game-point-info]")).toContainText("0.0");
  const invalid = await d.evaluate(async (d) => {
    d.querySelector("[data-game-point-info]").textContent = "999";
    return claimTest.a.validateRenderedClaims(
      d,
      claimTest.overview,
      d.uiClaimSession,
    ).status;
  });
  expect(invalid).toBe("invalid");
});
test("game-variation point positions, labels and peak emphasis are evidence-bound", async ({
  page,
}) => {
  await setup(page);
  const outcomes = await page.evaluate(() => {
    const t = claimTest,
      bad = [];
    for (const attr of ["cx", "cy", "r", "fill"]) {
      paintClaim();
      t.root
        .querySelector("[data-game-variation] circle")
        .setAttribute(attr, attr === "fill" ? "red" : "999");
      bad.push(
        t.a.validateRenderedClaims(t.root, t.payload, t.root.uiClaimSession)
          .status,
      );
    }
    paintClaim();
    t.root.querySelector("[data-game-variation] circle title").textContent =
      "Wrong date and points";
    bad.push(
      t.a.validateRenderedClaims(t.root, t.payload, t.root.uiClaimSession)
        .status,
    );
    return bad;
  });
  expect(outcomes).toEqual(Array(5).fill("invalid"));
});
test("qualification opportunities preserve canonical attempt threshold and zero count", async ({
  page,
}) => {
  await setup(page);
  const result = await page.evaluate(() => {
    const t = claimTest,
      m = t.overview.semantic_evidence.metrics[0];
    m.percentile_label = "Low attempts";
    m.percentile_policy = { attempt_field: "fga", min_attempts: 100 };
    m.components = { fga: 0 };
    m.distribution.among_players.focal_in_cohort = false;
    const report = paintClaim("drawer", { key: "pts", view: "among_players" });
    const text = t.root.querySelector(".drawer-warning").textContent;
    t.root.querySelector(".drawer-warning").textContent = text.replace(
      "0 attempts",
      "100 attempts",
    );
    return {
      report,
      text,
      invalid: t.a.validateRenderedClaims(
        t.root,
        t.payload,
        t.root.uiClaimSession,
      ).status,
    };
  });
  expect(result.report.errors).toEqual([]);
  expect(result.text).toContain("0 attempts; 100 required");
  expect(result.invalid).toBe("invalid");
});
