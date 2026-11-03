import test from 'node:test';
import assert from 'node:assert/strict';
import {drawerContent,distributionChart} from '../app/static/metric_drawer.js';
import {overviewMetricsTable,overviewPresentation} from '../app/static/ask_presentation.js';
const metric=()=>({key:'ft_pct',label:'FT%',display_label:'FT%',value:76.6,unit:'percent',percentile:null,percentile_label:'Low attempts',percentile_note:'Requires 50 FTA',percentile_policy:{attempt_field:'fta',min_attempts:50},components:{fta:47},distribution:{among_players:{focal_in_cohort:false,summary:{n:2,min:60,q1:65,median:70,q3:75,max:80},bins:[{start:60,end:70,count:1},{start:70,end:80,count:1}]},across_games:{summary:{n:2,min:0,q1:25,median:50,q3:75,max:100},bins:[{start:0,end:100,count:2}],observed_games:3,missing_games:0,zero_denominator_games:1,points:[{date:'2025-01-01',value:100,attempts:1,attempt_field:'fta'},{date:'2025-01-02',value:0,attempts:9,attempt_field:'fta'}]}}});
const overview={playerName:'Klay <script>',range:'Jan 2025',scope:{phases:['Regular Season']}};
test('drawer separates peer and game summaries, identifies excluded marker and attempts',()=>{
 const m=metric(), peers=drawerContent(m,overview), games=drawerContent(m,overview,'across_games');
 assert.match(peers,/47 attempts; 50 required/);assert.match(peers,/Not included in the qualified cohort/);
 assert.match(peers,/stroke-dasharray/);assert.match(peers,/League median/);
 assert.match(games,/Game median/);assert.match(games,/Period pooled percentage/);assert.match(games,/1 with zero denominators omitted/);
 assert.match(games,/2025-01-02/);assert.match(games,/9 FTA/);
 assert.doesNotMatch(peers,/<script>/);assert.match(peers,/Klay &lt;script&gt;/);
});
test('empty and identical distributions render without invalid coordinates',()=>{
 const m=metric();m.distribution.among_players.summary.n=0;
 assert.match(distributionChart(m,'among_players'),/No qualified players/);
 m.distribution.among_players={focal_in_cohort:true,summary:{n:1,min:0,q1:0,median:0,q3:0,max:0},bins:[{start:-.5,end:.5,count:1}]};m.value=0;
 assert.doesNotMatch(distributionChart(m,'among_players'),/NaN|Infinity/);
});
test('distribution controls do not add table rows or alter saved answers without evidence',()=>{
 const m=metric();const p={player_profile:{player:{player_name:'Klay'}},semantic_evidence:{scope:{start:'2025-01-01',end:'2025-01-31',previous_start:'2024-12-01',previous_end:'2024-12-31',phases:['Regular Season']},metrics:[m],presentation:{primary_keys:['ft_pct']}}};
 const html=overviewMetricsTable(overviewPresentation(p));assert.doesNotMatch(html,/data-metric-distribution|metric-distribution-fallback/);
 delete m.distribution;assert.doesNotMatch(overviewMetricsTable(overviewPresentation(p)),/data-metric-distribution/);
});


test('unavailable period values do not claim a visible marker and malformed counts cannot inject HTML',()=>{
 const m=metric();m.value=null;
 const html=drawerContent(m,overview);assert.match(html,/no marker comparison is shown/);assert.doesNotMatch(html,/stroke-dasharray/);
 m.distribution.across_games.observed_games='<script>';m.distribution.across_games.summary.n='<script>';
 assert.doesNotMatch(drawerContent(m,overview,'across_games'),/<script>/);
});


test('TS game details disclose both shooting opportunity components',()=>{
 const m=metric();m.key='ts_pct';m.distribution.across_games.points[0].components={fga:10,fta:4};
 assert.match(drawerContent(m,overview,'across_games'),/10 FGA, 4 FTA/);
});
