import test from 'node:test';
import assert from 'node:assert/strict';
import {overviewPresentation, overviewMetricsTable, overviewTakeaways, changeClass, metricAbbreviation} from '../app/static/ask_presentation.js';
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
 assert.doesNotMatch(primary.replace(/<span[^>]*class="metric-tooltip"[^>]*>.*?<\/span>/g,''),/<br>|per game<|Sample \/ opportunity/);
 assert.match(primary,/-0.6 pp/);assert.doesNotMatch(primary,/-0.0/);
 assert.match(html,/data-sample-details/);assert.doesNotMatch(html,/<details[^>]* open/);
 const contribution=overviewTakeaways(o);
 assert.match(contribution,/<h2>Key Takeaways<\/h2>/);assert.match(contribution,/<h3>Creation<\/h3>/);assert.doesNotMatch(contribution,/<details|<summary/);
});

test('change colors respect TOV, playing time, rounded zero and unavailable comparisons',()=>{
 assert.equal(changeClass({key:'pts'},1),'change-up');
 assert.equal(changeClass({key:'reb'},-1),'change-down');
 assert.equal(changeClass({key:'tov'},-1),'change-up');
 assert.equal(changeClass({key:'tov'},1),'change-down');
 for (const value of [0,-0.001,null,undefined,NaN]) assert.equal(changeClass({key:'pts'},value),'change-neutral');
 assert.equal(changeClass({key:'min'},3),'change-neutral');
 const p=payload(); p.semantic_evidence.metrics[0].display_change=2;
 p.semantic_evidence.metrics[5].display_change=-1;
 const html=overviewMetricsTable(overviewPresentation(p));
 assert.match(html,/class="change-up"[^>]*>\+2.0/);
 assert.match(html,/class="change-up"[^>]*>-1.0/);
});
test('abbreviations are consistent and TS tooltip uses pooled governed formula',()=>{
 const p=payload(), html=overviewMetricsTable(overviewPresentation(p));
 assert.deepEqual(p.semantic_evidence.metrics.slice(0,10).map(metricAbbreviation),['PTS','TS%','3PM','3P%','AST','TOV','REB','STL','BLK','MIN']);
 assert.match(html,/100 × SUM\(PTS\) ÷ \[2 × \(SUM\(FGA\) \+ 0.44 × SUM\(FTA\)\)\]/);
 assert.match(html,/popover="manual"/);assert.match(html,/Uses summed components from valid games/);
 const again=overviewMetricsTable(overviewPresentation(p));
 assert.notEqual(html.match(/id="(metric-help-[^"]+)"/)[1], again.match(/id="(metric-help-[^"]+)"/)[1]);
});

test('takeaways are visible, capped at three and escape saved source content',()=>{
 const o=overviewPresentation(payload());
 o.insights=[{title:'Clear <headline>',text:'A <script> fact',icon:'../../unsafe'},{title:'Second',text:'Fact two',icon:'shield'},{title:'Third',text:'Fact three',icon:'chart-line'},{title:'Fourth',text:'Should not appear',icon:'chart-bar'}];
 const html=overviewTakeaways(o);
 assert.equal((html.match(/class="takeaway"/g)||[]).length,3);
 assert.match(html,/Clear &lt;headline&gt;/);assert.match(html,/A &lt;script&gt; fact/);
 assert.doesNotMatch(html,/unsafe|Fourth|<details|<script>/);
 assert.match(html,/icons\/chart-bar.svg/);
 o.insights=[];assert.equal(overviewTakeaways(o),'');
});
