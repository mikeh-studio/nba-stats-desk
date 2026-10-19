import { test, expect } from '@playwright/test';
import fs from 'node:fs';

// Exercise the actual self-contained review template without private reports.
const cases = ['first', 'second'].map(id => ({
  id, family: 'Review controls', source_variant: 'complete',
  oracle_note: 'Synthetic UI fixture', turns: [{ question: `Review ${id}`, checks: [] }],
}));
const turn = { payload: { answer: 'Synthetic answer', tables: [] }, checks: [], passed: true, http_status: 200, latency_ms: 1 };
const run = {
  cases, total: 2, passed: 2, provider: 'controlled', model: 'predeclared-plan',
  case_sha256: 'review-test-cases', source_sha256: 'review-test-source', code_sha256: 'review-test-code',
  results: cases.map(c => ({ id: c.id, passed: true, parity: true, modes: { json: [turn], sse: [turn] } })),
};
const html = fs.readFileSync('scripts/ask_eval/review.html', 'utf8').replace('/*__DATA__*/', JSON.stringify({
  baseline: run, candidate: run, source: { description: 'Synthetic UI fixture' },
}));

test.beforeEach(async ({ page }) => {
  await page.route('**/__review-test', route => route.fulfill({ contentType: 'text/html', body: html }));
  await page.goto('/__review-test');
});

test('save requires an explicit decision, with feedback beside the button', async ({ page }) => {
  await page.locator('#save').click();
  await expect(page.locator('#review-save-state')).toContainText('Choose');
  await expect(page.locator('#decision')).toBeFocused();
  await expect(page.locator('#stats')).toContainText('0 / 2');
  expect(await page.evaluate(() => localStorage.length)).toBe(0);
});

test('reviewer and notes are optional for saving, counting, reload and export', async ({ page }) => {
  await expect(page.getByLabel('Reviewer (optional)')).toBeVisible();
  await expect(page.getByLabel('Review notes (optional)')).toBeVisible();
  await page.locator('#decision').selectOption('revise');
  await page.locator('#save').click();
  await expect(page.locator('#review-save-state')).toContainText('Saved locally: Needs revision.');
  await expect(page.locator('#stats')).toContainText('1 / 2');
  await page.reload();
  await expect(page.locator('#decision')).toHaveValue('revise');
  await expect(page.locator('#reviewer')).toHaveValue('');
  await expect(page.locator('#notes')).toHaveValue('');
  await expect(page.locator('[data-case="first"]')).toContainText('revise');
  const downloaded = page.waitForEvent('download');
  await page.locator('#export').click();
  const data = JSON.parse(fs.readFileSync(await (await downloaded).path(), 'utf8'));
  expect(data.decisions.first).toMatchObject({ reviewer: '', decision: 'revise', notes: '' });
});

test('review guidance distinguishes human judgment from automated checks and the old baseline', async ({ page }) => {
  const guide = page.locator('[data-review-guide]');
  await expect(guide).toContainText('right player, season, phase');
  await expect(guide).toContainText('expected calculation');
  await expect(guide).toContainText('wording match the evidence');
  await expect(guide).toContainText('Passing automated checks is not human approval');
  await expect(guide).toContainText('do not need to approve the old answer');
});

test('a saved decision updates the case, survives reload, and exports unchanged', async ({ page }) => {
  await page.locator('#reviewer').fill('Test reviewer');
  await page.locator('#decision').selectOption('approved');
  await page.locator('#notes').fill('Checked the expected values.');
  await page.locator('#save').click();
  await expect(page.locator('#review-save-state')).toContainText('Saved locally');
  await expect(page.locator('#stats')).toContainText('1 / 2');
  await expect(page.locator('[data-case="first"]')).toContainText('approved');
  await page.locator('[data-case="second"]').click();
  await page.locator('[data-case="first"]').click();
  await expect(page.locator('#notes')).toHaveValue('Checked the expected values.');
  await page.reload();
  await expect(page.locator('#reviewer')).toHaveValue('Test reviewer');
  await expect(page.locator('#decision')).toHaveValue('approved');
  await expect(page.locator('#review-save-state')).toContainText('Saved locally');
  const downloaded = page.waitForEvent('download');
  await page.locator('#export').click();
  const data = JSON.parse(fs.readFileSync(await (await downloaded).path(), 'utf8'));
  expect(data.decisions.first).toMatchObject({ reviewer: 'Test reviewer', decision: 'approved', notes: 'Checked the expected values.' });
});

