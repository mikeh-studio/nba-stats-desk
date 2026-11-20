import {expect, test} from '@playwright/test';

test('reopening a publication answer after a correction preserves its counts and release', async ({page}) => {
  const original = 'pub_0000000000000001';
  const corrected = 'pub_0000000000000002';
  let release = original;
  let requests = 0;
  await page.route('**/api/agent/ask**', async route => {
    requests++;
    expect(new URL(route.request().url()).pathname).toBe('/publication-core/api/agent/ask');
    const first = release === original;
    await route.fulfill({json: {
      publication_id: release,
      scope: {season: '2026-27', phase: 'Regular Season', first: '2026-10-20', last: '2026-10-31'},
      data: {
        metric: {key: 'fg_pct', label: 'FG%', unit: 'ratio', numerator: 'fgm', denominator: 'fga'},
        scope: {aggregation: 'ratio'},
        rows: [{player_id: 101, display_value: first ? 10 : 11.1, numerator: 1, denominator: first ? 10 : 9,
          valid_games: 2, observed_games: 2, missing_component_games: 0}],
      },
    }});
  });
  async function ask() {
    // This browser harness normally uses the archived Ask service.
    await page.locator('[data-agent-form]').evaluate(form => form.setAttribute('data-publication-routing', ''));
    await page.locator('[data-agent-question]').fill('Test Player A ratio FG%');
    await page.locator('[data-agent-submit]').click();
    await expect(page.locator('[data-agent-status]')).toHaveText('Answered');
  }
  const answer = page.locator('.agent-turn-answer').last();
  await page.goto('/ask');
  await ask();
  await expect(answer).toContainText('10%');
  await expect(answer).toContainText('1 made of 10 attempted');
  const originalText = await answer.innerText();
  const chat = await page.evaluate(() => JSON.parse(localStorage.getItem('askChatHistory:v1')).conversations[0]);
  expect(chat.turns[0].payload).toMatchObject({
    publication_id: original,
    counts: [{metric: 'fg_pct', player_id: 101, numerator: 1, denominator: 10,
      valid_games: 2, observed_games: 2, missing_component_games: 0}],
  });

  release = corrected;
  await page.locator('[data-agent-new-chat]').click();
  await page.reload();
  await page.locator('[data-chat-more]').click();
  await page.locator(`[data-history-conversation-id="${chat.id}"]`).click();
  await expect(answer).toHaveText(originalText, {useInnerText: true});
  await expect(answer).toContainText(original);
  expect(requests).toBe(1);

  await page.locator('[data-agent-new-chat]').click();
  await ask();
  await expect(answer).toContainText(corrected);
  await expect(answer).toContainText('11.1%');
  await expect(answer).toContainText('1 made of 9 attempted');
  expect(requests).toBe(2);
});
