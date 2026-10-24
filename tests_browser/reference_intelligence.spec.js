import { test, expect } from '@playwright/test';

test('reference app renders governed statistics with every model key absent', async ({ page }) => {
  await page.setExtraHTTPHeaders({'X-Evaluation-Intelligence': 'reference'});
  await page.route('**/api/agent/ask**', async route => {
    await route.continue({url: new URL('/api/agent/ask/stream?season=2024-25', route.request().url()).href});
  });
  await page.goto('/ask');
  await page.locator('[data-agent-question]').fill('Avery Example points per game in the 2024-25 regular season');
  const response = page.waitForResponse(r => r.url().includes('/api/agent/ask/stream'));
  await page.locator('[data-agent-submit]').click();
  expect((await response).status()).toBe(200);
  await expect(page.locator('[data-agent-tables] table')).toHaveCount(1);
  await expect(page.locator('[data-agent-tables]')).toContainText('Avery Example');
  await expect(page.locator('[data-agent-answer]')).not.toContainText('API_KEY');
});
