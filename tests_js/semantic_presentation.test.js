import test from 'node:test';
import assert from 'node:assert/strict';
import {metricLabel, semanticTable, semanticText, scopeLabel} from '../app/static/semantic_presentation.js';
const table = {title:'Result: BLK', columns:[{key:'player',label:'Player'},{key:'value',label:'Value'},{key:'observed',label:'Games in scope'},{key:'valid',label:'Games with data'},{key:'rank',label:'Rank'},{key:'percentile',label:'Percentile'}],rows:[['Example Player','1.4','64','64','1','100.0']]};
const payload = {semantic_evidence:{metric:{key:'blk',label:'BLK',unit:'count'},scope:{operation:'rank',aggregation:'average',season:'2025-26',season_type:'Regular Season',window:'season_to_date',as_of:'2026-04-12',limit:50},cohort:{min_games:5,size:550,direction:'higher'}}};
test('saved rankings get names and units without mutating values or evidence',()=>{
 const original=JSON.stringify({table,payload});const view=semanticTable(table,payload,0);
 assert.equal(view.title,'Top 1 player by blocks per game');
 assert.deepEqual(view.rows,[['1','Example Player','1.4','64','100.0']]);
 assert.deepEqual(view.columns.map(c=>c.label),['Rank','Player','Blocks per game','Games played','League percentile']);
 assert.match(view.qualification,/1 of 50 requested results available/);
 assert.equal(JSON.stringify({table,payload}),original);
});
test('incomplete counts are explicit and null values remain unavailable',()=>{
 const partial={...table,rows:[['Example Player','Unavailable','64','62','—','Unavailable']]};
 const view=semanticTable(partial,payload,0);
 assert.equal(view.columns[3].label,'Games used');assert.equal(view.rows[0][3],'62 of 64');assert.equal(view.rows[0][2],'Unavailable');
});
test('average, total, shooting and per-36 units are distinct',()=>{
 assert.equal(metricLabel({key:'pts',unit:'count'},{aggregation:'total'}),'Total points');
 assert.equal(metricLabel({key:'pts',unit:'count'},{aggregation:'average'}),'Points per game');
 assert.equal(metricLabel({key:'ts_pct',unit:'ratio'},{aggregation:'ratio'}),'True shooting percentage');
 assert.equal(metricLabel({key:'pts_per36',unit:'per36'},{aggregation:'ratio'}),'Points per 36 minutes');
 assert.equal(metricLabel({key:'blk',unit:'count'},{operation:'game_log',aggregation:'average'}),'Blocks');
});
test('cutoff is not described as a refresh or a full season; all filters remain',()=>{
 const label=scopeLabel({...payload.semantic_evidence.scope,window:'last_n_games',n:10,team_abbr:'BOS',opponent_abbr:'NYK',home_away:'AWAY'});
 assert.match(label,/Last 10 games/);assert.match(label,/Through Apr 12, 2026/);assert.match(label,/Team: BOS.*Against NYK.*Away games/);assert.doesNotMatch(label,/Full season|updated|season_to_date/);
});
test('lower-first ranking and shooting attempt qualifications stay explicit',()=>{
 const p=structuredClone(payload);p.semantic_evidence.metric={key:'fg3_pct',label:'3P%',unit:'ratio'};Object.assign(p.semantic_evidence.cohort,{direction:'lower',min_attempts:100,attempt_field:'fg3a'});
 const view=semanticTable(table,p,0);assert.match(view.title,/lowest first/);assert.match(view.qualification,/100 three-point attempts/);assert.match(view.help,/Lower values rank first/);assert.match(view.help,/zero/);
 assert.equal(semanticText('3P% 44.2%',p),'three-point percentage 44.2%');
});
test('unrecognized and unrelated payloads keep their original table',()=>{
 assert.equal(semanticTable(table,{},0),table);
 assert.equal(semanticTable(table,{semantic_evidence:{metric:{key:'unknown'},scope:{}}},0),table);
});
