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

// UI fault injection follows a populated answer from the real offline endpoint.
// It verifies client recovery/replay behavior; server saturation and disconnect
// lifetimes are covered separately by controlled transport/API tests.
for (const fault of ['busy', 'conversation-busy', 'partial-error', 'network-before-events']) {
  test(`populated chat survives ${fault} without replay and allows explicit retry`, async ({page}, testInfo) => {
    const c = await prepare(page, 'baseline_points');
    await submit(page, c.turns[0].question);
    await expect(page.locator('[data-agent-tables]').first()).toContainText('11.7 per game');
    const storedTurns = () => page.evaluate(() => {
      const history = JSON.parse(localStorage.getItem('askChatHistory:v1') || '{}');
      return (history.conversations || []).flatMap(chat => chat.turns || []).length;
    });
    const before = await storedTurns();
    const posts = [];
    page.on('request', request => {
      if (request.method() === 'POST' && /\/api\/agent\/ask(?:\/stream)?(?:\?|$)/.test(request.url())) posts.push(request.url());
    });
    let inject = true;
    await page.route('**/api/agent/ask**', async route => {
      if (!inject) return route.fallback();
      inject = false;
      if (fault === 'network-before-events') return route.abort('connectionfailed');
      if (fault === 'conversation-busy') return route.fulfill({status:409, contentType:'application/json', body:JSON.stringify({detail:'This conversation already has a request in progress. Wait for it to finish.'})});
      if (fault === 'busy') return route.fulfill({status:503, contentType:'application/json', body:JSON.stringify({detail:'Ask NBA Stats is busy. Try again shortly.'})});
      return route.fulfill({status:200, contentType:'text/event-stream', body:
        'event: answer_delta\ndata: {"delta":"Incomplete injected answer"}\n\n' +
        'event: error\ndata: {"detail":"Injected generation failure"}\n\n'});
    });
    await page.locator('[data-followup-question]').fill(c.turns[0].question);
    await page.locator('[data-followup-submit]').click();
    const detail = fault === 'busy' ? 'Ask NBA Stats is busy.' : fault === 'conversation-busy' ? 'This conversation already has a request in progress.' : fault === 'partial-error' ? 'Injected generation failure' : 'The request may still be finishing on the server.';
    await expect(page.locator('[data-agent-answer]')).toContainText(detail);
    await expect(page.locator('[data-followup-submit]')).toBeEnabled();
    await expect(page.locator('[data-agent-submit]')).toBeEnabled();
    await expect(page.locator('[data-agent-tables]').first()).toContainText('11.7 per game');
    expect(await storedTurns()).toBe(before);
    expect(posts).toHaveLength(1);
    expect(posts[0]).toContain('/api/agent/ask/stream');
    await page.screenshot({path:testInfo.outputPath(`${fault}-populated.png`), fullPage:true});
    await page.locator('[data-followup-submit]').click();
    await expect(page.locator('[data-followup-submit]')).toBeEnabled();
    await expect.poll(storedTurns).toBe(before + 1);
    expect(posts).toHaveLength(2);
    expect(posts.every(url => url.includes('/api/agent/ask/stream'))).toBe(true);
    await expect(page.locator('[data-agent-tables]').last()).toContainText('11.7 per game');
  });
}

test('overview shows ten metrics, sample evidence, balanced takeaways and expandable details', async ({page}) => {
  await prepare(page,'baseline_points');
  await submit(page,'How did Avery Example perform in the 2024-25 regular season?');
  const table = page.locator('[data-agent-tables] > .table-scroll table');
  await expect(table.locator('tbody tr')).toHaveCount(10);
  await expect(page.getByRole('heading',{name:'Contributions',exact:true})).toBeVisible();
  await expect(table).toContainText('3PM');
  const samples=page.locator('[data-sample-details]');
  await samples.locator('summary').click();
  await expect(samples).toContainText('attempts');
  await expect(samples).toContainText('valid appearances');
  await samples.locator('summary').click();
  const details=page.locator('[data-additional-metrics]');
  await expect(details).not.toHaveAttribute('open','');
  await details.locator('summary').click();
  await expect(details.locator('table')).toBeVisible();
  await expect(details).toContainText('PTS/36');
  await expect(details.locator('td[aria-label*="per 36 minutes"]').first()).toBeVisible();
  await page.reload();
  await expect(table.locator('tbody tr')).toHaveCount(10);
});

