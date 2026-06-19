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
    const ratingLabel = document.querySelector('[data-sort-key="score"]')?.textContent?.trim() || "";
    const ratingTooltip = document.querySelector("#performance-rating-tooltip")?.textContent?.trim() || "";
    const modalInitiallyHidden = Boolean(document.querySelector("[data-performance-modal]")?.hidden);
    const oldRailCopy = document.body.textContent?.includes("Percentile ranges load after selecting a player.");
    const missing = requiredKeys.filter((key) => !metricKeys.includes(key));
    return { rows, metricKeys, underOneCount, ratingLabel, ratingTooltip, modalInitiallyHidden, oldRailCopy, missing };
  }, requiredMetricKeys);
  assert(tableState.rows > 0, "Expected at least one playoff player row.");
  assert(tableState.underOneCount === 0, "Found a visible player row below one minute.");
  assert(tableState.missing.length === 0, `Missing table metrics: ${tableState.missing.join(", ")}`);
  assert(tableState.ratingLabel.includes("P-Rating"), "Expected P-Rating sort header.");
  assert(
    tableState.ratingTooltip.includes("converted to z-scores"),
    "Expected P-Rating explanation tooltip."
  );
  assert(tableState.modalInitiallyHidden, "Expected Player View modal to be hidden by default.");
  assert(!tableState.oldRailCopy, "Old right-rail Player View empty-state copy is still visible.");

  const playerButtons = page.locator(".performance-player-button");
  assert(await playerButtons.count() > 0, "Expected at least one player button.");
  const firstPlayerId = await playerButtons.first().getAttribute("data-player-id");
  await playerButtons.first().click();
  await page.waitForSelector("[data-performance-modal]:not([hidden])", { timeout: 20000 });

  const modalState = await page.evaluate(() => {
    const summaryText = document.querySelector("#performance-modal-summary")?.textContent || "";
    const title = document.querySelector("#performance-modal-title")?.textContent?.trim() || "";
    const href = document.querySelector("#performance-modal-detail-link")?.getAttribute("href") || "";
    const status = document.querySelector("#performance-modal-status")?.textContent?.trim() || "";
    return { summaryText, title, href, status };
  });
  assert(modalState.title.length > 0 && modalState.title !== "Player View", "Expected modal player title.");
  assert(modalState.summaryText.includes("P-Rating"), "Expected P-Rating in modal summary.");
  assert(modalState.summaryText.includes("Minutes"), "Expected minutes in modal summary.");
  assert(modalState.status.length > 0, "Expected modal status chip.");
  assert(
    modalState.href === `/players/${firstPlayerId}`,
    `Unexpected View Details href: ${modalState.href}`
  );

  await page.keyboard.press("Escape");
  await page.waitForFunction(() => document.querySelector("[data-performance-modal]")?.hidden === true);

  await page.setViewportSize({ width: 390, height: 900 });
  await playerButtons.first().click();
  await page.waitForSelector("[data-performance-modal]:not([hidden])", { timeout: 20000 });
  const mobilePanelFits = await page.evaluate(() => {
    const panel = document.querySelector(".performance-modal-panel");
    if (!panel) return false;
    const bounds = panel.getBoundingClientRect();
    return bounds.width <= window.innerWidth && bounds.height <= window.innerHeight;
  });
  assert(mobilePanelFits, "Expected Player View modal to fit inside a mobile viewport.");

  await page.screenshot({ path: screenshotPath, fullPage: true });
  console.log(`Capturing screenshot into ${screenshotPath}`);
  console.log(`Performance UI playoff metric check passed for ${baseUrl}`);
} finally {
  await browser.close();
}
NODE
