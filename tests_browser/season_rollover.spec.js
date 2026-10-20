import fs from 'node:fs';
import {test, expect} from '@playwright/test';

test('research binds the disclosed available season to the populated breakdown', async ({page}) => {
  await page.route('**/api/research/players*', async route => {
    expect(new URL(route.request().url()).searchParams.has('season')).toBe(false);
    await route.fulfill({json: {season: '2025-26', players: [{player_id: 1, player_name: 'Fixture Player'}], notice: 'No matching data is available for 2026-27. Using 2025-26, the most recent available season for this request.'}});
  });
  await page.route('**/api/research/studies', route => route.fulfill({json: {studies: []}}));
  await page.route('**/api/research/breakdown*', async route => {
    expect(new URL(route.request().url()).searchParams.get('season')).toBe('2025-26');
    expect(route.request().postDataJSON().season).toBe('2025-26');
    await route.fulfill({json: {players: [{player_name: 'Fixture Player', metrics: [], games: []}], scope: {season: '2025-26'}, limitations: [], source: 'synthetic browser fixture', snapshot_id: 'fixture'}});
  });
  const panel = fs.readFileSync('app/templates/research_panel.html', 'utf8')
    .replace(/{{\s*season\s*}}/g, '2026-27').replace(/{{.*?}}/g, '');
  await page.route('**/__rollover-panel', route => route.fulfill({contentType: 'text/html', body: `<html><body>${panel}</body></html>`}));
  await page.goto('/__rollover-panel');
  const root = page.locator('[data-research-root]');
  await expect(root.locator('[data-research-status]')).toContainText('Using 2025-26');
  await root.locator('select[name=player]').selectOption('1');
  await root.locator('[type=submit]').click();
  await expect(root.locator('[data-research-results]')).toContainText('Fixture Player');
  await expect(root.locator('[data-research-status]')).toContainText('Using 2025-26');
  await expect(root.locator('[data-research-status]')).toContainText('Breakdown ready.');
});
