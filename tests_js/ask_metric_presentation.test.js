import test from 'node:test';
import assert from 'node:assert/strict';
import {overviewPresentation, overviewMetricsTable, overviewTakeaways, changeClass, metricAbbreviation} from '../app/static/ask_presentation.js';
import {claimSchema} from '../app/static/ask_claim_bindings.js';
const keys=['pts','ts_pct','fg3m','fg3_pct','ast','tov','reb','stl','blk','min','fga'];
const payload=()=>({semantic_evidence:{scope:{start:'2025-01-01',end:'2025-02-01',previous_start:'2024-01-01',previous_end:'2024-02-01',phases:['Regular Season']},summary_paragraphs:['Canonical evidence.'],metrics:keys.map((key,i)=>({key,label:key,value:i,unit:key.includes('pct')?'percent':'per game',percentile:key==='ast'?99:null,change:1,valid_games:8,observed_games:8,sample:{status:'descriptive',text:'8/8 valid appearances'}})),presentation:{primary_keys:keys.slice(0,10),preferred_metric:'pts',scope_note:'Observed sample',limitation:'Defensive tracking unavailable',takeaways:[{area:'Creation',title:'Creation',text:'Assists and turnovers together',icon:'chart-bar'}]}}});
test('overview hides empty percentile and change columns without losing zero, distributions or source evidence',()=>{
 const p=payload();p.semantic_evidence.metrics.forEach(m=>{m.percentile=null;m.display_change=null;});
 p.semantic_evidence.metrics[0].distribution={across_games:{summary:{n:1},points:[{value:0}]}};
 const before=JSON.stringify(p),html=overviewMetricsTable(overviewPresentation(p));
 const primary=html.split('</table>')[0];
 assert.equal((primary.match(/scope="col"/g)||[]).length,2);
 assert.doesNotMatch(html,/data-column="percentile"|data-column="change"|Percentiles use complete/);
 assert.doesNotMatch(html,/Versus |TOV colors/);
 assert.match(primary,/data-column="value" aria-label="0.0 per game">0.0/);
 assert.doesNotMatch(primary,/data-metric-distribution|metric-distribution-fallback/);
 const claims=claimSchema(p,'overview');
 assert.ok(!claims.some(r=>['change','change-aria','overview-meter','ordinal'].includes(r.format)));
 assert.ok(claims.some(r=>r.format==='overview-value'&&r.selector.includes('data-column="value"')));
 assert.equal(JSON.stringify(p),before);
 p.semantic_evidence.metrics[0].distribution={across_games:{summary:{n:0},points:[]}};
 assert.doesNotMatch(overviewMetricsTable(overviewPresentation(p)),/data-metric-distribution/);
});
test('overview visibility is per table and retains mixed columns with valid zero ranks and changes',()=>{
 const p=payload();p.semantic_evidence.metrics.forEach(m=>{m.value=null;m.percentile=null;m.display_change=null;});
 Object.assign(p.semantic_evidence.metrics.at(-1),{value:0,percentile:0,display_change:0});
 let html=overviewMetricsTable(overviewPresentation(p));
 assert.doesNotMatch(html.split('</table>')[0],/data-column=/);
 assert.match(html.split('data-additional-metrics')[1],/data-column="value".*0.0/);
 assert.equal(claimSchema(p,'overview').filter(r=>r.format==='overview-value').length,1);
 Object.assign(p.semantic_evidence.metrics[0],{value:0,percentile:0,display_change:0});
 html=overviewMetricsTable(overviewPresentation(p)).split('</table>')[0];
 assert.equal((html.match(/scope="col"/g)||[]).length,4);
 assert.match(html,/0th/);assert.match(html,/Comparison unavailable/);
 assert.equal((html.match(/data-column="value"/g)||[]).length,10);
 p.semantic_evidence.metrics[0].sample.status='limited';
 assert.doesNotMatch(overviewMetricsTable(overviewPresentation(p)).split('</table>')[0],/data-column="percentile"/);
});
test('policy renders ten primary rows, collapses details and keeps canonical takeaways',()=>{
 const p=payload(), overview=overviewPresentation(p), html=overviewMetricsTable(overview);
 assert.equal(overview.metrics.length,10);assert.equal(overview.additionalMetrics.length,1);
 assert.equal(overview.preferredMetric,'pts');assert.deepEqual(overview.insights,p.semantic_evidence.presentation.takeaways);
 assert.equal((html.split('<details')[0].match(/data-metric-key=/g)||[]).length,10);
 assert.match(html,/View Other Metrics/);assert.doesNotMatch(html,/Samples &amp; methodology|data-sample-details/);
 assert.match(html,/percent/);assert.ok(html.indexOf('data-overview-performance') < html.indexOf('data-additional-metrics'));
});
test('focused policy follows provided keys and sample warnings survive escaping',()=>{
 const p=payload();p.semantic_evidence.presentation.primary_keys=['pts','ts_pct','fg3m','fg3_pct','fga','min'];
 p.semantic_evidence.metrics[0].sample={status:'limited',text:'2/8 valid appearances; limited sample <script>'};
 p.semantic_evidence.metrics[0].percentile=99;p.semantic_evidence.metrics[0].change=null;p.semantic_evidence.metrics[0].valid_games=2;
 const o=overviewPresentation(p), html=overviewMetricsTable(o);
 assert.equal(o.metrics.length,6);assert.match(html,/2 of 8 appearances/);assert.doesNotMatch(html,/>Limited</);assert.doesNotMatch(html,/<script>/);
 assert.doesNotMatch(html.split('</tr>')[1],/99th/);
});