test('storage failure shows an inline warning and preserves the decision for export', async ({ page }) => {
  await page.evaluate(() => { Storage.prototype.setItem = () => { throw new DOMException('Full', 'QuotaExceededError'); }; });
  await page.locator('#reviewer').fill('Test reviewer');
  await page.locator('#decision').selectOption('revise');
  await page.locator('#save').click();
  await expect(page.locator('#review-save-state')).toContainText('Not saved');
  await expect(page.locator('#review-save-state')).toContainText('Export');
  await page.locator('[data-case="second"]').click();
  await page.locator('[data-case="first"]').click();
  await expect(page.locator('#review-save-state')).toContainText('Not saved');
  const downloaded = page.waitForEvent('download');
  await page.locator('#export').click();
  const data = JSON.parse(fs.readFileSync(await (await downloaded).path(), 'utf8'));
  expect(data.decisions.first.decision).toBe('revise');
});

for (const [checks, expected] of [
  [[{path:'semantic_evidence.kind',equals:'recent_statistical_similarity'},{path:'semantic_evidence.rows.0.similarity_score',equals:0.5}], 'each player’s own last-N appearances'],
  [[{path:'participation_evidence.participated',equals:false},{path:'tables',equals:[]}], 'must not be inferred from missing box scores'],
]) {
  test(`specific review guidance: ${expected}`, async ({page}) => {
    const data=structuredClone({baseline:run,candidate:run,source:{description:'Synthetic UI fixture'}});
    data.candidate.cases[0].turns[0].checks=checks;
    const body=fs.readFileSync('scripts/ask_eval/review.html','utf8').replace('/*__DATA__*/',JSON.stringify(data));
    await page.route('**/__review-test',route=>route.fulfill({contentType:'text/html',body}));
    await page.reload();
    await expect(page.locator('[data-review-guide]')).toContainText(expected);
  });
}

test('coverage separates distinct scenarios from paraphrase variants and exposes families', async ({ page }) => {
  const expanded = { ...run, coverage: { distinct: {total: 100, passed: 100}, paraphrases: {total: 24, passed: 24} }, families: {'Availability success': {total: 8, passed: 8}} };
  const body = fs.readFileSync('scripts/ask_eval/review.html','utf8').replace('/*__DATA__*/',JSON.stringify({baseline:expanded,candidate:expanded,source:{description:'Synthetic coverage fixture'}}));
  await page.route('**/__coverage-review',route=>route.fulfill({contentType:'text/html',body}));
  await page.goto('/__coverage-review');
  await expect(page.locator('#coverage-summary')).toContainText('100/100 distinct scenarios passed; 24/24 paraphrase variants passed separately');
  await expect(page.locator('#coverage-summary')).toContainText('Availability success');
  await expect(page.locator('#stats .stat').first()).toContainText('100');
});

