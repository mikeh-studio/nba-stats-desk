import { test, expect } from '@playwright/test';

// The real loopback fixture keeps two core games eligible while its optional
// shot product contains one complete zero-attempt game and one unknown game.
test('covered zero attempts and unknown observations stay distinct in the UI', async ({ page }) => {
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('/ask');
  await page.locator('#metric-product').selectOption('shot_events');
  const run = async (day) => {
    await page.locator('#question').fill(`Test Player B ratio contested_attempt_share from ${day} to ${day}`);
    const request = page.waitForResponse(response => response.url().endsWith('/api/agent/ask'));
    await page.locator('#ask button').click();
    const response = await request;
    expect(response.ok()).toBe(true);
    const payload = await response.json();
    await expect(page.locator('#metric-summary')).toBeVisible();
    await expect(page.locator('#metric-summary')).toContainText('unavailable');
    await page.locator('#metric-evidence').evaluate(element => { element.open = true; });
    await expect(page.locator('#metric-evidence-data')).toBeVisible();
    expect(JSON.parse(await page.locator('#metric-evidence-data').textContent())).toEqual(payload);
    return payload;
  };
  const zero = await run('2026-10-20');
  await expect(page.locator('#metric-summary')).toContainText('1 of 1 eligible player-games covered (0 unknown)');
  expect(zero.data.rows[0]).toMatchObject({ value: null, numerator: 0, denominator: 0 });
  const unknown = await run('2026-10-21');
  await expect(page.locator('#metric-summary')).toContainText('0 of 1 eligible player-games covered (1 unknown)');
  expect(unknown.data.rows[0]).toMatchObject({ value: null, numerator: null, denominator: null });
  expect(unknown.data.coverage.unknown_game_ids).toHaveLength(1);
  expect(errors).toEqual([]);
});