test('focused scoring retains 3PM and seven metrics at a narrow viewport', async ({page}) => {
  await page.setViewportSize({width:390,height:844});
  await prepare(page,'baseline_points');
  await submit(page,'How did Avery Example perform in scoring in the 2024-25 regular season?');
  const table=page.locator('[data-agent-tables] > .table-scroll table');
  await expect(table.locator('tbody tr')).toHaveCount(7);
  await expect(table).toContainText('3PM');
  const samples=page.locator('[data-sample-details]');
  await samples.locator('summary').click();
  await expect(samples).toContainText('no inferred ranking threshold');
  await samples.locator('summary').click();
  expect(await page.evaluate(()=>document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
});


test('compact overview keeps ten rows within a short table and expands contribution context', async ({page}) => {
  await prepare(page,'baseline_points');
  await submit(page,'How did Avery Example perform in the 2024-25 regular season?');
  const table=page.locator('table.compact-metrics').first();
  await expect(table.locator('tbody tr')).toHaveCount(10);
  expect((await table.boundingBox()).height).toBeLessThan(500);
  await expect(table.locator('thead th')).toHaveCount(4);
  const creation=page.locator('.compact-contribution').filter({has:page.getByText('Creation',{exact:true})});
  await expect(creation).not.toHaveAttribute('open','');
  await creation.locator('summary').click();
  await expect(creation.locator('p')).toContainText('turnovers');
  await creation.locator('summary').click();
});


for (const width of [1280,390]) {
  test(`calendar overview stays compact and TS help works at ${width}px`, async ({page}) => {
    await page.setViewportSize({width,height:900});
    await prepare(page,'baseline_points');
    await submit(page,'How did Avery Example perform in April 2025?');
    const table=page.locator('table.compact-metrics').first();
    await expect(table.locator('tbody tr')).toHaveCount(10);
    expect((await table.boundingBox()).height).toBeLessThan(500);
    await expect(table.locator('tbody th abbr').first()).toHaveText('PTS');
    await expect(page.getByRole('heading',{name:'Performance over time',exact:true})).toBeVisible();
    await expect(page.locator('.chart-header')).toContainText('Game-by-game');
    await expect(page.locator('[data-chart-key]')).toHaveCount(5);
    await page.locator('[data-chart-key="reb"]').click();
    await expect(page.locator('.chart-header')).toContainText('REB · Game-by-game');
    const svg=page.locator('.overview-chart svg');
    expect((await svg.boundingBox()).width).toBeLessThanOrEqual((await page.locator('.overview-chart').boundingBox()).width + 1);
    expect(await svg.locator('.agent-date-label').evaluateAll(nodes => nodes.every((node,index) => index === 0 || node.getBoundingClientRect().left >= nodes[index-1].getBoundingClientRect().right + 2))).toBe(true);
    const points=page.locator('[data-agent-charts] .agent-point');
    await expect(points.first()).toHaveAttribute('aria-label',/^2025-04-/);
    const help=table.getByRole('button',{name:'TS%: True shooting percentage calculation'});
    await help.focus();
    const tooltip=table.getByRole('tooltip');
    await expect(tooltip).toBeVisible();
    await expect(tooltip).toContainText('100 × SUM(PTS)');
    await expect(tooltip).toContainText('valid games');
    const rect=await tooltip.boundingBox();
    expect(rect.x).toBeGreaterThanOrEqual(0);expect(rect.x+rect.width).toBeLessThanOrEqual(width);
    expect((await table.boundingBox()).height).toBeLessThan(500);
    await help.press('Escape');
    await expect(tooltip).not.toBeVisible();
    await help.click();await expect(tooltip).toBeVisible();
    await help.click();await expect(tooltip).not.toBeVisible();
    expect(await page.evaluate(()=>document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
    await page.reload();
    await expect(page.locator('.chart-header')).toContainText('Game-by-game');
  });
}

test('dated week remains daily while a season remains monthly',async ({page})=>{
  await prepare(page,'baseline_points');
  await submit(page,'How did Avery Example perform week of 2025-04-14?');
  await expect(page.locator('.chart-header')).toContainText('Game-by-game');
  await expect(page.locator('[data-agent-charts] .agent-point').first()).toHaveAttribute('aria-label',/^2025-04-14/);
  await page.reload();
  await page.getByRole('button',{name:'New chat',exact:true}).click();
  await submit(page,'How did Avery Example perform in the 2024-25 regular season?');
  await expect(page.locator('.chart-header')).toContainText('Monthly average');
});
