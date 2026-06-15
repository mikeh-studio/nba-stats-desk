#!/usr/bin/env bash
set -euo pipefail

base_url="${PERFORMANCE_BASE_URL:-http://127.0.0.1:8001}"
out_dir="${PERFORMANCE_UI_OUT_DIR:-/tmp}"
screenshot_path="${out_dir}/nba-performance-playoff-metrics-table.png"

playwright_module="playwright"
if node --input-type=module -e "await import('playwright')" >/dev/null 2>&1; then
  playwright_module="playwright"
else
  cached_pkg="$(find "${HOME}/.npm/_npx" -path '*/node_modules/playwright/index.mjs' -print -quit 2>/dev/null || true)"
  if [[ -z "${cached_pkg}" ]]; then
    echo "Playwright is not installed or cached. Run npx playwright install first." >&2
    exit 1
  fi
  playwright_module="${cached_pkg}"
fi

PLAYWRIGHT_MODULE="${playwright_module}" \
PERFORMANCE_BASE_URL="${base_url}" \
PERFORMANCE_SCREENSHOT_PATH="${screenshot_path}" \
node --input-type=module <<'NODE'
import { pathToFileURL } from "node:url";

const moduleSpec = process.env.PLAYWRIGHT_MODULE || "playwright";
const importSpec = moduleSpec === "playwright" ? moduleSpec : pathToFileURL(moduleSpec).href;
const { chromium } = await import(importSpec);

const baseUrl = process.env.PERFORMANCE_BASE_URL || "http://127.0.0.1:8001";
const screenshotPath = process.env.PERFORMANCE_SCREENSHOT_PATH || "/tmp/nba-performance-playoff-metrics-table.png";
const requiredMetricKeys = ["min", "pts", "reb", "ast", "fg3m", "fg_pct", "ft_pct", "stl", "blk"];

function assert(condition, message) {
  if (!condition) {
    throw new Error(message);
  }
}

const browser = await chromium.launch({ channel: "chrome" });
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1400 } });
  const url = `${baseUrl}/performance`;
  console.log(`Navigating to ${url}`);
  await page.goto(url, { waitUntil: "domcontentloaded" });
  await page.waitForSelector('.performance-table [data-metric-key="fg_pct"]', { timeout: 20000 });

  const tableState = await page.evaluate((requiredKeys) => {
    const rows = document.querySelectorAll(".performance-table tbody tr").length;
    const metricKeys = Array.from(
      document.querySelectorAll(".performance-table tbody tr:first-child [data-metric-key]")
    ).map((node) => node.getAttribute("data-metric-key"));
    const underOneCount = Array.from(
      document.querySelectorAll(".performance-player-button .performance-player-meta")
    ).filter((node) => /- 0\.[0-9]+ MIN/.test(node.textContent || "")).length;
    const statusText = document.querySelector("[data-health-status]")?.textContent?.trim() || "";
    const missing = requiredKeys.filter((key) => !metricKeys.includes(key));
    return { rows, metricKeys, underOneCount, statusText, missing };
  }, requiredMetricKeys);
  assert(tableState.rows > 0, "Expected at least one playoff player row.");
  assert(tableState.underOneCount === 0, "Found a visible player row below one minute.");
  assert(tableState.missing.length === 0, `Missing table metrics: ${tableState.missing.join(", ")}`);

  await page.waitForFunction(() => {
    const text = document.querySelector("[data-health-status]")?.textContent?.trim() || "";
    return text !== "Last refresh loading" && text.includes("2025-26");
  }, null, { timeout: 20000 });

  const playerButtons = page.locator(".performance-player-button");
  assert(await playerButtons.count() > 0, "Expected at least one player button.");
  await playerButtons.first().click();
  await page.waitForSelector('[data-detail-metric-key="fg_pct"]', { timeout: 20000 });

  const detailState = await page.evaluate((requiredKeys) => {
    const detailKeys = Array.from(
      document.querySelectorAll("[data-detail-metric-key]")
    ).map((node) => node.getAttribute("data-detail-metric-key"));
    const chartPointCount = Number(
      document.querySelector("#performance-trend-chart")?.getAttribute("data-point-count") || "0"
    );
    const statusText = document.querySelector("[data-health-status]")?.textContent?.trim() || "";
    const missing = requiredKeys.filter((key) => !detailKeys.includes(key));
    return { chartPointCount, detailKeys, statusText, missing };
  }, requiredMetricKeys);
  assert(detailState.missing.length === 0, `Missing detail metrics: ${detailState.missing.join(", ")}`);
  assert(detailState.chartPointCount > 0, "Expected a non-empty player trend chart.");
  assert(
    detailState.statusText.includes("2025-26") && detailState.statusText.includes("season"),
    `Unexpected health status text: ${detailState.statusText}`
  );

  await page.screenshot({ path: screenshotPath, fullPage: true });
  console.log(`Capturing screenshot into ${screenshotPath}`);
  console.log(`Performance UI playoff metric check passed for ${baseUrl}`);
} finally {
  await browser.close();
}
NODE
