import test from "node:test";
import assert from "node:assert/strict";
import {gamePerformance, hasMeaningfulTrend, renderGamePerformance} from "../app/static/ask_game_report.js";
import {renderAskSources} from "../app/static/ask_sources.js";

const pin = "pub_0123456789abcdef";
const report = {
  kind: "game_performance",
  game: {
    game_id: "0022600255", game_date: "2026-11-17", phase: "Regular Season",
    team_abbr: "SAS", team_name: "San Antonio Spurs", opponent_abbr: "DET", opponent_name: "Detroit Pistons",
    home_team_abbr: "DET", home_score: 104, away_team_abbr: "SAS", away_score: 108,
  },
  player: {player_id: 1641705, player_name: "Victor Wembanyama"},
  metrics: [
    {key:"reb", value:18, baseline:13.8, change:4.2},
    {key:"oreb", value:5, baseline:2, change:3}, {key:"dreb", value:13}, {key:"min", value:28.8},
    {key:"pts", value:25}, {key:"ast", value:4},
    {key:"fg_pct", value:58.8, baseline:49.5, change:9.3, unit:"%", change_unit:"percentage points"},
  ],
  baseline: {first:"2026-10-22", last:"2026-11-15", games:14},
  contribution: [{key:"pts", value:25, team_total:108, share:25/108}],
  team_rebounding: {
    status:"ok", formula:"OREB / (OREB + opponent DREB)",
    team:{team_abbr:"SAS", oreb:16, opponent_dreb:28, rate:16/44},
    opponent:{team_abbr:"DET", oreb:9, opponent_dreb:33, rate:9/42},
  },
  rosters: {status:"ok",
    team:[
      {player_id:1641705, player_name:"Victor Wembanyama", pts:25, ast:4, reb:18, oreb:5, dreb:13},
      {player_id:2, player_name:"Example Guard", pts:30, ast:2, reb:3, oreb:0, dreb:3},
    ],
    opponent:[{player_id:1, player_name:"Example <Forward>", reb:11, oreb:2, dreb:9}],
  },
};

test("a game report chooses the verified lead exhibit and keeps both rosters expandable", () => {
  assert.equal(gamePerformance({semantic_evidence:report}), report);
  const rebounds = renderGamePerformance({...report, lead_metric:"reb"});
  const main = rebounds.split("<details")[0];
  assert.match(main, /San Antonio Spurs vs Detroit Pistons/);
  assert.match(main, /SAS 108 · DET 104/);
  assert.match(main, /Offensive rebound rate.*36\.4%.*21\.4%/s);
  assert.doesNotMatch(main, /Team scoring/);
  assert.equal((main.match(/scope="row"/g) || []).length, 4);
  assert.match(rebounds, /Who got the rebounds\?.*>REB<.*>OREB<.*>DREB<.*Victor Wembanyama.*Example Guard.*Example &lt;Forward&gt;/s);
  assert.match(rebounds, /More player stats.*58\.8%.*49\.5%.*\+9\.3 pp/s);
  assert.doesNotMatch(rebounds, /<Forward>|<details[^>]* open/);

  const scoring = renderGamePerformance({...report, lead_metric:"pts"});
  const featured = scoring.split("<details")[0];
  assert.match(featured, /Team scoring.*108 points.*104 points.*25 of 108 SAS points \(23\.1%\)/s);
  assert.doesNotMatch(featured, /Offensive rebound rate/);
  assert.match(scoring, /More team comparisons.*Offensive rebound rate/s);
  assert.match(scoring, /Who scored the points\?.*>PTS<.*>REB<.*>AST<.*Example Guard.*Victor Wembanyama/s);
  const assists = renderGamePerformance({...report, lead_metric:"ast"});
  assert.match(assists, /Who recorded the assists\?.*>AST<.*>PTS<.*>TOV<.*Victor Wembanyama.*Example Guard/s);
  const steals = renderGamePerformance({...report, lead_metric:"stl"});
  assert.match(steals, /Who recorded the steals\?.*>STL<.*>BLK<.*>MIN</s);
  const blocks = renderGamePerformance({...report, lead_metric:"blk"});
  assert.match(blocks, /Who recorded the blocks\?.*>BLK<.*>STL<.*>MIN</s);
});

