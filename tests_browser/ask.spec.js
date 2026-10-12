import { test, expect } from '@playwright/test';
import fs from 'node:fs';

const cases = JSON.parse(fs.readFileSync('tests/fixtures/ask/cases.json','utf8')).cases;
async function prepare(page, id) {
  const scenario = cases.find(c => c.id === id);
  // Tag fixture inputs and select the archive. All responses come from the real app.
  await page.route('**/api/agent/ask**', async route => {
    const request = route.request();
    const body = request.postDataJSON();
    const index = scenario.turns.findIndex(t => t.question === body.question);
    await route.continue({url: new URL('/api/agent/' + (request.url().includes('/stream')?'ask/stream':'ask') + '?season=2024-25',request.url()).href,
      postData: JSON.stringify(body),
      headers:{...request.headers(),'X-Evaluation-Case':id,'X-Evaluation-Turn':String(Math.max(0,index))}});
  });
  await page.goto('/ask');
  return scenario;
}
async function submit(page, question) {
  await page.locator('[data-agent-question]').fill(question);
  const response = page.waitForResponse(r => r.url().includes('/api/agent/ask/stream'));
  await page.locator('[data-agent-submit]').click();
  expect((await response).status()).toBe(200);
  await expect(page.locator('[data-agent-submit]')).toBeEnabled();
}

test('streamed baseline displays verified values, chart and evidence', async ({page}) => {
  const c = await prepare(page,'baseline_points');
  await submit(page,c.turns[0].question);
  await expect(page.locator('[data-agent-answer]')).toContainText('Player minus league: -6.7');
  await expect(page.locator('[data-agent-tables]')).toContainText('11.7 per game');
  await expect(page.locator('[data-agent-tables]')).toContainText('18');
  await expect(page.locator('[data-agent-charts] svg').first()).toBeVisible();
});

test('follow-up changes metric while preserving league comparison and saved context', async ({page}) => {
  const c = await prepare(page,'follow_baseline');
  await submit(page,c.turns[0].question);
  expect((await page.request.post("/__eval/reset-conversations")).ok()).toBe(true);
  await page.reload();
  await expect(page.locator('[data-agent-answer]')).toContainText('Player minus league: -6.7');
  await page.locator('[data-followup-question]').fill(c.turns[1].question);
  await page.locator('[data-followup-submit]').click();
  await expect(page.locator('[data-agent-tables]').last()).toContainText('3.8 per game');
  await expect(page.locator('[data-agent-answer]')).toContainText('2.5 per game');
});

test('clarification and unsupported requests do not display substitute numbers', async ({page}) => {
  const c=await prepare(page,'missing_threshold');
  await submit(page,c.turns[0].question);
  await expect(page.locator('[data-agent-answer]')).toContainText('attempt threshold');
  await expect(page.locator('[data-agent-tables] table')).toHaveCount(0);
});

test('similarity renders publication values at a narrow viewport', async ({page}) => {
  await page.setViewportSize({width:390,height:844});
  const c=await prepare(page,'similarity');
  await submit(page,c.turns[0].question);
  await expect(page.locator('[data-agent-tables]')).toContainText('Blair Sample');
  await expect(page.locator('[data-agent-tables]')).toContainText('0.8000');
  expect(await page.evaluate(()=>document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
});

test('recent statistical comparison shows assumptions and actual last-five values', async ({page}) => {
  const c = await prepare(page,'recent_similarity_default');
  await submit(page,c.turns[0].question);
  await expect(page.locator('[data-agent-answer]')).toContainText('Since you did not specify metrics');
  await expect(page.locator('[data-agent-answer]')).toContainText('last 5 recorded');
  await expect(page.locator('[data-agent-tables]')).toContainText('Statistical Profile Similarity');
  await expect(page.locator('[data-agent-tables]')).toContainText('Blair Sample');
  await expect(page.locator('[data-agent-tables]')).toContainText('6.0');
});

test('methodology page explains the feature set, scoring and model selection', async ({page}) => {
  await page.goto('/similarity-methodology');
  await expect(page.getByRole('heading',{name:'Statistical Profile Similarity',exact:true})).toBeVisible();
  await expect(page.getByText('Inspect all 48 configured input fields')).toBeVisible();
  await expect(page.getByRole('heading',{name:'Why these grouping models?'})).toBeVisible();
  await expect(page.getByText('55% normalized silhouette score, 25% group balance and 20% assigned-player coverage')).toBeVisible();
  await page.getByRole('link',{name:'Explore statistical profiles'}).click();
  await expect(page.getByRole('heading',{name:'Statistical Profile Similarity',exact:true})).toBeVisible();
  await expect(page.getByRole('link',{name:'Features, scoring and model selection explained'})).toBeVisible();
});

test('known nonparticipation is explained without fabricated Finals statistics', async ({page}) => {
  const c = await prepare(page,'finals_known_nonparticipant');
  await submit(page,c.turns[0].question);
  await expect(page.locator('[data-agent-answer]')).toContainText('did not participate');
  await expect(page.locator('[data-agent-answer] a')).toHaveAttribute('href','https://www.nba.com/game/gsw-vs-phx-0052500211');
  await expect(page.locator('[data-agent-tables] table')).toHaveCount(0);
});

test('availability shows verified groups, source exclusions and metric follow-up', async ({page}) => {
  const c = await prepare(page, 'availability_followup');
  await submit(page, c.turns[0].question);
  await expect(page.locator('[data-agent-answer]')).toContainText('3 appearances');
  await expect(page.locator('[data-agent-answer]')).toContainText('1 other focal appearances were excluded');
  await expect(page.locator('[data-agent-tables]')).toContainText('Blair Sample did not play');
  await expect(page.locator('[data-agent-tables]')).toContainText('6.0');
  await page.locator('[data-followup-question]').fill(c.turns[1].question);
  await page.locator('[data-followup-submit]').click();
  await expect(page.locator('[data-agent-answer]')).toContainText('3.0 AST');
  await expect(page.locator('[data-agent-charts] svg').first()).toBeVisible();
});

test('research displays populated filtered values and explicit player override', async ({page}) => {
  const c = await prepare(page, 'context_research_player_override');
  await submit(page, c.turns[0].question);
  await expect(page.locator('[data-agent-tables]')).toContainText('Research breakdown');
  await expect(page.locator('[data-agent-tables]')).toContainText('Avery Example');
  await page.locator('[data-followup-question]').fill(c.turns[1].question);
  await page.locator('[data-followup-submit]').click();
  await expect(page.locator('[data-agent-answer]')).toContainText('Blair Sample');
  await expect(page.locator('[data-agent-tables]').last()).toContainText('13');
});

test('published study shows estimates and uncertainty without claiming significance', async ({page}) => {
  const c = await prepare(page, 'research_published_study');
  await submit(page, c.turns[0].question);
  await expect(page.locator('[data-agent-tables]')).toContainText('+1.50');
  await expect(page.locator('[data-agent-tables]')).toContainText('-2.00 to +5.00');
  await expect(page.locator('[data-agent-answer]')).toContainText('no validated statistical-significance or causal claim');
});
