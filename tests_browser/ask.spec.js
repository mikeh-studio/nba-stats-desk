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

for (const width of [1280, 390]) {
  test(`insight overview connects peer context and non-scoring production at ${width}px`, async ({page}, testInfo) => {
    await page.setViewportSize({width, height:900});
    const scenario = await prepare(page, 'balanced_overview');
    await submit(page, scenario.turns[0].question);
    const prose = page.locator('.overall-copy');
    await expect(prose).toContainText('2.5 assists per game');
    await expect(prose).toContainText('2.0 turnovers per game');
    await expect(page.locator('.overview-scope')).toContainText('6 regular-season appearances');
    await expect(prose.locator('p').first()).not.toContainText('This assessment covers');
    await expect(prose).not.toContainText('exceeded');
    await expect(prose).not.toContainText('positioning and assignments');
    await expect(prose).not.toContainText('highest-scoring game');
    await expect(page.locator('.takeaway')).toHaveCount(2);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({path:testInfo.outputPath(`balanced-overview-${width}.png`), fullPage:true});
    // Frozen saved prose stays intact; an explicit new question gets new prose.
    await page.evaluate(() => {
      const key = 'askChatHistory:v1';
      const history = JSON.parse(localStorage.getItem(key));
      const payload = history.conversations[0].turns[0].payload;
      payload.semantic_evidence.summary_paragraphs = ['Saved scoring-only assessment.'];
      payload.answer = 'Saved scoring-only assessment.';
      localStorage.setItem(key, JSON.stringify(history));
    });
    await page.reload();
    await expect(prose).toContainText('Saved scoring-only assessment.');
    await expect(prose).not.toContainText('2.5 assists');
    await submit(page, scenario.turns[0].question);
    await expect(prose).toContainText('2.5 assists per game');
    await expect(prose).toContainText('2.0 turnovers per game');
    await expect(page.locator('.overview-scope')).toContainText('6 regular-season appearances');
    await expect(prose.locator('p').first()).not.toContainText('This assessment covers');
  });
}

test('non-scoring wording selects creation chart and omits generic limitations', async ({page}) => {
  const scenario = await prepare(page, 'balanced_non_scoring');
  await submit(page, scenario.turns[0].question);
  await expect(page.locator('.overall-copy')).toContainText('2.5 assists per game');
  await expect(page.locator('.overall-copy')).toContainText('2.0 turnovers per game');
  await expect(page.locator('.overall-copy')).not.toContainText('exceeded');
  await expect(page.locator('.overall-copy')).not.toContainText(/Screens|secondary assists|overall defensive impact/);
  await expect(page.locator('.overall-copy')).not.toContainText('True shooting');
  await expect(page.locator('[data-chart-key="ast"]')).toHaveAttribute('aria-pressed', 'true');
});

test('comparison hides unavailable impact in fresh and legacy saved analyses', async ({page}, testInfo) => {
  const c = await prepare(page, 'two_player');
  await submit(page, c.turns[0].question);
  await expect(page.locator('[data-agent-answer]')).toContainText('Playing time and workload');
  await expect(page.locator('[data-agent-tables]')).toContainText('Blair Sample by 7.5');
  await expect(page.locator('[data-agent-answer]')).not.toContainText(/Win Shares|possession|winning-impact metrics/i);
  await expect(page.locator('.comparison-impact')).toHaveCount(0);
  await expect(page.locator('[data-comparison-metric="impact"]')).toHaveCount(0);
  await page.evaluate(() => {
    const key = 'askChatHistory:v1';
    const history = JSON.parse(localStorage.getItem(key));
    const payload = history.conversations[0].turns[0].payload;
    const old = 'These category differences do not establish an overall winner. Win Shares and possession-based on/off ratings are unavailable in the governed source, so total versus per-minute winning contribution cannot yet be assessed.';
    payload.semantic_evidence.paragraphs.push(old);
    payload.semantic_evidence.impact = [{key:'ws', label:'Win Shares', left:null, right:null, reason:'Win Shares source not connected'}];
    payload.assumptions.push('Win Shares and on/off possession data are not connected. Raw plus/minus is not substituted for net rating. No illustrative values are used.');
    localStorage.setItem(key, JSON.stringify(history));
  });
  await page.reload();
  await expect(page.locator('[data-agent-tables]')).toContainText('Blair Sample by 7.5');
  await expect(page.locator('[data-agent-answer]')).not.toContainText(/Win Shares|possession|winning-impact metrics/i);
  await expect(page.locator('.comparison-impact')).toHaveCount(0);
  await expect(page.locator('[data-answer-context]')).not.toContainText(/Win Shares|possession/i);
  await page.locator('[data-comparison-metric="ast"]').click();
  await expect(page.locator('[data-agent-charts]')).toContainText('Assists per game comparison');
  await page.screenshot({path:testInfo.outputPath('comparison-available-metrics.png'),fullPage:true});
});

