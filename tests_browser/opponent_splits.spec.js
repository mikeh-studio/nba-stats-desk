import { test, expect } from "@playwright/test";

test("governed opponent splits show an expandable delta table and game evidence", async ({ page }) => {
  await page.setExtraHTTPHeaders({
    "X-Evaluation-Intelligence": "reference",
    "X-Evaluation-Case": "rich_archive_dates",
  });
  await page.route("**/api/agent/ask**", route =>
    route.continue({ url: new URL("/api/agent/ask/stream?season=2024-25", route.request().url()).href }));
  await page.goto("/ask");
  await page.locator("[data-agent-question]").fill("Show Avery Finch's opponent splits in 2024-25");
  await page.locator("[data-agent-submit]").click();

  const tables = page.locator("[data-agent-tables]").first();
  await expect(tables.locator('[data-opponent-split-table="0"] tbody tr').first()).toBeVisible();
  await expect(tables).toContainText("Struggle score");
  await expect(page.locator("[data-agent-answer]").first()).toContainText("toughest opponent");
  await tables.getByText("Change from the player's baseline", { exact: true }).click();
  await expect(tables.locator('[data-opponent-split-table="1"] tbody')).toContainText(" pp");
  await tables.getByText("Games against the toughest opponent", { exact: true }).click();
  await expect(tables.locator('[data-opponent-split-table="2"] tbody tr').first()).toBeVisible();
  expect(await tables.evaluate(node => node.uiClaimReport.status)).toBe("complete");
});