test('compact table has four columns with inline units and closed detail controls',()=>{
 const p=payload();p.semantic_evidence.metrics[1].display_change=-0.6;p.semantic_evidence.metrics[1].change_unit='percentage points';
 p.semantic_evidence.metrics[2].display_change=-0.001;
 const o=overviewPresentation(p),html=overviewMetricsTable(o),primary=html.split('<details')[0];
 assert.equal((primary.match(/scope="col"/g)||[]).length,4);
 assert.doesNotMatch(primary.replace(/<span[^>]*class="metric-tooltip"[^>]*>.*?<\/span>/g,''),/<br>|per game<|Sample \/ opportunity/);
 assert.match(primary,/-0.6 pp/);assert.doesNotMatch(primary,/-0.0/);
 assert.doesNotMatch(html,/data-sample-details/);assert.doesNotMatch(html,/<details[^>]* open/);
 const contribution=overviewTakeaways(o);
 assert.match(contribution,/>Key takeaways<\/h2>/);assert.match(contribution,/<h3>Creation<\/h3>/);assert.doesNotMatch(contribution,/<details|<summary/);
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
 assert.equal((html.match(/class="takeaway-card has-icon"/g)||[]).length,3);
 assert.match(html,/Clear &lt;headline&gt;/);assert.match(html,/A &lt;script&gt; fact/);
 assert.doesNotMatch(html,/unsafe|Fourth|<details|<script>/);
 assert.match(html,/icons\/chart-bar.svg/);
 o.insights=[];assert.equal(overviewTakeaways(o),'');
});

test('qualified ranks and compact reasons are accessible and escaped',()=>{
 const p=payload();
 Object.assign(p.semantic_evidence.metrics[0],{percentile:null,percentile_status:'low_attempts',percentile_label:'Low attempts',percentile_note:'Requires 100 FGA <script>',percentile_policy:{version:'ask_overview_percentiles/1'}});
 Object.assign(p.semantic_evidence.metrics[9],{percentile:75,percentile_status:'ranked',percentile_note:'Playing-time volume'});
 const html=overviewMetricsTable(overviewPresentation(p));
 assert.match(html,/Low attempts/);assert.match(html,/tabindex="0"/);
 assert.match(html,/Requires 100 FGA &lt;script&gt;/);assert.doesNotMatch(html,/<script>/);
 assert.match(html,/percentile-neutral/);assert.match(html,/75th/);
 assert.doesNotMatch(html,/data-sample-details/);
});


test('new general policy renders eleven rows with percentages last, saved policy stays intact',()=>{
 const p=payload(), keys=['pts','reb','ast','fg3m','stl','blk','tov','min','fg_pct','ft_pct','ts_pct'];
 p.semantic_evidence.presentation.primary_keys=keys;
 for (const key of ['fg_pct','ft_pct']) p.semantic_evidence.metrics.push({key,label:key,value:70,unit:'percent',percentile:50,valid_games:8,sample:{status:'descriptive',text:'Qualified'},percentile_policy:{version:'ask_overview_percentiles/1'}});
 const o=overviewPresentation(p), html=overviewMetricsTable(o);
 assert.equal(o.metrics.length,11);
 assert.deepEqual(o.metrics.slice(-3).map(metricAbbreviation),['FG%','FT%','TS%']);
 assert.doesNotMatch(html,/data-sample-details/);
 assert.doesNotMatch(overviewMetricsTable(overviewPresentation(payload())),/Saved ranking policy/);
});


