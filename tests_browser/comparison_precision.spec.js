import {test, expect} from "@playwright/test";
import {comparisonFixture} from "../tests_js/ask_claim_fixture.js";

for (const width of [1280,390]) {
  test(`similar rates are neutral, precision is accessible and claims remain bound at ${width}px`, async ({page}, testInfo) => {
    await page.setViewportSize({width,height:1000});
    await page.goto("/ask");
    const data=comparisonFixture();
    const side=(value,percentile)=>({value,percentile,valid_games:20,observed_games:20,missing_component_games:0,cohort_size:100});
    data.semantic_evidence.metrics=[
      ["pts","Points",31.1,27.7,99,98], ["reb","Rebounds",4.3,12.9,69,99],
      ["ast","Assists",6.6,10.7,96,99], ["stl","Steals",1.401,1.419,93,94],
      ["blk","Blocks",0.779,0.827,86,87]
    ].map(([key,label,a,b,ap,bp])=>({key,label,left:side(a,ap),right:side(b,bp),delta:a-b}));
    await page.evaluate(async (payload)=>{
      const c=await import("/static/ask_comparison.js"), a=await import("/static/ask_claim_bindings.js");
      const stylesheet=document.querySelector('link[href*="/static/ask.css"]');
      if (!stylesheet) throw Error("Comparison stylesheet missing");
      document.head.append(stylesheet); // Preserve the real styling when replacing the fixture body.
      const root=document.createElement("main"); root.className="ask-desk"; root.style.padding="16px";
      const view=c.comparisonPresentation(payload);
      root.innerHTML=c.comparisonProfiles(view)+c.comparisonAnswer(view)+`<section id="comparison-tables">${c.comparisonTables(view)}</section>`;
      document.body.replaceChildren(root);
      const tables=document.querySelector("#comparison-tables");
      window.precisionTest={a,tables,payload,session:null};
      const result=a.bindRenderedClaims(tables,payload,"comparison");
      window.precisionTest.session=tables.uiClaimSession;
      if(result.errors.length) throw Error(JSON.stringify(result.errors));
    },data);
    await expect(page.getByText("Comparison summary",{exact:true})).toBeVisible();
    await expect(page.locator(".lean-meter")).toHaveCount(0);
    for(const key of ["STL","BLK"]){
      const row=page.locator(".h2h-table tbody tr").filter({has:page.locator(".h2h-label",{hasText:key})});
      await expect(row).toContainText("Similar rates");
      await expect(row.locator(".is-leader,.is-trailing,.h2h-edge")).toHaveCount(0);
    }
    await page.getByText("More precision & comparison rules",{exact:true}).click();
    await expect(page.locator(".comparison-precision")).toContainText("not a test of statistical significance");
    await expect(page.locator(".precision-table")).toContainText("1.401");
    await expect(page.locator(".precision-table")).toContainText("0.018");
    expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1)).toBe(true);
    await page.screenshot({path:testInfo.outputPath(`similar-rates-${width}.png`),fullPage:true});
    const result=await page.evaluate(()=>{
      const t=window.precisionTest;
      t.tables.querySelector('.comparison-precision td[data-side="left"]').textContent="999.000";
      return t.a.validateRenderedClaims(t.tables,t.payload,t.session);
    });
    expect(result.status).toBe("invalid");
  });
}
