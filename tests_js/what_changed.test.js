import test from "node:test";
import assert from "node:assert/strict";
import { selectMovers, renderMover, number } from "../app/static/what_changed.js";

test("top and surge lists exclude ineligible players and sort independently", () => {
  const items = [
    {player_id:1, player_name:"Star", team_abbr:"AAA", top_score:40, surge_score:null},
    {player_id:2, player_name:"Rising", team_abbr:"BBB", top_score:30, surge_score:10},
    {player_id:3, player_name:"Small sample", team_abbr:"BBB", top_score:null, surge_score:null},
  ];
  assert.deepEqual(selectMovers(items,"top").map(item=>item.player_id),[1,2]);
  assert.deepEqual(selectMovers(items,"surging").map(item=>item.player_id),[2]);
  assert.deepEqual(selectMovers(items,"top"," bbb ").map(item=>item.player_id),[2]);
  assert.equal(items[0].player_id,1);
});

test("unknown metrics remain unknown and positive deltas retain sign", () => {
  assert.equal(number(null),"—");
  assert.equal(number(undefined),"—");
  assert.equal(number(0),"0.0");
  assert.equal(number(2,true),"+2.0");
});

test("rendered evidence escapes source content and rejects unsafe report links", () => {
  const window = {games_played:3,team_games:4,no_recorded_appearance:1, games:[],
    start_date:"2026-02-09",end_date:"2026-02-15",
    injuries:{available:true,games_covered:1,out_reports:1,reports:[{game_date:"2026-02-15",report_date:"2026-02-15",injury_status:"Out",reason:"<script>bad</script>",source_url:"javascript:alert(1)"}]}};
  const html = renderMover({player_id:1,player_name:"<script>name</script>",team_abbr:"AAA",top_rank:1,top_score:30,current:window,previous:window,sample_note:"Small sample",
    clusters:[{key:"availability",label:"Playing availability",metrics:[{key:"pf",label:"Fouls",current:3,previous:2,delta:1,lower_is_better:true,unit:"count"}]}]},"top");
  assert.equal(html.includes("<script>"),false);
  assert.equal(html.includes("javascript:"),false);
  assert.match(html,/DNP-CD: not available/);
  assert.match(html,/Reasons are unconfirmed/);
  assert.match(html,/delta-negative/);
});

// Baselines and axis placement matter when top performers include new players.
const { opportunityPoints, chartAxis, renderOpportunityMap, initWhatChanged } = await import('../app/static/what_changed.js');
test('map excludes unknown, short, and traded baselines without dropping zero changes', () => {
  const item = {player_id:1,player_name:'Zero',team_abbr:'AAA',production_delta:0,current:{values:{min:30}},previous:{values:{min:30},games_played:3,team_games:4}};
  const items=[item,{...item,player_id:2,production_delta:null},{...item,player_id:3,team_changed:true},{...item,player_id:4,previous:{...item.previous,games_played:2}}];
  assert.deepEqual(opportunityPoints(items).map(p=>[p.item.player_id,p.x,p.y]),[[1,0,0]]);
  assert.equal(opportunityPoints(items,2,false).length,2);
});
test('map axes include zero, negative values, outliers, and nonzero padding', () => {
  for (const values of [[0,0],[-7,2],[50,300],[-300,-50],[]]) {
    const axis=chartAxis(values);
    assert.ok(axis.min<Math.min(0,...values));
    assert.ok(axis.max>Math.max(0,...values));
    assert.ok(axis.ticks.length<15);
  }
});
test('overlapping map points retain separate keyboard targets and escaped labels', () => {
  const points=[1,2].map(id=>({item:{player_id:id,player_name:'<img onerror=x>',team_abbr:'AAA'},x:0,y:0}));
  const html=renderOpportunityMap(points,1);
  assert.equal((html.match(/tabindex="0"/g)||[]).length,2);
  assert.ok(html.indexOf('data-select-player="2"')<html.indexOf('data-select-player="1"'));
  assert.equal(html.includes('<img'),false);
  assert.match(renderOpportunityMap([],null),/ranked list/);
});
test('changing Compare or Season phase auto-triggers Trending data reload', async () => {
  const makeNode = (value = "") => ({
    value,
    innerHTML: "",
    textContent: "",
    hidden: false,
    listeners: {},
    addEventListener(name, fn) { this.listeners[name] = fn; },
    setAttribute() {},
    querySelectorAll() { return []; },
  });
  const nodes = {
    "#changed-controls": makeNode(),
    "#changed-period": makeNode("four_games"),
    "#changed-phase": makeNode("Regular Season"),
    "#changed-list": makeNode(),
    "#changed-status": makeNode(),
    "#changed-more": makeNode(),
    "#changed-search": makeNode(),
    "#changed-chart": makeNode(),
    "#changed-detail": makeNode(),
    "#changed-chart-note": makeNode(),
    "#changed-dates": makeNode(),
    "#changed-ranking": makeNode(),
  };
  const doc = {
    querySelector: (sel) => nodes[sel],
    querySelectorAll: () => [],
  };
  const origFormData = globalThis.FormData;
  globalThis.FormData = class {
    *[Symbol.iterator]() {
      yield ["period", nodes["#changed-period"].value];
      yield ["season_type", nodes["#changed-phase"].value];
    }
  };
  const urls = [];
  try {
    initWhatChanged(doc, async (url) => {
      urls.push(url);
      return {
        ok: true,
        json: async () => ({
          state: "empty",
          items: [],
          ranking: { top: "Top", surging: "Surging", formula: "Index", minimum_games: 3 },
        }),
      };
    });
    assert.equal(urls.length, 1);
    nodes["#changed-period"].value = "week";
    await nodes["#changed-period"].listeners.change();
    assert.equal(urls.length, 2);
    assert.match(urls[1], /period=week/);
    nodes["#changed-phase"].value = "Playoffs";
    await nodes["#changed-phase"].listeners.change();
    assert.equal(urls.length, 3);
    assert.match(urls[2], /season_type=Playoffs/);
  } finally {
    globalThis.FormData = origFormData;
  }
});
