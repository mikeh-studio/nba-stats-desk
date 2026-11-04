import test from 'node:test';
import assert from 'node:assert/strict';
import {comparisonWindow, publicationRead, initCompare, initPerformance} from '../app/static/production_compare_performance.js';

test('comparison only maps publication-supported windows', () => {
  assert.equal(comparisonWindow('regular_season'), 'scope');
  assert.equal(comparisonWindow('last_5'), 'last_5');
  assert.equal(comparisonWindow('playoffs'), null);
  assert.equal(comparisonWindow('last_3'), null);
});

class Element {
  constructor() { this.children=[]; this.listeners={}; this.value=''; this.textContent=''; this.disabled=false; }
  appendChild(value) { this.children.push(value); }
  add(value) { this.children.push(value); }
  replaceChildren(...values) { this.children=values; }
  addEventListener(name, fn) { this.listeners[name]=fn; }
}

test('comparison and performance controls display pinned publication data', async () => {
  const token='pub_0123456789abcdef';
  globalThis.document={createElement:()=>new Element()};
  globalThis.Option=class { constructor(text,value) {this.text=text; this.value=value;} };
  const status=new Element(), results=new Element(), date=new Element(), game=new Element();
  const nodes={'[data-publication-status]':status,'[data-publication-results]':results,'[data-publication-date]':date,'[data-publication-game]':game};
  const card={dataset:{playerA:'101',playerB:'202',window:'last_5'},querySelector:key=>nodes[key]};
  const requests=[];
  globalThis.fetch=async (url, options)=>{
    requests.push({url, options});
    let data;
    if (url.includes('/api/compare')) data={differences:[{metric:'pts',aggregation:'total',difference:3,unit:'count'}]};
    else if (url.includes('/dates')) data=['2026-10-20'];
    else if (url.includes('/games')) data=[{game_id:'game1',away_team_abbr:'BOS',home_team_abbr:'LAL'}];
    else data=[{player_name:'Player A',team_abbr:'LAL',metrics:[{metric:'pts',aggregation:'total',value:10}]}];
    return {ok:true,json:async()=>({publication_id:token,scope:{season:'2026-27',phase:'Regular Season',first:'2026-10-20',last:'2026-10-30'},data})};
  };
  await initCompare(card);
  assert.match(status.textContent, /Publication pub_0123456789abcdef/);
  assert.equal(results.children[0].children[1].children[0].children[2].textContent,'3');
  await initPerformance(card);
  date.value='2026-10-20'; await date.listeners.change();
  game.value='game1'; await game.listeners.change();
  assert.equal(results.children[0].children[1].children[0].children[0].textContent,'Player A');
  assert.equal(requests.at(-1).options.headers['X-Publication-ID'],token);
  delete globalThis.document;
});

test('performance reads pin the publication and reject changed or unavailable releases', async () => {
  const token = 'pub_0123456789abcdef';
  const calls = [];
  const fetcher = async (url, options) => {
    calls.push({url, options});
    return {ok:true, json:async () => ({publication_id:token, data:[]})};
  };
  await publicationRead('/api/performance/games?game_date=2026-10-20', token, fetcher);
  assert.equal(calls[0].options.headers['X-Publication-ID'], token);
  await assert.rejects(publicationRead('/api/performance/dates', token, async () => ({ok:true, json:async () => ({publication_id:'pub_ffffffffffffffff'})})), /changed/);
  await assert.rejects(publicationRead('/api/performance/dates', token, async () => ({ok:false})), /unavailable/);
});
