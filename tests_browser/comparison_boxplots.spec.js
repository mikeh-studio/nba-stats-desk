import {test, expect} from "@playwright/test";
import {comparisonFixture} from "../tests_js/ask_claim_fixture.js";

async function setup(page) {
  await page.goto('/ask');
  await page.evaluate(async payload=>{
    const c=await import('/static/ask_comparison.js'),a=await import('/static/ask_claim_bindings.js'),u=await import('/static/comparison_plot_ui.js');
    document.head.append(document.querySelector('link[href*="/static/ask.css"]'));
    const root=document.createElement('main');root.className='ask-desk';root.style.padding='16px';
    root.innerHTML=c.comparisonTables(c.comparisonPresentation(payload));document.body.replaceChildren(root);
    const report=a.bindRenderedClaims(root,payload,'comparison');
    if(report.errors.length)throw Error(JSON.stringify(report.errors));
    u.bindComparisonPlots(root);window.boxTest={root,payload,a};
  },comparisonFixture());
}

for(const width of [1280,390,320])test(`compact box plots, hover and keyboard details at ${width}px`,async({page},testInfo)=>{
  await page.setViewportSize({width,height:900});await setup(page);
  await expect(page.locator('.h2h-table meter,.h2h-edge')).toHaveCount(0);
  const alignment=await page.evaluate(()=>{
    const table=document.querySelector('.h2h-table').getBoundingClientRect();
    const parent=document.querySelector('main').getBoundingClientRect();
    const center=document.querySelector('.h2h-table thead th:nth-child(2)').getBoundingClientRect();
    return {width:table.width,available:parent.width-32,center:(center.left+center.right)/2,midpoint:(table.left+table.right)/2};
  });
  expect(Math.abs(alignment.width-alignment.available)).toBeLessThanOrEqual(2);
  expect(Math.abs(alignment.center-alignment.midpoint)).toBeLessThanOrEqual(2);
  await expect(page.locator('.h2h-boxplot')).toHaveCount(4);
  await expect(page.locator('.h2h-mean').first()).toHaveCSS('stroke','rgb(243, 241, 236)');
  expect(await page.locator('.h2h-value').allTextContents()).toEqual(['5.0avg','5.0avg','2.0avg','1.0avg']);
  const button=page.locator('.h2h-plot-trigger').first();
  const tip=page.locator('.h2h-plot-tooltip').first();
  await expect(tip).toBeHidden();await button.hover();await expect(tip).toBeVisible();
  await expect(tip.locator('dt')).toHaveText(['Med','Min','Max','Q1','Q3']);
  await expect(tip.locator('dd')).toHaveText(['5','0','10','2.5','7.5']);
  const bounds=await tip.boundingBox();expect(bounds.x).toBeGreaterThanOrEqual(0);expect(bounds.x+bounds.width).toBeLessThanOrEqual(width);
  await page.mouse.move(0,0);await expect(tip).toBeHidden();
  await button.focus();await expect(tip).toBeVisible();await button.press('Escape');await expect(tip).toBeHidden();
  await button.click();await expect(tip).toBeVisible();await button.click();await expect(tip).toBeHidden();
  const rank=page.locator('button.h2h-pct').first();await expect(rank).toHaveText('50th percentile');await rank.focus();
  await expect(page.locator('.h2h-rank-tooltip').first()).toContainText('League standing by per-game average');
  await expect(page.locator('.h2h-rank-tooltip').first()).toBeVisible();await rank.press('Escape');
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
  expect(await page.evaluate(()=>boxTest.root.uiClaimReport.errors)).toEqual([]);
  await page.screenshot({path:testInfo.outputPath(`boxplots-${width}.png`),fullPage:true});
});

test('tooltip values, identity, axis and plotted geometry are independently bound',async({page})=>{
  for(const [selector,attribute,value] of [
    ['.h2h-plot-tooltip [data-summary="median"]',null,'999'],
    ['.h2h-median','x1','99'],['.h2h-box','width','1'],['.h2h-whisker','x2','1'],
    ['.h2h-mean','points','0,0 1,1 2,2'],['.h2h-axis-max',null,'999'],
    ['.h2h-plot-trigger','aria-label','Wrong player'],['.h2h-pct',null,'1st percentile']
  ]){
    await setup(page);
    const result=await page.evaluate(({selector,attribute,value})=>{
      const t=boxTest,node=t.root.querySelector(selector);
      if(attribute)node.setAttribute(attribute,value);else node.textContent=value;
      return t.a.enforceRenderedClaims(t.root,t.payload);
    },{selector,attribute,value});
    expect(result.status).toBe('invalid');
    await expect(page.locator('main')).toContainText('could not be verified');
  }
});

test('touch opens and dismisses game statistics without submitting requests',async({browser})=>{
  const context=await browser.newContext({viewport:{width:390,height:844},hasTouch:true,isMobile:true});
  const page=await context.newPage();await setup(page);
  const button=page.locator('.h2h-plot-trigger').first(),tip=page.locator('.h2h-plot-tooltip').first();
  await button.tap();await expect(tip).toBeVisible();await button.tap();await expect(tip).toBeHidden();
  await context.close();
});
