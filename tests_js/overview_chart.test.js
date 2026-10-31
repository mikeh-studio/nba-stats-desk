import test from 'node:test';
import assert from 'node:assert/strict';
import {overviewBoxplot,overviewTrend,boxplotContext} from '../app/static/overview_chart.js';
import {claimSchema,expectedClaim} from '../app/static/ask_claim_bindings.js';
const metric=()=>({key:'pts',label:'Points',unit:'per game',value:10,distribution:{across_games:{observed_games:3,summary:{n:3,min:0,q1:5,median:10,q3:15,max:20},points:[{date:'2026-10-21',value:0},{date:'2026-10-23',value:10},{date:'2026-10-26',value:20}]}}});
test('boxplot uses canonical quartiles and retains zero game values without changing evidence',()=>{
 const m=metric(),before=JSON.stringify(m),html=overviewBoxplot(m);
 assert.match(html,/3 of 3 appearances/);assert.equal((html.match(/<circle /g)||[]).length,3);
 assert.match(html,/data-box-stat="q1">5.0/);assert.match(html,/data-box-stat="median">10.0/);
 assert.match(html,/2026-10-21: 0.0/);assert.equal(JSON.stringify(m),before);
 const p={semantic_evidence:{metrics:[m],scope:{phases:['Regular Season'],start:'2026-07-01',end:'2026-10-28'}}};
 const claims=claimSchema(p,'overview',{chartKey:'pts'}).filter(r=>r.format.startsWith('box-'));
 assert.equal(claims.filter(r=>r.format==='box-value').length,5);
 assert.equal(claims.filter(r=>r.format==='box-point').length,6);
 assert.equal(expectedClaim(p,claims.find(r=>r.source.at(-1)==='median'&&r.format==='box-value')),'10.0');
});
test('empty distributions hide the plot; constant and single-game samples retain finite geometry',()=>{
 const m=metric();m.distribution.across_games.summary.n=0;assert.equal(overviewBoxplot(m),'');
 Object.assign(m.distribution.across_games.summary,{n:1,min:0,q1:0,median:0,q3:0,max:0});
 m.distribution.across_games.points=m.distribution.across_games.points.slice(0,1);
 assert.ok(boxplotContext(m));assert.doesNotMatch(overviewBoxplot(m),/NaN|Infinity/);
});
test('short trends use actual game observations; saved monthly averages never become fabricated games',()=>{
 const chart={grain:'monthly',series:[{key:'pts',points:[{x:'2026-10',y:10}]}]},m=metric();
 assert.deepEqual(overviewTrend(chart,m).series[0].points.map(p=>p.y),[0,10,20]);
 delete m.distribution;assert.equal(overviewTrend(chart,m),chart);
});
