import {dateLabel, metricAbbreviation} from "./ask_presentation.js";

const escape = value => String(value ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
const rowsOf = value => Array.isArray(value) ? value : [];
const finite = Number.isFinite;
const number = (value, fraction = false) => finite(value)
  ? value.toLocaleString("en-US", {maximumFractionDigits: fraction ? 1 : 0}) : "—";
const isShare = value => finite(value) && value >= 0 && value <= 1;
const percent = value => isShare(value) ? `${(value * 100).toFixed(1)}%` : "—";
const teamLabel = (name, abbreviation) => name || abbreviation || "";

function shareMark(value) {
  return isShare(value) ? `<div class="ask-game-rate"><meter min="0" max="1" value="${value}" aria-hidden="true"></meter><span>${percent(value)}</span></div>` : "—";
}

function boxScore(report, metrics) {
  if (!metrics.length) return "";
  const hasBaseline = metrics.some(metric => finite(metric.baseline));
  const baseline = report.baseline;
  const baselineDates = baseline?.first && baseline?.last
    ? baseline.first === baseline.last ? dateLabel(baseline.first)
      : `${dateLabel(baseline.first)} – ${dateLabel(baseline.last)}`
    : "";
  const metricValue = (value, metric) => finite(value)
    ? `${number(value, true)}${metric.unit === "%" ? "%" : ""}` : "—";
  function metricRow(metric) {
    const delta = finite(metric.change)
      ? `${metric.change > 0 ? "+" : ""}${number(metric.change, true)}${metric.change_unit === "percentage points" ? " pp" : ""}`
      : "—";
    return `<tr><th scope="row">${escape(metricAbbreviation(metric))}</th><td>${metricValue(metric.value, metric)}</td>${hasBaseline
      ? `<td>${metricValue(metric.baseline, metric)}</td><td>${delta}</td>` : ""}</tr>`;
  }
  return `<section class="ask-game-panel">
    <h3>Player box score</h3>
    ${hasBaseline && baselineDates ? `<p class="meta">Compared with earlier games · ${escape(baselineDates)}${Number.isSafeInteger(baseline.games) ? ` · ${baseline.games} games` : ""}</p>` : ""}
    <table class="ask-game-table"><thead><tr><th scope="col">Stat</th><th scope="col">Game</th>${hasBaseline
      ? '<th scope="col">Earlier</th><th scope="col">Change</th>' : ""}</tr></thead><tbody>${metrics.map(metricRow).join("")}</tbody></table>
  </section>`;
}

function contribution(report) {
  const records = rowsOf(report.contribution);
  if (!records.some(row => isShare(row.share))) return "";
  return `<section class="ask-game-panel ask-game-contribution">
    <h3>Share of ${escape(report.game.team_abbr)} production</h3>
    <table class="ask-game-table"><thead><tr><th scope="col">Stat</th><th scope="col">Player</th><th scope="col">Team</th><th scope="col">Share</th></tr></thead>
    <tbody>${records.map(row => `<tr><th scope="row">${escape(metricAbbreviation(row))}</th><td>${number(row.value)}</td><td>${number(row.team_total)}</td><td>${shareMark(row.share)}</td></tr>`).join("")}</tbody></table>
  </section>`;
}

function rebounding(report) {
  const board = report.team_rebounding;
  if (board?.status !== "ok" || !board.team || !board.opponent) return "";
  const teams = [board.team, board.opponent];
  if (!teams.some(team => isShare(team.rate))) return "";
  return `<section class="ask-game-panel ask-game-team-boards">
    <h3>Offensive rebound rate</h3>
    <ul class="ask-game-team-rates">${teams.map(team => `<li><strong>${escape(team.team_abbr)}</strong>${shareMark(team.rate)}<span class="meta">${number(team.oreb)} offensive · ${number(team.opponent_dreb)} opponent defensive</span></li>`).join("")}</ul>
    ${typeof board.formula === "string" ? `<p class="meta">${escape(board.formula)}</p>` : ""}
  </section>`;
}

function scoring(report) {
  const {game, player} = report;
  const scores = new Map([[game.home_team_abbr, game.home_score], [game.away_team_abbr, game.away_score]]);
  const teamScore = scores.get(game.team_abbr);
  const opponentScore = scores.get(game.opponent_abbr);
  if (!finite(teamScore) || !finite(opponentScore) || Math.max(teamScore, opponentScore) <= 0) return "";
  const scale = Math.max(teamScore, opponentScore);
  const points = rowsOf(report.contribution).find(row => row.key === "pts" && row.team_total === teamScore);
  const scorer = isShare(points?.share) ? points : null;
  const bar = (team, value, focal) => `<li>
    <div><strong>${escape(team)}</strong> <span>${number(value)} points</span></div>
    <div class="ask-game-score-track" aria-hidden="true">
      <div class="ask-game-score-bar" style="--score-width:${value / scale * 100}%">
        ${focal ? `<span style="--focal-share:${scorer.share * 100}%"></span>` : ""}
      </div>
    </div>
  </li>`;
  const foot = scorer && player?.player_name
    ? `<p class="meta">${escape(player.player_name)} scored ${number(scorer.value)} of ${number(teamScore)} ${escape(game.team_abbr)} points (${percent(scorer.share)}).</p>` : "";
  return `<section class="ask-game-panel ask-game-scoring"><h3>Team scoring</h3>
    <ul class="ask-game-score-bars">${bar(game.team_abbr, teamScore, Boolean(scorer))}${bar(game.opponent_abbr, opponentScore, false)}</ul>
    ${foot}</section>`;
}

function rosterBoard(report) {
  const rosters = report.rosters;
  if (rosters?.status !== "ok" || !rowsOf(rosters.team).length || !rowsOf(rosters.opponent).length) return "";
  const counts = player => finite(player.reb) ? player.reb : -1;
  const table = (players, label) => `<table class="ask-game-table"><caption>${escape(label)}</caption>
    <thead><tr><th scope="col">Player</th><th scope="col">REB</th><th scope="col">OREB</th><th scope="col">DREB</th></tr></thead>
    <tbody>${[...players].sort((a, b) => counts(b) - counts(a)).map(player => `<tr${player.player_id === report.player?.player_id ? ' class="is-focal"' : ""}><th scope="row">${escape(player.player_name)}</th><td>${number(player.reb)}</td><td>${number(player.oreb)}</td><td>${number(player.dreb)}</td></tr>`).join("")}</tbody></table>`;
  const {game} = report;
  return `<details class="ask-game-board"><summary>Who got the rebounds?</summary>
    <div class="ask-game-grid">${table(rosters.team, teamLabel(game.team_name, game.team_abbr))}${table(rosters.opponent, teamLabel(game.opponent_name, game.opponent_abbr))}</div>
  </details>`;
}

export function gamePerformance(payload) {
  const report = payload?.semantic_evidence;
  return report?.kind === "game_performance" && report.game?.team_abbr && report.game?.opponent_abbr ? report : null;
}

export function renderGamePerformance(report) {
  const {game} = report;
  const sides = [game.away_team_abbr, game.home_team_abbr];
  const scores = [game.away_score, game.home_score];
  const final = sides.every(Boolean) && scores.every(finite)
    ? `${sides[0]} ${scores[0]} · ${sides[1]} ${scores[1]}` : "";
  const matchup = [teamLabel(game.team_name, game.team_abbr), teamLabel(game.opponent_name, game.opponent_abbr)].join(" vs ");
  const exhibits = {reb: rebounding(report), pts: scoring(report), contribution: contribution(report)};
  const lead = report.lead_metric || report.focus_metric;
  const preferred = lead === "reb" || lead === "pts" ? lead : "contribution";
  const primary = [preferred, "contribution", "pts", "reb"].find(key => exhibits[key]);
  const other = Object.entries(exhibits).filter(([key, html]) => key !== primary && html).map(([, html]) => html);
  const metrics = rowsOf(report.metrics).filter(metric => finite(metric.value));
  const keys = lead === "reb" ? ["reb", "oreb", "dreb", "min"]
    : [lead, "pts", "reb", "ast", "fg_pct"];
  const selected = [...new Set(keys)].map(key => metrics.find(metric => metric.key === key)).filter(Boolean).slice(0, 4);
  const featured = selected.length ? selected : metrics.slice(0, 4);
  const extra = metrics.filter(metric => !featured.includes(metric));
  return `<div class="ask-game-report">
    <div class="ask-game-matchup"><h2>${escape(matchup)}</h2>
      <p class="meta">${[game.game_date && dateLabel(game.game_date), game.phase, final].filter(Boolean).map(escape).join(" · ")}</p>
    </div>
    <div class="ask-game-grid">${primary ? exhibits[primary] : ""}${boxScore(report, featured)}</div>
    ${rosterBoard(report)}
    ${extra.length ? `<details class="ask-game-board"><summary>More player stats</summary>${boxScore(report, extra)}</details>` : ""}
    ${other.length ? `<details class="ask-game-board"><summary>More team comparisons</summary><div class="ask-game-grid">${other.join("")}</div></details>` : ""}
    ${rowsOf(report.warnings).length ? `<ul class="meta ask-game-notes">${report.warnings.map(warning => `<li>${escape(warning)}</li>`).join("")}</ul>` : ""}
  </div>`;
}

export function hasMeaningfulTrend(chart) {
  return chart.type !== "line" || rowsOf(chart.series).some(series =>
    new Set(rowsOf(series.points).filter(point => finite(point.y) && point.x != null).map(point => point.x)).size >= 2);
}
