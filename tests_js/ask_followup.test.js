import test from "node:test";
import assert from "node:assert/strict";
import {followupFitsReport} from "../app/static/ask_followup.js";
const report = {conversation_context: {players: [{player_name: "Avery Example"}]}};
test("report refinements preserve identity while allowing metrics and dates", () => {
  for (const question of ["Why is that below average?", "What about assists?", "Only the playoffs", "Show game-by-game", "How did Avery Example perform week of 2025-04-14?"])
    assert.equal(followupFitsReport(question, report), true, question);
});
test("unrelated names, topics, and ambiguous wording need an explicit handoff", () => {
  for (const question of ["Tell me about LeBron James", "What about Blair Sample?", "What is the weather?", "How about that situation?", ""])
    assert.equal(followupFitsReport(question, report), false, question);
  assert.equal(followupFitsReport("What about assists?", null), false);
});
test("clarification replies can use identities in the original scoped question", () => {
  const pending = {status: "clarification_required"};
  assert.equal(followupFitsReport("Avery Example", pending, "Compare Avery Example with Blair Sample"), true);
  assert.equal(followupFitsReport("LeBron James", pending, "Compare Avery Example with Blair Sample"), false);
});

test("backend research suggestions remain admissible without a copied fixture list", async () => {
  const {readFileSync} = await import('node:fs');
  const source = readFileSync(new URL('../app/research_studies.py', import.meta.url), 'utf8');
  const suggestions = JSON.parse(source.match(/"followups": (\[[^\]]*\])/)[1]);
  assert.ok(suggestions.length > 0);
  for (const question of suggestions)
    assert.equal(followupFitsReport(question, {...report, followups: suggestions}), true, question);
});

test("exact report suggestions bypass vocabulary but edits do not inherit that exemption", () => {
  const suggestion = "Describe possession-adjusted synergy";
  assert.equal(followupFitsReport(suggestion, {...report, followups: [suggestion]}), true);
  assert.equal(followupFitsReport(suggestion, report, '', [suggestion]), true);
  assert.equal(followupFitsReport(suggestion + ' with Blair Sample', report, '', [suggestion]), false);
  for (const question of ['What about rebounding?', 'How was his passing?', 'What was the defensive rating?',
    'What about plus-minus?', 'How consistent was that pattern?', 'In the clutch', 'Off the bench',
    'As a starter', 'Change the threshold for limited minutes'])
    assert.equal(followupFitsReport(question, report), true, question);
});

test("only usable reports and pending clarifications enable follow-ups", async () => {
  const {isFollowupReport} = await import('../app/static/ask_followup.js');
  for (const payload of [null, {}, {status:'unsupported'}, {status:'error'}, {status:'insufficient_data'}])
    assert.equal(isFollowupReport(payload), false);
  for (const payload of [{status:'ok'}, {status:'clarification_required'},
    {availability_scope: {}, availability_evidence: {}}])
    assert.equal(isFollowupReport(payload), true);
});

test("opponent splits reports admit canonical metric labels and aliases", () => {
  const oppReport = {
    status: "ok",
    ask_route: { route: "opponent_splits" },
    conversation_context: {
      analysis_type: "opponent_splits",
      players: [{ player_id: 101, player_name: "Avery Example" }],
    },
    opponent_splits: {
      version: "opponent_splits/1",
      player: { player_id: 101, player_name: "Avery Example" },
      metric_definitions: {
        pts: { key: "pts", label: "PTS" },
      },
    },
  };
  for (const question of [
    "What about 3P%?",
    "What about FG%?",
    "What about FT%?",
    "What about TS%?",
    "What about eFG%?",
    "What about 3PA?",
    "What about 3PA and PTS?",
    "What about MPG?",
    "What about PTS per 36 minutes?",
    "What about OREB, DREB, and PF?",
  ]) {
    assert.equal(
      followupFitsReport(question, oppReport, "Which team did Avery Example struggle against most?"),
      true,
      question,
    );
  }
  assert.equal(followupFitsReport("What about Blair Sample?", oppReport), false);
});
