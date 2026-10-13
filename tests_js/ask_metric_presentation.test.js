import test from 'node:test';
import assert from 'node:assert/strict';
import {overviewPresentation, overviewMetricsTable, overviewContributions} from '../app/static/ask_presentation.js';
const keys=['pts','ts_pct','fg3m','fg3_pct','ast','tov','reb','stl','blk','min','fga'];
const payload=()=>({semantic_evidence:{scope:{start:'2025-01-01',end:'2025-02-01',previous_start:'2024-01-01',previous_end:'2024-02-01',phases:['Regular Season']},summary_paragraphs:['Canonical evidence.'],metrics:keys.map((key,i)=>({key,label:key,value:i,unit:key.includes('pct')?'percent':'per game',percentile:key==='ast'?99:null,change:1,valid_games:8,observed_games:8,sample:{status:'descriptive',text:'8/8 valid appearances'}})),presentation:{primary_keys:keys.slice(0,10),preferred_metric:'pts',scope_note:'Observed sample',limitation:'Defensive tracking unavailable',takeaways:[{area:'Creation',title:'Creation',text:'Assists and turnovers together',icon:'chart-bar'}]}}});
test('policy renders ten primary rows, collapses details and keeps canonical takeaways',()=>{
 const p=payload(), overview=overviewPresentation(p), html=overviewMetricsTable(overview);
 assert.equal(overview.metrics.length,10);assert.equal(overview.additionalMetrics.length,1);
 assert.equal(overview.preferredMetric,'pts');assert.deepEqual(overview.insights,p.semantic_evidence.presentation.takeaways);
 assert.equal((html.split('<details')[0].match(/data-metric-key=/g)||[]).length,10);
 assert.match(html,/View all metrics/);assert.match(html,/Sample \/ opportunity/);
 assert.match(html,/percent/);assert.match(html,/8\/8 valid appearances/);
});
test('focused policy follows provided keys and sample warnings survive escaping',()=>{
 const p=payload();p.semantic_evidence.presentation.primary_keys=['pts','ts_pct','fg3m','fg3_pct','fga','min'];
 p.semantic_evidence.metrics[0].sample={status:'limited',text:'2/8 valid appearances; limited sample <script>'};
 p.semantic_evidence.metrics[0].percentile=99;p.semantic_evidence.metrics[0].change=null;
 const o=overviewPresentation(p), html=overviewMetricsTable(o);
 assert.equal(o.metrics.length,6);assert.match(html,/limited sample &lt;script&gt;/);assert.doesNotMatch(html,/<script>/);
 assert.doesNotMatch(html.split('</tr>')[1],/99th/);
});

test('compact table has four columns with inline units and closed detail controls',()=>{
 const p=payload();p.semantic_evidence.metrics[1].display_change=-0.6;p.semantic_evidence.metrics[1].change_unit='percentage points';
 p.semantic_evidence.metrics[2].display_change=-0.001;
 const o=overviewPresentation(p),html=overviewMetricsTable(o),primary=html.split('<details')[0];
 assert.equal((primary.match(/scope="col"/g)||[]).length,4);
 assert.doesNotMatch(primary,/<br>|per game<|Sample \/ opportunity/);
 assert.match(primary,/-0.6 pp/);assert.doesNotMatch(primary,/-0.0/);
 assert.match(html,/data-sample-details/);assert.doesNotMatch(html,/<details[^>]* open/);
 const contribution=overviewContributions(o);
 assert.match(contribution,/<summary>Creation<\/summary>/);assert.doesNotMatch(contribution,/<details[^>]* open/);
});
