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
