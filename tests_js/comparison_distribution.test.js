import test from "node:test";
import assert from "node:assert/strict";
import {comparisonFixture} from "./ask_claim_fixture.js";
import {gameSummary, comparisonDistribution, boxPosition} from "../app/static/comparison_distribution.js";
import {comparisonPresentation, comparisonTables} from "../app/static/ask_comparison.js";
import {claimSchema, expectedClaim} from "../app/static/ask_claim_bindings.js";

const side = () => ({value:1, percentile:null, valid_games:4, observed_games:4, missing_component_games:0,
  distribution:{version:"ask_metric_distribution/1",quantile_method:"linear (n-1)",across_games:{
    summary:{n:4,min:0,q1:0,median:.5,q3:1.5,max:3},observed_games:4,missing_games:0,zero_denominator_games:0,
    points:[{value:3}],points_truncated:true,displayed_games:1}}});

test("box plots use full canonical game summaries, including zeros and unranked samples", () => {
  const left=side(), right={...side(),value:3};
  right.distribution=structuredClone(left.distribution);
  right.distribution.across_games.summary={n:4,min:0,q1:1.5,median:3,q3:4.5,max:6};
  const m={key:"stl",label:"Steals",left,right,delta:-2};
  const data=comparisonFixture(); data.semantic_evidence.metrics=[m];
  const before=JSON.stringify(data), scale=comparisonDistribution(m);
  assert.equal(scale.hi,6);
  assert.equal(gameSummary(left).median,.5);
  assert.equal(boxPosition(.5,scale),12.5);
  const html=comparisonTables(comparisonPresentation(data));
  assert.match(html,/class="h2h-box" x="5" y="6" width="22.5"/);
  assert.match(html,/data-summary="median">0.5/);
  assert.match(html,/Not ranked/);
  assert.doesNotMatch(html,/h2h-edge|<meter|\bmed<\/|class="median"/);
  const claims=claimSchema(data,"comparison");
  const med=claims.find(r=>r.format==="comparison-summary"&&r.source.at(-1)==="median");
  assert.equal(expectedClaim(data,med),"0.5");
  assert.deepEqual(med.source,["semantic_evidence","metrics",0,"left","distribution","across_games","summary","median"]);
  const position=claims.find(r=>r.selector.endsWith(".h2h-median")&&r.attribute==="x1");
  assert.equal(expectedClaim(data,position),"12.5");
  assert.equal(JSON.stringify(data),before);
});

test("missing, incomplete or malformed evidence never becomes an estimated distribution", () => {
  const mutations=[s=>delete s.distribution,s=>s.distribution.version="unknown",s=>s.missing_component_games=1,
    s=>s.distribution.across_games.missing_games=1,s=>s.distribution.across_games.summary.n=3,
    s=>s.distribution.across_games.summary.q1=5,s=>s.distribution.across_games.summary.max=null,
    s=>s.distribution.across_games.summary.median=NaN,s=>s.value=10,
    s=>s.observed_games=5,s=>s.distribution.across_games.zero_denominator_games=1];
  for(const mutate of mutations){const s=side();mutate(s);assert.equal(gameSummary(s),null);}
  const data=comparisonFixture(); delete data.semantic_evidence.metrics[0].left.distribution;
  const html=comparisonTables(comparisonPresentation(data));
  assert.match(html,/Distribution unavailable/);
  assert.match(html,/50th percentile/);
});

test("constant zero distributions retain zero width and finite geometry", () => {
  const s=side();s.value=0;s.distribution.across_games.summary={n:4,min:0,q1:0,median:0,q3:0,max:0};
  const scale=comparisonDistribution({left:s,right:s});
  assert.equal(scale.hi,1);assert.equal(boxPosition(0,scale),5);
  const data=comparisonFixture();data.semantic_evidence.metrics=[{key:"blk",label:"Blocks",left:s,right:s,delta:0}];
  const html=comparisonTables(comparisonPresentation(data));
  assert.match(html,/class="h2h-box" x="5" y="6" width="0"/);
  assert.match(html,/data-summary="median">0/);
  assert.doesNotMatch(html,/NaN|Infinity/);
});
