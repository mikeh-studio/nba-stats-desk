import test from 'node:test';
import assert from 'node:assert/strict';
import {periodRange} from '../app/static/period_labels.js';
import {overviewPresentation} from '../app/static/ask_presentation.js';
import {formatClaim} from '../app/static/ask_claim_bindings.js';

test('annual regular-season bounds display official opening night and cutoff, including saved answers',()=>{
 const scope={start:'2026-07-01',end:'2026-10-28',previous_start:'2025-07-01',previous_end:'2025-10-28',phases:['Regular Season']};
 const before=JSON.stringify(scope);
 for(const s of [scope,{...scope,window_kind:'season'}]) {
   assert.equal(periodRange(s),'Oct 20, 2026 – Oct 28, 2026');
   assert.equal(periodRange(s,true),'Oct 21, 2025 – Oct 28, 2025');
   const view=overviewPresentation({semantic_evidence:{scope:s,metrics:[]}});
   assert.equal(view.range,periodRange(s));
   assert.equal(formatClaim(null,'drawer-scope',{evidence:{scope:s}}),`${view.range} · Regular Season`);
   assert.equal(formatClaim(null,'scope',{evidence:{scope:s}}),`Regular Season · ${view.range}`);
   assert.equal(formatClaim(null,'overview-scope',{evidence:{scope:s}}),`Versus ${view.comparison}`);
 }
 assert.equal(JSON.stringify(scope),before);
 assert.equal(periodRange({...scope,end:'2027-06-30'}),'Oct 20, 2026 – Jun 30, 2027');
});
test('opening dates are season-specific, phase-aware and never produce a reversed range',()=>{
 const s={start:'2026-07-01',end:'2026-09-01',phases:['Regular Season']};
 assert.equal(periodRange(s),'Season starts Oct 20, 2026 · As of Sep 1, 2026');
 assert.equal(periodRange({...s,end:'2026-10-28',phases:['Playoffs']}),'2026–27 season · Through Oct 28, 2026');
 assert.equal(periodRange({...s,start:'2024-07-01',end:'2024-10-28'}),'Oct 22, 2024 – Oct 28, 2024');
 assert.equal(periodRange({...s,start:'2023-07-01',end:'2023-10-28'}),'Oct 24, 2023 – Oct 28, 2023');
 assert.equal(periodRange({...s,start:'2027-07-01',end:'2027-10-28'}),'2027–28 season · Through Oct 28, 2027');
});
test('explicit July dates, custom windows and appearance windows retain their dates',()=>{
 assert.equal(periodRange({start:'2026-07-01',end:'2026-10-28',window_kind:'date_range'}),'Jul 1, 2026 – Oct 28, 2026');
 assert.equal(periodRange({start:'2026-10-21',end:'2026-10-26'}),'Oct 21, 2026 – Oct 26, 2026');
 assert.equal(periodRange({start:'2025-07-01',end:'2026-10-28'}),'Jul 1, 2025 – Oct 28, 2026');
});