for (const width of [1280,390]) {
  test(`additional comparison sections show actual endpoint values at ${width}px`, async ({page},testInfo) => {
    await page.setViewportSize({width,height:900});
    const c=await prepare(page,'two_player');
    await submit(page,c.turns[0].question);
    const shooting=page.locator('[data-comparison-section="shooting_efficiency"]');
    await expect(shooting).toContainText('TS%');
    await expect(shooting).toContainText('percentage points');
    await expect(shooting).toContainText('6 of 6 games');
    const security=page.locator('[data-comparison-section="ball_security"]');
    await expect(security).toContainText('Turnovers');
    const rates=page.locator('[data-comparison-section="per36_production"]');
    await expect(rates).toContainText('Points per 36 minutes');
    await expect(rates).toContainText('15.0');
    await expect(rates).toContainText('Blair Sample is higher by 9.00 per 36 minutes');
    expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1)).toBe(true);
    await page.reload();
    await expect(rates).toContainText('15.0');
    await page.screenshot({path:testInfo.outputPath(`comparison-expanded-${width}.png`),fullPage:true});
  });
}

test('top questions create separate analyses and follow-ups belong to the selected tab', async ({page}, testInfo) => {
  const c = await prepare(page, 'follow_baseline');
  const posts = [];
  page.on('request', request => {
    if (request.method() === 'POST' && request.url().includes('/api/agent/ask/stream'))
      posts.push(request.postDataJSON());
  });
  await submit(page, c.turns[0].question);
  const firstId = posts[0].conversation_id;
  await expect(page.locator('[data-agent-tables]')).toContainText('11.7 per game');
  await expect(page.locator('[data-agent-question]')).toHaveValue('');
  await submit(page, c.turns[0].question);
  const secondId = posts[1].conversation_id;
  expect(secondId).not.toBe(firstId);
  expect(posts[1].previous_context).toBeUndefined();
  await expect(page.locator('[data-chat-tabs] [role=tab]')).toHaveCount(2);
  await expect(page.locator('[data-agent-answer] .agent-turn')).toHaveCount(1);
  await page.locator(`[data-chat-id="${firstId}"]`).click();
  await page.locator('[data-followup-question]').fill(c.turns[1].question);
  await page.locator('[data-followup-submit]').click();
  await expect(page.locator('[data-followup-submit]')).toBeEnabled();
  expect(posts[2].conversation_id).toBe(firstId);
  expect(posts[2].previous_context).toBeDefined();
  await expect(page.locator('[data-agent-tables]').last()).toContainText('3.8 per game');
  await expect(page.locator('[data-agent-answer] .agent-turn')).toHaveCount(2);
  await expect(page.locator('[data-chat-tabs] [role=tab]')).toHaveCount(2);
  await page.locator(`[data-chat-id="${secondId}"]`).click();
  await expect(page.locator('[data-agent-answer] .agent-turn')).toHaveCount(1);
  await expect(page.locator('[data-agent-tables]')).toContainText('11.7 per game');
  await page.reload();
  await expect(page.locator(`[data-chat-id="${secondId}"]`)).toHaveAttribute('aria-selected', 'true');
  await page.locator(`[data-chat-id="${firstId}"]`).click();
  await expect(page.locator('[data-agent-tables]').last()).toContainText('3.8 per game');
  await expect(page.locator('[data-agent-answer] .agent-turn')).toHaveCount(2);
  await page.screenshot({path:testInfo.outputPath('separate-analyses.png'), fullPage:true});
});