for (const [id, expected] of [
  ['availability_points', ['both-played and verified nonparticipation groups', 'game IDs, sample counts and exclusions', 'pregame Out report with no appearance or verified zero-minute DNP', 'never become proof of absence or injury']],
  ['research_home', ['exact player, filters, phase and date window', 'pooled shooting ratios', 'valid versus observed samples, and missing components', 'rather than becoming zero']],
  ['research_published_study', ['contrast direction, sample counts and confidence interval', 'frozen published study', 'Distinguish raw and adjusted estimates', 'not validated significance or evidence of a causal effect']],
  ['boundary_study_wrong_dates', ['clarification or refusal is warranted', 'without substituting an answer or inventing numbers']],
]) {
  test(`review guidance uses the actual ${id} case contract`, async ({page}) => {
    const fixture = JSON.parse(fs.readFileSync('tests/fixtures/ask/cases.json','utf8')).cases.find(c=>c.id===id);
    const scopedRun = structuredClone(run);
    scopedRun.cases = [fixture];
    scopedRun.results = [{id,passed:true,parity:true,modes:{json:[turn],sse:[turn]}}];
    const body = fs.readFileSync('scripts/ask_eval/review.html','utf8').replace('/*__DATA__*/',JSON.stringify({baseline:scopedRun,candidate:scopedRun,source:{description:'Synthetic contract fixture'}}));
    await page.route('**/__case-review',route=>route.fulfill({contentType:'text/html',body}));
    await page.goto('/__case-review');
    const guide = page.locator('[data-review-guide]');
    for (const text of expected) await expect(guide).toContainText(text);
    await expect(guide).not.toContainText('Confirm the expected calculation matches the question');
    await expect(guide).toContainText('Passing automated checks is not human approval');
  });
}


test('oracle provenance stays separate from browser approval and pass rate', async ({ page }) => {
  const expanded = { ...run, execution_mode: 'controlled_planner_frozen_source_http',
    corpus_summary: { counts: { cases: 147 } },
    selection_summary: { counts: { cases: 2, source_rows: 24 }, review_states: { pending: 2 }, cases: { first: { review: { status: 'pending', reviewer: null } } } },
  };
  const body = fs.readFileSync('scripts/ask_eval/review.html','utf8').replace('/*__DATA__*/',JSON.stringify({baseline:expanded,candidate:expanded,source:{description:'Synthetic provenance fixture'}}));
  await page.route('**/__provenance-review',route=>route.fulfill({contentType:'text/html',body}));
  await page.goto('/__provenance-review');
  const panel = page.getByRole('region', {name:'Oracle provenance'});
  await expect(panel).toContainText('controlled_planner_frozen_source_http');
  await expect(panel).toContainText('Selected cases: 2; full corpus: 147; source rows: 24');
  await expect(panel).toContainText('"pending":2');
  await page.locator('#decision').selectOption('approved');
  await page.locator('#save').click();
  await expect(page.locator('#stats')).toContainText('1 / 2');
  await expect(panel).toContainText('"pending":2');
  await panel.locator('summary').click();
  await expect(panel.locator('pre')).toContainText('"reviewer": null');
});


for (const malformed of [false, true]) {
  test(`optional provenance remains safe and usable: malformed=${malformed}`, async ({ page }) => {
    const malicious = '<img src=x onerror="window.provenanceInjected=true">';
    const expanded = { ...run, execution_mode: malicious,
      corpus_summary: {counts:{cases:malicious}},
      selection_summary: malformed ? {} : {counts:{cases:malicious,source_rows:malicious},review_states:{pending:2}},
    };
    const body=fs.readFileSync('scripts/ask_eval/review.html','utf8').replace('/*__DATA__*/',JSON.stringify({baseline:expanded,candidate:expanded,source:{}}).replaceAll('<','\\u003c'));
    await page.route('**/__unsafe-provenance',route=>route.fulfill({contentType:'text/html',body}));
    await page.goto('/__unsafe-provenance');
    const panel=page.getByRole('region',{name:'Oracle provenance'});
    if(malformed) await expect(panel).toContainText('Oracle provenance unavailable');
    else {await expect(panel).toContainText(malicious);await expect(panel.locator('img')).toHaveCount(0);}
    expect(await page.evaluate(()=>window.provenanceInjected)).toBeUndefined();
    await page.locator('#decision').selectOption('revise');
    await page.locator('#save').click();
    await expect(page.locator('#review-save-state')).toContainText('Saved locally');
    const downloaded=page.waitForEvent('download');await page.locator('#export').click();
    const data=JSON.parse(fs.readFileSync(await (await downloaded).path(),'utf8'));
    expect(data.decisions.first.decision).toBe('revise');
  });
}