test('distribution buttons announce percentile or qualification and descriptive policy',()=>{
 const p=payload();const m=p.semantic_evidence.metrics[0];
 Object.assign(m,{percentile:75,percentile_status:'ranked',percentile_note:'Complete appearances, descriptive rank',distribution:{}});
 let html=overviewMetricsTable(overviewPresentation(p));
 assert.match(html,/aria-label="PTS distribution: 75th percentile"/);
 assert.match(html,/aria-description="Complete appearances, descriptive rank"/);
 Object.assign(m,{percentile:null,percentile_label:'Low attempts'});
 html=overviewMetricsTable(overviewPresentation(p));
 assert.match(html,/aria-label="PTS distribution: Low attempts"/);
});

test('scope metadata preserves canonical text and saved responses need no migration',()=>{
 const p=payload();
 p.semantic_evidence.summary_context='Scope: 19 appearances · baseline: 74 appearances.';
 p.semantic_evidence.summary_paragraphs.push(p.semantic_evidence.summary_context);
 const o=overviewPresentation(p);
 assert.equal(o.summaryContext,p.semantic_evidence.summary_context);
 assert.deepEqual(o.paragraphs,p.semantic_evidence.summary_paragraphs);
 delete p.semantic_evidence.summary_context;
 assert.equal(overviewPresentation(p).summaryContext,'');
});

test('position evidence has its own labeled distribution and preserves league table',()=>{
 const p=payload(), m=p.semantic_evidence.metrics[0];
 m.peer_comparison={kind:'position',group:'Guard',mode:'ordinal',rank:4,tied:true,cohort_size:14,season:'2024-25',phases:['Regular Season'],start:'2024-07-01',end:'2025-06-30',qualification:{min_games:5},distribution:{among_players:{bins:[{start:1,end:2,count:4},{start:2,end:3,count:10}]}}};
 const html=overviewMetricsTable(overviewPresentation(p));
 assert.match(html,/League percentile/);assert.match(html,/data-peer-details/);
 assert.match(html,/Tied 4th of 14/);assert.match(html,/PTS Guards distribution, 14 qualifying players/);
 assert.match(html,/1.0 to 2.0: 4 players/);
 m.peer_comparison.mode='withheld';assert.doesNotMatch(overviewMetricsTable(overviewPresentation(p)),/data-peer-details/);
});

test('variation chart uses focused metric, median band, dates and escaped opponents',async()=>{
 const {gameVariationDetails}=await import('../app/static/ask_presentation.js');
 const p=payload(), m=p.semantic_evidence.metrics.find(m=>m.key==='reb');
 m.distribution={across_games:{points:[{date:'2024-11-01',value:2,opponent:'<script>'},{date:'2024-11-05',value:16,opponent:'XYZ'}]}};
 p.semantic_evidence.insights=[{game_variation:{metric_key:'reb',summary:{n:5,min:0,max:16,q1:2,median:4,q3:8},spread_text:'Median 4 rebounds.',peak_text:'16 rebounds on 2024-11-05 against XYZ.'}}];
 const html=gameVariationDetails(overviewPresentation(p));
 assert.match(html,/REB game-by-game spread/);assert.match(html,/2024-11-05/);
 assert.match(html,/stroke-dasharray/);assert.match(html,/&lt;script&gt;/);assert.doesNotMatch(html,/<script>/);
 assert.equal((html.match(/<circle /g)||[]).length,2);
 assert.equal(gameVariationDetails({...overviewPresentation(p),gameVariation:null}), '');
});

test('unfulfilled position request never displays a league peer histogram as its answer',()=>{
 const p=payload(); p.semantic_evidence.metrics[0].peer_comparison={mode:'withheld',kind:'unavailable',requested_group:'Wing'};
 const html=overviewMetricsTable(overviewPresentation(p));
 assert.doesNotMatch(html,/data-peer-details/);
 assert.match(html,/League percentile/);
});

test('league-only peer details omit historical-position methodology',()=>{
 const p=payload();
 p.semantic_evidence.metrics[0].peer_comparison={kind:'league',group:null,mode:'ordinal',rank:2,cohort_size:15,season:'2024-25',phases:['Regular Season'],qualification:{min_games:5},distribution:{among_players:{bins:[]}}};
 const html=overviewMetricsTable(overviewPresentation(p));
 assert.match(html,/League-wide/);
 assert.doesNotMatch(html,/Historical listed positions|Hybrids belong/);
});