test("an unreconciled focal rebound split stays out of the player box and team share", () => {
  const mismatched = {
    ...report,
    lead_metric: "reb",
    rebounding_context: {split: null},
    metrics: report.metrics.map(metric => metric.key === "dreb" ? {...metric, value: 12, baseline: 5, change: 7} : metric),
    contribution: [...report.contribution, {key:"dreb", value:12, team_total:24, share:.5}],
    rosters: {
      ...report.rosters,
      team: report.rosters.team.map(player => player.player_id === report.player.player_id ? {...player, dreb: 12} : player),
    },
  };
  const html = renderGamePerformance(mismatched);
  assert.match(html, /<th scope="row">REB<\/th><td>18<\/td>/);
  assert.doesNotMatch(html, /Offensive rebound rate|20\.0%/);
  assert.doesNotMatch(html, /scope="row">(?:OREB|DREB)<|\+7<\/td>/);
  assert.match(html, /Victor Wembanyama<\/th><td>18<\/td><td>—<\/td><td>—<\/td>/);
  assert.match(html, /Example Guard<\/th><td>3<\/td><td>0<\/td><td>3<\/td>/);
  assert.match(renderGamePerformance({...report, lead_metric: "reb"}), /<th scope="row">DREB<\/th><td>13<\/td>/);
});

test("saved game reports show rebound component averages only with verified baseline membership", () => {
  const rebounding = {...report, lead_metric: "reb"};
  assert.match(renderGamePerformance(rebounding), /OREB<\/th><td>5<\/td><td>—<\/td><td>—<\/td>/);
  assert.match(renderGamePerformance({...rebounding, rebound_baseline_verified: true}),
    /OREB<\/th><td>5<\/td><td>2<\/td><td>\+3<\/td>/);
});

test("trends need two dated observations while discrete charts retain single marks", () => {
  const line = points => ({type:"line", series:[{points}]});
  assert.equal(hasMeaningfulTrend(line([{x:"2026-11-17",y:0}])), false);
  assert.equal(hasMeaningfulTrend(line([{x:"2026-11-17",y:0},{x:"2026-11-18",y:null}])), false);
  assert.equal(hasMeaningfulTrend(line([{x:"2026-11-17",y:0},{x:"2026-11-18",y:12}])), true);
  assert.equal(hasMeaningfulTrend({type:"bar",series:[{points:[{x:"Team",y:0}]}]}), true);
});

test("Sources retain official game attribution, independently pinned reads and all public availability URLs", () => {
  const html = renderAskSources({publication_id:pin, source_coverage:{last:"2026-11-17"}, semantic_evidence:report});
  assert.match(html, /Accepted game results.*Game 0022600255/s);
  assert.match(html, /performance\/games\?publication_id=pub_0123456789abcdef&amp;game_date=2026-11-17/);
  assert.match(html, /performance\/players\?publication_id=pub_0123456789abcdef&amp;game_id=0022600255/);
  assert.match(html, /players\/1641705\/game-log\?publication_id=pub_0123456789abcdef/);
  const incomplete = {...report, game:{...report.game, home_score:null}};
  assert.doesNotMatch(renderAskSources({publication_id:pin, semantic_evidence:incomplete}), /Accepted game results/);

  const availability = renderAskSources({availability_evidence:{groups:{played:[{
    game_date:"2025-01-01", source_urls:["https://www.nba.com/report-a", "https://www.nba.com/report-b"],
  }]}, excluded_games:[{source_urls:["https://www.nba.com/report-b","javascript:alert(1)"]}]}});
  assert.equal((availability.match(/href=/g) || []).length, 2);
  assert.match(availability, /report-a.*report-b/s);
  assert.doesNotMatch(availability, /javascript:/);
});