for (const width of [1280, 390, 320]) {
  test(`revised header and composer layout works at ${width}px`, async ({page}, testInfo) => {
    await page.setViewportSize({width, height:900});
    const c = await prepare(page, 'baseline_points');
    await submit(page, c.turns[0].question);
    await expect(page.locator('.nav-tabs a')).toHaveCount(4);
    await expect(page.locator('.nav-tabs a')).toHaveText(['ASK', 'PERFORMANCE', 'TRENDING', 'PROFILES']);
    const brand = await page.locator('.brand-lockup').boundingBox();
    const search = await page.locator('.nav-player-search').boundingBox();
    const query = await page.locator('.ask-query').boundingBox();
    const tabs = await page.locator('.chat-tab-strip').boundingBox();
    expect(query.y + query.height).toBeLessThanOrEqual(tabs.y);
    if (width > 720) { expect(search.x).toBeGreaterThan(brand.x + brand.width); expect(Math.abs(search.y - brand.y)).toBeLessThan(20); }
    else expect(search.y).toBeGreaterThanOrEqual(brand.y + brand.height);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    await page.locator('[data-chat-more]').click();
    await expect(page.locator('[data-agent-form]')).toBeVisible();
    await expect(page.locator('[data-chat-history]')).toBeVisible();
    await page.locator('[data-history-back]').click();
    await page.screenshot({path:testInfo.outputPath(`ask-layout-${width}.png`), fullPage:true});
    // Search uses the existing real fixture endpoint and profile navigation.
    const response = page.waitForResponse(r => r.url().includes('/api/players/search'));
    await page.locator('[data-player-search-input]').fill('Avery');
    expect((await response).status()).toBe(200);
    const result = page.locator('[data-player-search-results] [data-player-id]').first();
    await expect(result).toBeVisible();
    const playerId = await result.getAttribute('data-player-id');
    await result.click();
    await expect(page).toHaveURL(new RegExp(`/players/${playerId}`));
    await expect(page.locator('.nav-player-search')).toBeVisible();
  });
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

test('overview shows eleven metrics, sample evidence, balanced takeaways and expandable details', async ({page}) => {
  await prepare(page,'baseline_points');
  await submit(page,'How did Avery Example perform in the 2024-25 regular season?');
  const table = page.locator('[data-agent-tables] > .table-scroll table');
  await expect(table.locator('tbody tr')).toHaveCount(11);
    expect(await table.locator('tbody tr').evaluateAll(rows=>rows.slice(-3).map(r=>r.dataset.metricKey))).toEqual(['fg_pct','ft_pct','ts_pct']);
  await expect(page.getByRole('heading',{name:'Key Takeaways',exact:true})).toBeVisible();
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
  await expect(table.locator('tbody tr')).toHaveCount(11);
    expect(await table.locator('tbody tr').evaluateAll(rows=>rows.slice(-3).map(r=>r.dataset.metricKey))).toEqual(['fg_pct','ft_pct','ts_pct']);
});

test('focused scoring retains 3PM and nine metrics at a narrow viewport', async ({page}) => {
  await page.setViewportSize({width:390,height:844});
  await prepare(page,'baseline_points');
  await submit(page,'How did Avery Example perform in scoring in the 2024-25 regular season?');
  const table=page.locator('[data-agent-tables] > .table-scroll table');
  await expect(table.locator('tbody tr')).toHaveCount(9);
  await expect(table).toContainText('3PM');
  const samples=page.locator('[data-sample-details]');
  await samples.locator('summary').click();
  await expect(samples).toContainText('Shooting ranks use provisional minimums');
  await samples.locator('summary').click();
  expect(await page.evaluate(()=>document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
});


test('compact overview has visible Key Takeaways and a short eleven-row table', async ({page}) => {
  await prepare(page,'baseline_points');
  await submit(page,'How did Avery Example perform in the 2024-25 regular season?');
  const table=page.locator('table.compact-metrics').first();
  await expect(table.locator('tbody tr')).toHaveCount(11);
    expect(await table.locator('tbody tr').evaluateAll(rows=>rows.slice(-3).map(r=>r.dataset.metricKey))).toEqual(['fg_pct','ft_pct','ts_pct']);
  expect((await table.boundingBox()).height).toBeLessThan(500);
  await expect(table.locator('thead th')).toHaveCount(4);
  await expect(page.getByRole('heading',{name:'Key Takeaways',exact:true})).toBeVisible();
  const items=page.locator('.compact-takeaways .takeaway');
  expect(await items.count()).toBeGreaterThan(0);expect(await items.count()).toBeLessThanOrEqual(3);
  await expect(items.first().locator('p')).toBeVisible();
  await expect(page.locator('.compact-takeaways details')).toHaveCount(0);
});


for (const width of [1280,390]) {
  test(`calendar overview stays compact and TS help works at ${width}px`, async ({page}) => {
    await page.setViewportSize({width,height:900});
    await prepare(page,'baseline_points');
    await submit(page,'How did Avery Example perform in April 2025?');
    const table=page.locator('table.compact-metrics').first();
    await expect(table.locator('tbody tr')).toHaveCount(11);
    expect(await table.locator('tbody tr').evaluateAll(rows=>rows.slice(-3).map(r=>r.dataset.metricKey))).toEqual(['fg_pct','ft_pct','ts_pct']);
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


for (const width of [1280,390]) {
  test(`conversation panel matches selected design and preserves scope at ${width}px`, async ({page}) => {
    await page.setViewportSize({width,height:900});
    await prepare(page,'baseline_points');
    await submit(page,'How did Avery Example perform in April 2025?');
    const panel=page.locator('[data-followup-section]');
    await expect(panel.getByRole('heading',{name:'Ask your next question',exact:true})).toBeVisible();
    await expect(panel.locator('.followup-eyebrow')).toHaveText('Continue the conversation');
    await expect(panel.locator('[data-followup-context]')).toContainText('Avery Example');
    await expect(panel.locator('[data-followup-context]')).toContainText('Apr 1, 2025');
    const styles=await panel.locator('h2').evaluate(el=>({font:getComputedStyle(el).fontFamily,color:getComputedStyle(el.parentElement).backgroundColor}));
    expect(styles.font).toMatch(/^Barlow,/);expect(styles.font).not.toContain('Condensed');
    expect(styles.color).toBe('rgb(26, 29, 34)');
    expect((await panel.boundingBox()).height).toBeLessThan(width===390 ? 400 : 310);
    expect((await panel.locator('textarea').boundingBox()).height).toBeLessThanOrEqual(61);
    await panel.getByRole('button',{name:'Review scoring in these dates',exact:true}).click();
    await expect(panel.locator('textarea')).toHaveValue(/2025-04-01 through 2025-04-30/);
    await expect(panel.locator('textarea')).toBeFocused();
    await panel.locator('textarea').press('Shift+Enter');
    expect(await panel.locator('textarea').inputValue()).toContain('\n');
    await panel.locator('textarea').fill('How did Avery Example perform week of 2025-04-14?');
    await panel.locator('textarea').press('Enter');
    await expect(panel.locator('[data-followup-submit]')).toBeEnabled();
    await expect(page.locator('[data-agent-answer]').last()).toContainText('Avery Example');
    await expect(panel.locator('[data-followup-context]')).toContainText('Apr 14, 2025');
    await expect(panel.locator('[data-followup-context]')).toContainText('Apr 20, 2025');
    await expect(page.locator('.chart-header').last()).toContainText('Game-by-game');
    expect(await page.evaluate(()=>document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
    await page.reload();
    await expect(panel.locator('h2')).toHaveText('Ask your next question');
    await expect(panel.locator('[data-followup-context]')).toContainText('Apr 14, 2025');
  });
}


for (const width of [1280,390]) {
  test(`natural time defaults answer and period correction works at ${width}px`, async ({page}) => {
    await page.setViewportSize({width,height:900});
    await prepare(page,'baseline_points');
    await submit(page,'How did Avery Example played last year as of 2025-10-12?');
    await expect(page.locator('[data-agent-answer]')).toContainText('last completed NBA season (2024-25)');
    await expect(page.getByRole('heading',{name:'Key Takeaways',exact:true}).first()).toBeVisible();
    await expect(page.locator('table.compact-metrics').first().locator('tbody tr')).toHaveCount(11);
    await page.locator('.scope-assumption summary').click();
    await page.getByRole('button',{name:'Past 12 months',exact:true}).click();
    await expect(page.locator('[data-agent-submit]')).toBeEnabled();
    await expect(page.locator('[data-agent-answer]')).not.toContainText('Please specify a date range');
    await expect(page.locator('.agent-turn')).toHaveCount(2);
    expect(await page.evaluate(()=>document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    await page.reload();
    await expect(page.locator('.scope-assumption').first()).toContainText('2024-25');
  });
}


for (const width of [1280,390]) {
  test(`metric drawer has peer/game views, keyboard dismissal and compact layout at ${width}px`, async ({page})=>{
    await page.setViewportSize({width,height:900});
    await prepare(page,'baseline_points');
    await submit(page,'How did Avery Example perform in April 2025?');
    let requests=0; page.on('request',r=>{if(r.method()==='POST' && r.url().includes('/api/agent/ask'))requests++;});
    const table=page.locator('table.compact-metrics').first(), height=(await table.boundingBox()).height;
    const trigger=page.getByRole('button',{name:/^PTS distribution:/}).first();
    await expect(trigger).toHaveAccessibleName(/PTS distribution: \d+(?:st|nd|rd|th) percentile/);
    await trigger.click();
    const drawer=page.getByRole('dialog');
    await expect(drawer).toBeVisible();await expect(drawer).toContainText('Among players');await expect(drawer).toContainText('League median');
    const activeBackground=await drawer.getByRole('button',{name:'Among players',exact:true}).evaluate(el=>getComputedStyle(el).backgroundColor);
    expect(activeBackground).not.toBe(await drawer.getByRole('button',{name:'Across games',exact:true}).evaluate(el=>getComputedStyle(el).backgroundColor));
    await page.screenshot({path:`reports/ask-harness/browser-artifacts/drawer-${width}-among.png`});
    await page.keyboard.press('Shift+Tab');expect(await drawer.evaluate(d=>d.contains(document.activeElement))).toBe(true);
    const rect=await drawer.boundingBox();expect(rect.width).toBeLessThanOrEqual(width);expect(rect.height).toBeLessThanOrEqual(900);
    await drawer.getByRole('button',{name:'Across games',exact:true}).click();
    await expect(drawer).toContainText('Game median');await expect(drawer.locator('circle')).not.toHaveCount(0);
    await drawer.locator('circle').first().focus();await expect(drawer.locator('circle').first()).toBeFocused();
    await expect(drawer.locator('[data-game-point-info]')).not.toHaveText('Hover, tap or focus a dot for game details.');
    await drawer.getByRole('combobox',{name:'Review a game'}).selectOption('0');await expect(drawer.locator('circle').first()).toBeFocused();
    expect(await drawer.getByRole('button',{name:'Across games',exact:true}).evaluate(el=>getComputedStyle(el).backgroundColor)).toBe(activeBackground);
    await page.screenshot({path:`reports/ask-harness/browser-artifacts/drawer-${width}-games.png`});
    await page.keyboard.press('Escape');await expect(drawer).not.toBeVisible();await expect(trigger).toBeFocused();
    expect((await table.boundingBox()).height).toBeCloseTo(height,1);expect(requests).toBe(0);
    await page.getByRole('button',{name:/^FT% distribution:/}).first().click();
    await expect(drawer).toContainText(/Not included in the qualified cohort|no marker comparison is shown/);
    await drawer.getByRole('button',{name:'Close metric details',exact:true}).click();await expect(drawer).not.toBeVisible();
    expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1)).toBe(true);
    await page.reload();await page.getByRole('button',{name:/^PTS distribution:/}).first().click();await expect(drawer).toBeVisible();
    await drawer.getByRole('button',{name:'Close metric details',exact:true}).click();
  });
}

for (const width of [1280, 390]) {
  for (const id of ['overview_group_recovery', 'overview_pair_recovery', 'overview_window_recovery', 'overview_prior_answer_recovery']) {
    test(`overview clarification recovers in the same conversation: ${id} at ${width}px`, async ({page}, testInfo) => {
      await page.setViewportSize({width, height:900});
      const c = await prepare(page, id);
      const posts = [];
      page.on('request', r => {
        if (r.method() === 'POST' && r.url().includes('/api/agent/ask/stream')) posts.push(r.postDataJSON());
      });
      // April includes 8 games totaling 56 points; the regular season has 30/6.
      const expected = id === 'overview_window_recovery' ? '7.0 points per game' : '5.0 points per game';
      await submit(page, c.turns[0].question);
      let populated = c.turns[0].checks[0].equals === 'ok' ? 1 : 0;
      await expect(page.locator('.overall-copy')).toHaveCount(populated);
      for (const turn of c.turns.slice(1)) {
        await page.locator('[data-followup-question]').fill(turn.question);
        await page.locator('[data-followup-submit]').click();
        await expect(page.locator('[data-followup-submit]')).toBeEnabled();
        if (turn.checks[0].equals === 'ok') populated++;
        await expect(page.locator('.overall-copy')).toHaveCount(populated);
      }
      await expect(page.locator('.overall-copy').last()).toContainText(expected);
      await expect(page.locator('[data-chat-tabs] [role=tab]')).toHaveCount(1);
      expect(posts).toHaveLength(c.turns.length);
      expect(new Set(posts.map(p => p.conversation_id)).size).toBe(1);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
      await page.reload();
      await expect(page.locator('.overall-copy').last()).toContainText(expected);
      await page.screenshot({path:testInfo.outputPath('clarification-recovered.png'), fullPage:true});
    });
  }
}
