import { test, expect } from "@playwright/test";
// Explicit local-only frozen-source fixture; no paid provider or warehouse reads.
const url = process.env.ASK_RANK_STORAGE_URL;
test("reachable100-row ranking survives real browser history and reports actual quota eviction", async ({
  page,
}, testInfo) => {
  test.skip(
    !url,
    "Requires explicit ASK_RANK_STORAGE_URL pointing to the documented100-player frozen fixture.",
  );
  const question =
    "Rank the top 100 players by points per game in the 2024-25 regular season.";
  const headers = {
    "X-Evaluation-Case": "rank_storage_100",
    "X-Evaluation-Turn": "0",
  };
  await page.route("**/api/agent/ask**", async (route) => {
    const req = route.request();
    await route.continue({
      url: new URL(
        "/api/agent/" +
          (req.url().includes("/stream") ? "ask/stream" : "ask") +
          "?season=2024-25",
        url,
      ).href,
      headers: { ...req.headers(), ...headers },
    });
  });
  await page.goto(new URL("/ask", url).href);
  const response = await page.request.post(
    new URL("/api/agent/ask?season=2024-25", url).href,
    { headers, data: { question, conversation_id: "rank-storage-json-probe" } },
  );
  expect(response.status()).toBe(200);
  const payload = await response.json();
  expect(payload.status).toBe("ok");
  expect(payload.tables).toHaveLength(1);
  expect(payload.tables[0].rows).toHaveLength(100);
  expect(payload.semantic_evidence.rows[0].player_id).toBe(100);
  expect(payload.semantic_evidence.rows[0].value).toBe(100);
  expect(payload.semantic_evidence.rows[99].player_id).toBe(1);
  expect(payload.claim_validation.errors).toEqual([]);
  const submit = async (follow) => {
    const input = page.locator(
      follow ? "[data-followup-question]" : "[data-agent-question]",
    );
    await input.fill(question);
    const pending = page.waitForResponse((r) =>
      r.url().includes("/api/agent/ask/stream"),
    );
    await input.press("Enter");
    expect((await pending).status()).toBe(200);
    await expect(page.locator("[data-agent-submit]")).toBeEnabled();
    await expect(
      page.locator("[data-agent-tables]").last().locator("tbody tr"),
    ).toHaveCount(100);
  };
  for (let chat = 0; chat < 3; chat++) {
    if (chat)
      await page.getByRole("button", { name: "New chat", exact: true }).click();
    await submit(false);
    await submit(true);
  }
  const snapshot = () =>
    page.evaluate(() => {
      const raw = localStorage.getItem("askChatHistory:v1");
      const h = JSON.parse(raw);
      return {
        bytes: new TextEncoder().encode(raw).length,
        counts: h.conversations.map((c) => c.turns.length),
        rows: h.conversations.flatMap((c) =>
          c.turns.map((t) => t.payload.tables[0].rows.length),
        ),
      };
    });
  const before = await snapshot();
  expect(before.counts).toEqual([2, 2, 2]);
  expect(before.rows).toEqual(Array(6).fill(100));
  expect(before.bytes).toBeLessThan(2 * 1024 * 1024);
  await page.reload();
  await expect(page.locator("[data-chat-tabs] [role=tab]")).toHaveCount(3);
  expect((await snapshot()).counts).toEqual([2, 2, 2]);
  const second = await page.context().newPage();
  await second.goto(new URL("/ask", url).href);
  await expect(second.locator("[data-chat-tabs] [role=tab]")).toHaveCount(3);
  expect(
    await second.evaluate(() =>
      JSON.parse(localStorage.getItem("askChatHistory:v1")).conversations.map(
        (c) => c.turns.length,
      ),
    ),
  ).toEqual([2, 2, 2]);
  await second.close();
  const quota = await page.evaluate(() => {
    let blocks = 0,
      error = null;
    const block = "x".repeat(32 * 1024);
    try {
      for (; blocks < 512; blocks++)
        localStorage.setItem(`test-occupied-${blocks}`, block);
    } catch (e) {
      error = e.name;
    }
    return { blocks, error, paddingBytes: blocks * block.length };
  });
  expect(quota.error).toBe("QuotaExceededError");
  await submit(true);
  const after = await snapshot();
  expect(after.counts.length).toBeLessThan(3);
  expect(after.counts[0]).toBe(3);
  await expect(page.locator("[data-storage-notice]")).toContainText(
    "nearly full",
  );
  await page.evaluate(() => {
    for (const k of Object.keys(localStorage))
      if (k.startsWith("test-occupied-")) localStorage.removeItem(k);
  });
  await page.reload();
  expect((await snapshot()).counts).toEqual(after.counts);
  await expect(
    page.locator("[data-agent-tables]").last().locator("tbody tr"),
  ).toHaveCount(100);
  await testInfo.attach("rank-storage-measurements", {
    body: JSON.stringify({
      scenario:
        "actual JSON and SSE single100-row rank; six answers in three app chats, second real browser page; artificial occupancy triggers native quota error",
      endpointBytes: Buffer.byteLength(JSON.stringify(payload)),
      proofBytes: Buffer.byteLength(JSON.stringify(payload.claim_bindings)),
      before,
      quota,
      after,
    }),
    contentType: "application/json",
  });
});
