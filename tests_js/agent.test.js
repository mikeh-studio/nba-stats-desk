import test from "node:test";
import assert from "node:assert/strict";

class FakeElement {
  constructor(tagName = "div") {
    this.tagName = tagName;
    this.children = [];
    this.dataset = {};
    this.hidden = false;
    this.listeners = new Map();
    this.textContent = "";
    this.className = "";
    this.tabIndex = 0;
    this.focused = false;
    this._innerHTML = "";
  }

  get innerHTML() {
    return this._innerHTML;
  }

  set innerHTML(value) {
    this._innerHTML = String(value || "");
    if (this._innerHTML === "") {
      this.children = [];
    }
    if (this._innerHTML.includes("agent-turn-answer")) {
      const answer = new FakeElement("div");
      answer.className = "agent-turn-answer";
      this.children = [answer];
    }
  }

  appendChild(child) {
    this.children.push(child);
    return child;
  }

  querySelector(selector) {
    if (selector === ".agent-turn-answer") {
      return (
        this.children.find(
          (child) => child.className === "agent-turn-answer",
        ) || null
      );
    }
    return null;
  }

  querySelectorAll(selector) {
    if (selector !== "[data-history-conversation-id]") return [];
    return [
      ...this._innerHTML.matchAll(/data-history-conversation-id="([^"]+)"/g),
    ].map((match) => {
      const button = new FakeElement("button");
      button.dataset.historyConversationId = match[1];
      return button;
    });
  }

  addEventListener(eventName, callback) {
    const listeners = this.listeners.get(eventName) || [];
    listeners.push(callback);
    this.listeners.set(eventName, listeners);
  }

  dispatch(eventName, event = {}) {
    (this.listeners.get(eventName) || []).forEach((callback) =>
      callback(event),
    );
  }

  focus() {
    this.focused = true;
  }

  scrollIntoView() {}
}

function createStorage({ failWrites = false } = {}) {
  const store = new Map();
  return {
    getItem(key) {
      return store.has(key) ? store.get(key) : null;
    },
    setItem(key, value) {
      if (failWrites) throw new Error("quota");
      store.set(key, value);
    },
    removeItem(key) {
      store.delete(key);
    },
    read(key) {
      return store.get(key) || null;
    },
  };
}

function createDocument(elements = {}) {
  return {
    querySelector(selector) {
      return elements[selector] || null;
    },
    querySelectorAll(selector) {
      return selector.split(',').map(s => elements[s.trim()]).filter(Boolean);
    },
    createElement(tagName) {
      return new FakeElement(tagName);
    },
    addEventListener() {},
  };
}

async function loadAgentModule({
  storage = createStorage(),
  elements = {},
} = {}) {
  globalThis.window = { localStorage: storage, __NBA_ASK_TEST_HOOKS__: true };
  globalThis.document = createDocument(elements);
  globalThis.HTMLButtonElement = FakeElement;
  globalThis.HTMLFormElement = FakeElement;
  globalThis.HTMLSelectElement = FakeElement;
  globalThis.HTMLTextAreaElement = FakeElement;
  globalThis.fetch = async () => ({
    ok: true,
    json: async () => ({ conversations: [] }),
  });
  await import(`../app/static/agent.js?test=${Date.now()}-${Math.random()}`);
  return globalThis.__askAgentTest;
}

test("reference row supports text-only emphasis without changing the league rank", async () => {
  const agent = await loadAgentModule();
  const html = agent.renderTable({
    columns: [{ key: "player" }, { key: "rank" }],
    rows: [
      ["Leader", "1"],
      ["Jalen Johnson", "8"],
    ],
    reference_row_index: 1,
  });
  assert.equal((html.match(/class="reference-player-row"/g) || []).length, 1);
  assert.match(
    html,
    /Jalen Johnson<\/td><td>8<\/td>/,
  );
  assert.doesNotMatch(html, /reference-player-label/);
});

test("saved sample counts get clear labels without changing evidence or unrelated fractions", async () => {
  const agent = await loadAgentModule();
  const table = {
    columns: [{ label: "Makes/attempts" }, { label: "Both valid/observed" }, { label: "Out valid/observed" }],
    rows: [["1/2", "0/2", "1/2"]],
  };
  const before = JSON.stringify(table);
  const html = agent.renderTable(table);
  assert.match(html, /Games with data — both played/);
  assert.match(html, /Games with data — teammate reported Out/);
  assert.match(html, /<td>1\/2<\/td><td>0 of 2<\/td><td>1 of 2<\/td>/);
  assert.match(html, /Counts can differ by stat/);
  assert.equal(JSON.stringify(table), before);
  const withDescription = agent.renderTable({ ...table, description: "<script>alert(1)</script>" });
  assert.match(withDescription, /&lt;script&gt;/);
  assert.doesNotMatch(withDescription, /<script>/);
});

test("study cards retain their scoped identity and clear on a refusal", async () => {
  const elements = Object.fromEntries([
    "[data-agent-table-card]", "[data-agent-tables]",
    "[data-agent-chart-card]", "[data-agent-charts]", "[data-agent-profile]",
  ].map((selector) => [selector, new FakeElement("div")]));
  const agent = await loadAgentModule({ elements });
  agent.renderAuxiliaryPayload({
    player_profile: {
      player: {
        player_name: "Focal Player",
        headshot_url: "https://cdn.nba.com/headshots/nba/latest/1040x760/1.png",
      },
      profile_url: "/players/1",
      scopeLabel: "2024-25 regular season · 2024-11-01 through 2024-11-30",
    },
  }, globalThis.document, false);
  const profile = elements["[data-agent-profile]"];
  assert.match(profile.innerHTML, /Focal Player/);
  assert.match(profile.innerHTML, /2024-25 regular season/);
  assert.match(profile.innerHTML, /2024-11-01 through 2024-11-30/);
  assert.match(profile.innerHTML, /headshots\/nba\/latest\/1040x760\/1.png/);
  assert.match(profile.innerHTML, /href="\/players\/1"/);
  assert.doesNotMatch(profile.innerHTML, /Rank #|P-Rating/);
  agent.renderAuxiliaryPayload({ answer: "Scope unavailable" }, globalThis.document, false);
  assert.equal(profile.innerHTML, "");
});

test("renderAnswerMarkdown repairs inline headings and keeps Markdown structure", async () => {
  const agent = await loadAgentModule();

  const html = agent.renderAnswerMarkdown(
    "Intro ## Title ### Context - **PTS:** 30\n\n---\n1. `AST`: 7",
  );

  assert.match(html, /<h3>Title<\/h3>/);
  assert.match(html, /<h4>Context<\/h4>/);
  assert.match(html, /<ul><li><strong>PTS:<\/strong> 30<\/li><\/ul>/);
  assert.match(html, /<hr \/>/);
  assert.match(html, /<ol><li><code>AST<\/code>: 7<\/li><\/ol>/);
});

test("renderAnswerMarkdown does not split block-level lines on inline markup", async () => {
  const agent = await loadAgentModule();

  // A list item whose text happens to contain "###" / "---" must stay one list
  // item. The per-line repair skips already-block-level lines, where the old
  // whole-text regexes would have split this into a spurious heading/rule.
  const html = agent.renderAnswerMarkdown("- Form: hot ### still --- climbing");

  assert.match(html, /<ul>/);
  assert.doesNotMatch(html, /<h[1-6]/);
  assert.doesNotMatch(html, /<hr \/>/);
});

test("renderAnswerMarkdown strips Markdown pipe tables from Answer prose", async () => {
  const agent = await loadAgentModule();

  const html = agent.renderAnswerMarkdown(
    "Intro\n\n| Metric | Value |\n|---|---|\n| PTS | 30 |\n\n### Takeaway\nGood.",
  );

  assert.match(html, /<p>Intro<\/p>/);
  assert.match(html, /<h4>Takeaway<\/h4>/);
  assert.doesNotMatch(html, /\| Metric \|/);
  assert.doesNotMatch(html, /\| PTS \|/);
});

test("example buttons fill the question without submitting", async () => {
  let submitCount = 0;
  const input = new FakeElement("textarea");
  const form = new FakeElement("form");
  form.requestSubmit = () => {
    submitCount += 1;
  };
  const button = new FakeElement("button");
  button.dataset.agentExample = "Who is similar to Tyrese Maxey?";
  const agent = await loadAgentModule({
    elements: {
      "[data-agent-question]": input,
      "[data-agent-form]": form,
    },
  });

  agent.bindExampleButtons({
    querySelectorAll(selector) {
      return selector === "[data-agent-example]" ? [button] : [];
    },
  });
  button.dispatch("click");

  assert.equal(input.value, "Who is similar to Tyrese Maxey?");
  assert.equal(input.focused, true);
  assert.equal(submitCount, 0);
});

test("browser history saves, dedupes, caps, and renders list rows", async () => {
  const storage = createStorage();
  const list = new FakeElement("div");
  const agent = await loadAgentModule({
    storage,
    elements: { "[data-agent-history-list]": list },
  });

  agent.persistHistoryTurn("Question A", {
    conversation_id: "c-a",
    request_id: "r-a",
    answer: "First answer",
  });
  agent.persistHistoryTurn("Question A updated", {
    conversation_id: "c-a",
    request_id: "r-a",
    answer: "Updated answer",
  });
  let saved = JSON.parse(storage.read("askChatHistory:v1"));
  let updated = saved.conversations.find(
    (item) => item.conversation_id === "c-a",
  );
  assert.equal(updated.turns.length, 1);
  assert.equal(updated.turns[0].payload.answer, "Updated answer");

  for (let index = 0; index < 30; index += 1) {
    agent.persistHistoryTurn(`Question ${index}`, {
      conversation_id: `c-${index}`,
      request_id: `r-${index}`,
      answer: `Answer ${index}`,
    });
  }

  saved = JSON.parse(storage.read("askChatHistory:v1"));
  assert.equal(saved.conversations.length, 25);
  updated = saved.conversations.find((item) => item.conversation_id === "c-a");
  assert.equal(updated, undefined);
  assert.match(list.innerHTML, /agent-history-item/);
});

test("browser history write failures do not break Ask", async () => {
  const storage = createStorage({ failWrites: true });
  const agent = await loadAgentModule({ storage });

  assert.doesNotThrow(() => {
    agent.persistHistoryTurn("Question", {
      conversation_id: "c-fail",
      request_id: "r-fail",
      answer: "Still renderable",
    });
  });
});

test("restoring a conversation paints the latest saved turn without rerunning it", async () => {
  const elements = {
    "[data-agent-history-list]": new FakeElement("div"),
    "[data-agent-empty]": new FakeElement("div"),
    "[data-agent-answer]": new FakeElement("div"),
    "[data-agent-status]": new FakeElement("span"),
    "[data-agent-table-card]": new FakeElement("section"),
    "[data-agent-tables]": new FakeElement("div"),
    "[data-agent-chart-card]": new FakeElement("section"),
    "[data-agent-charts]": new FakeElement("div"),
    "[data-agent-context]": new FakeElement("div"),
  };
  const agent = await loadAgentModule({ elements });

  agent.saveHistoryState({
    conversations: [
      {
        id: "c-restore",
        title: "Restore me",
        updated_at: "2026-06-19T00:01:00Z",
        turns: [
          {
            request_id: "r-old",
            question: "Old question",
            timestamp: "2026-06-19T00:00:00Z",
            payload: {
              status: "ok",
              answer: "Old answer",
              player_profile: {
                player: { player_id: 1, player_name: "Jalen Johnson" },
              },
              semantic_evidence: {
                metrics: [{ key: "ast" }],
                scope: {
                  start: "2025-09-13",
                  end: "2026-09-12",
                  phases: ["Regular Season"],
                },
              },
              tables: [{ title: "Old Table", columns: [], rows: [] }],
            },
          },
          {
            request_id: "r-new",
            question: "New question",
            timestamp: "2026-06-19T00:01:00Z",
            payload: {
              status: "clarification_required",
              answer: "New answer",
              tables: [{ title: "New Table", columns: [], rows: [] }],
            },
          },
        ],
      },
    ],
  });

  await agent.restoreConversation("c-restore");

  assert.equal(elements["[data-agent-empty]"].hidden, true);
  assert.equal(elements["[data-agent-answer]"].hidden, false);
  assert.equal(elements["[data-agent-answer]"].children.length, 2);
  assert.equal(elements["[data-agent-status]"].textContent, "Restored");
  assert.match(elements["[data-agent-tables]"].innerHTML, /New Table/);

  elements["[data-agent-answer]"].children[0].dispatch("click");
  assert.match(elements["[data-agent-tables]"].innerHTML, /New Table/);
  assert.equal(agent.getNavigation().activeConversationId, "c-restore");
  const body = agent.buildAskBody(
    "Beside Johnson, who are the other the other top playmaking leads",
  );
  assert.equal(body.previous_context.question, "Old question");
  assert.equal(body.previous_context.players[0].player_name, "Jalen Johnson");
  assert.equal(body.previous_context.scope.start, "2025-09-13");
  assert.equal(body.previous_context.metrics[0], "ast");
  assert.equal(body.previous_context.answer, undefined);
});

test("starting a follow-up preserves previously completed response nodes", async () => {
  const thread = new FakeElement();
  const completed = new FakeElement();
  completed.innerHTML = "Completed answer with evidence";
  thread.appendChild(completed);
  const agent = await loadAgentModule({
    elements: {
      "[data-agent-answer]": thread,
      "[data-agent-empty]": new FakeElement(),
    },
  });
  agent.startTurn("Follow-up question");
  assert.equal(thread.children.length, 2);
  assert.equal(thread.children[0], completed);
  assert.equal(completed.innerHTML, "Completed answer with evidence");
});

test("thinking indicators start and reset for Ask and follow-ups", async () => {
  const elements = Object.fromEntries(
    [
      "[data-agent-submit]",
      "[data-followup-submit]",
      "[data-agent-status]",
    ].map((selector) => [selector, new FakeElement()]),
  );
  const agent = await loadAgentModule({ elements });
  agent.setBusy(true);
  for (const element of Object.values(elements)) {
    assert.equal(element.dataset.thinking, "true");
  }
  assert.equal(elements["[data-agent-submit]"].textContent, "Thinking…");
  assert.equal(elements["[data-followup-submit]"].textContent, "Thinking…");
  agent.setBusy(false);
  for (const element of Object.values(elements)) {
    assert.equal(element.dataset.thinking, "false");
  }
  assert.equal(elements["[data-agent-submit]"].textContent, "Ask");
  assert.equal(elements["[data-followup-submit]"].textContent, "Ask follow-up");
});

test("line charts keep negative and positive observations inside the plot", async () => {
  const agent = await loadAgentModule();
  const html = agent.renderLineChart({
    title: "Plus/minus",
    series: [
      {
        label: "Player",
        points: [
          { x: "one", y: -25 },
          { x: "two", y: 0 },
          { x: "three", y: 20 },
        ],
      },
    ],
  });
  const positions = [
    ...html.matchAll(/class="agent-dot" cx="[^"]+" cy="([^"]+)"/g),
  ].map((m) => Number(m[1]));
  assert.equal(positions.length, 3);
  assert.ok(positions.every((y) => y >= 26 && y <= 204));
  assert.ok(positions[0] > positions[1] && positions[1] > positions[2]);
  assert.match(html, /Player: -25/);
});

test("bar charts preserve negative values and expose accessible hover details", async () => {
  const agent = await loadAgentModule();
  const html = agent.renderChart({
    type: "bar", title: "Plus-minus", y_label: "points per game",
    description: "Same season <script>unsafe</script>",
    selection_reason: "Compare two groups",
    series: [{ points: [
      { x: "Both played", y: -4, meta: "49 games" },
      { x: "Teammate out", y: 2, meta: "21 games" },
      { x: "Missing", y: null },
    ] }],
  });
  assert.match(html, /tabindex="0"/);
  assert.match(html, /49 games/);
  assert.match(html, /21 games/);
  assert.match(html, /Both played: -4/);
  assert.doesNotMatch(html, /Missing/);
  assert.doesNotMatch(html, /width="-/);
  assert.match(html, /Same season &lt;script&gt;/);
  assert.match(html, /agent-tooltip/);
});

test("chart labels wrap by rendered width without losing names or status", async () => {
  const agent = await loadAgentModule();
  const measure = (text) => Array.from(text).reduce((width, char) => width + (char === "W" ? 18 : 9), 0);
  for (const label of ["Draymond Green reported Out", "Kentavious Caldwell-Pope reported Out", "WWWWWWWWWWWWWWWWWWWW", "Nikola Jokić reported Out"]) {
    const lines = agent.wrapChartLabel(label, 160, measure);
    assert.ok(lines.length > 1);
    assert.ok(lines.every((line) => measure(line) <= 160));
    assert.equal(lines.join("").replaceAll(" ", ""), label.replaceAll(" ", ""));
  }
  assert.deepEqual(agent.wrapChartLabel("Both played", 160, measure), ["Both played"]);
});

test("wrapped bar labels stay within their own rows and preserve accessible attribution", async () => {
  const agent = await loadAgentModule();
  const name = "Kentavious Caldwell-Pope reported Out";
  const html = agent.renderBarChart({series: [{points: [
    {x: name, y: 39}, {x: "Both played", y: 26.4}, {x: "<unsafe> & status", y: 0},
  ]}]});
  const rows = [...html.matchAll(/<rect x="0" y="([\d.]+)" width="760" height="([\d.]+)" fill="transparent" \/>\s*<text class="agent-point-label"[^>]*>(.*?)<\/text>/gs)];
  assert.equal(rows.length, 3);
  for (const [, start, height, label] of rows) {
    const baselines = [...label.matchAll(/<tspan x="[\d.]+" y="([\d.]+)">/g)].map((m) => Number(m[1]));
    assert.ok(baselines.length > 0);
    assert.ok(baselines.every((y) => y - 18 >= Number(start) && y + 4 <= Number(start) + Number(height)));
  }
  assert.ok(Number(rows[0][2]) > Number(rows[1][2]));
  assert.match(html, new RegExp(`aria-label="${name}: 39`));
  assert.doesNotMatch(html, /<unsafe>/);
});

test("Source & Coverage follows the selected answer and clears on reset", async () => {
  const panel = new FakeElement();
  const context = new FakeElement();
  const label = new FakeElement();
  const agent = await loadAgentModule({ elements: {
    "[data-answer-coverage]": panel,
    "[data-answer-context]": context,
    "[data-coverage-question]": label,
  } });
  agent.updateSourceCoverage({ assumptions: ["First source"], research_scope: { season: "2025-26", phase: "Both" } }, "First question");
  assert.equal(panel.hidden, false);
  assert.match(context.innerHTML, /Regular season and playoffs/);
  assert.equal(label.textContent, "First question");
  agent.updateSourceCoverage({ assumptions: ["Second source"] }, "Older question");
  assert.doesNotMatch(context.innerHTML, /First source/);
  assert.match(context.innerHTML, /Second source/);
  assert.equal(label.textContent, "Older question");
  agent.updateSourceCoverage(null);
  assert.equal(panel.hidden, true);
  assert.equal(context.innerHTML, "");
  assert.doesNotMatch(agent.turnEvidenceMarkup(), /Methodology|data-agent-context/);
});

test("comparison explorer keeps zero, negative, and missing game values distinct", () => {
  const detail = {minutes:{participated:30,reported_out:32},rates:{},games:[
    {game_id:'001',date:'2025-11-01',opponent:'AAA',group:'both',phase:'Regular Season',values:{plus_minus:-4}},
    {game_id:'002',date:'2025-11-02',opponent:'BBB',group:'out',phase:'Regular Season',values:{plus_minus:0}},
    {game_id:'003',date:'2025-11-03',opponent:'CCC',group:'out',values:{plus_minus:null}},
  ]};
  const body=globalThis.__askAgentTest.comparisonExplorerBody({teammate:'Teammate',detail,metrics:[{key:'plus_minus',label:'+/-',unit:'score-margin points per game',both:-4,out:0,difference:4,relative_change:null}]},'plus_minus');
  assert.equal((body.match(/data-explore-point /g)||[]).length,2);
  assert.match(body,/1 games have no value/);
  assert.match(body,/Game 001/);
  assert.match(body,/Game 002/);
  assert.doesNotMatch(body,/Game 003/);
  assert.doesNotMatch(body,/% versus/);
  assert.match(body,/tabindex="0"/);
});

test("Ask sends the OpenRouter provider and its selected model", async () => {
  const provider=new FakeElement('select'); provider.value='openrouter';
  const model=new FakeElement('select'); model.value='qwen/qwen3-235b-a22b-2507';
  const options=new FakeElement('script'); options.textContent=JSON.stringify({openrouter:[{value:model.value,label:'Qwen3 235B Instruct'}]});
  const agent=await loadAgentModule({elements:{'[data-agent-provider]':provider,'[data-agent-model]':model,'[data-agent-model-options]':options}});
  const body=agent.buildAskBody('How did LeBron play?',null);
  assert.equal(body.provider,'openrouter');
  assert.equal(body.model,'qwen/qwen3-235b-a22b-2507');
});

test("each new tab owns a stable ID, draft, and both-player context", async () => {
  const input = new FakeElement("textarea");
  const followup = new FakeElement("textarea");
  const elements = {"[data-agent-question]": input, "[data-followup-question]": followup,
    "[data-agent-empty]": new FakeElement(), "[data-agent-answer]": new FakeElement()};
  const agent = await loadAgentModule({elements});
  agent.startNewChat();
  const a = agent.getNavigation().activeConversationId;
  input.value = "Question A";
  const context = {players:[{player_id:811,player_name:"Avery Finch",role:"focal"},{player_id:822,player_name:"Blake Reed",role:"teammate"}],scope:{season:"2025-26",phases:["Both"]},metrics:["pts"]};
  agent.persistHistoryTurn("Question A", {conversation_id:a,status:"ok",conversation_context:context});
  agent.startNewChat();
  const b = agent.getNavigation().activeConversationId;
  assert.notEqual(a,b);
  assert.equal(agent.buildAskBody("each player").conversation_id,b);
  assert.equal(agent.buildAskBody("each player").previous_context,undefined);
  input.value = "Question B";
  await agent.restoreConversation(a);
  assert.equal(input.value,"Question A");
  const body = agent.buildAskBody("How many games did each player play?");
  assert.equal(body.conversation_id,a);
  assert.deepEqual(body.previous_context.players,context.players);
  await agent.restoreConversation(b);
  assert.equal(input.value,"Question B");
  assert.equal(agent.buildAskBody("follow-up").previous_context,undefined);
});

test("late stream results stay in their originating tab and mismatched IDs are rejected", async () => {
  const elements = {"[data-agent-empty]":new FakeElement(),"[data-agent-answer]":new FakeElement(),"[data-agent-status]":new FakeElement()};
  const agent = await loadAgentModule({elements});
  agent.startNewChat();
  const a = agent.getNavigation().activeConversationId;
  const request = {conversationId:a,question:"Question A",answerText:"",body:{provider:"openai",model:"fixture"}};
  agent.runtimeFor(a).inFlight = true;
  agent.startNewChat();
  const b = agent.getNavigation().activeConversationId;
  agent.handleStreamEvent("meta",{conversation_id:a,request_id:"request-a"},request);
  agent.handleStreamEvent("answer_delta",{delta:"Answer A"},request);
  assert.equal(elements["[data-agent-answer]"].innerHTML,"");
  assert.throws(()=>agent.handleStreamEvent("final",{payload:{conversation_id:b,request_id:"request-a"}},request),/different conversation/);
  assert.throws(()=>agent.handleStreamEvent("final",{payload:{conversation_id:a,request_id:"request-b"}},request),/different request/);
  agent.handleStreamEvent("final",{payload:{conversation_id:a,request_id:"request-a",status:"ok",answer:"Answer A"}},request);
  assert.equal(agent.getNavigation().activeConversationId,b);
  assert.equal(agent.buildAskBody("followup").previous_context,undefined);
  const saved = agent.loadHistoryState().conversations.find(c=>c.id===a);
  assert.equal(saved.turns[0].question,"Question A");
  assert.equal(saved.turns[0].payload.answer,"Answer A");
  assert.equal(agent.loadHistoryState().conversations.find(c=>c.id===b),undefined);
});

test("two tab requests finish out of order without changing each other's state", async () => {
  const elements = {"[data-agent-empty]":new FakeElement(),"[data-agent-answer]":new FakeElement(),"[data-agent-status]":new FakeElement()};
  const agent = await loadAgentModule({elements});
  const pending = [];
  globalThis.fetch = async (url, options) => new Promise(resolve=>pending.push({body:JSON.parse(options.body),resolve}));
  const complete = (index, answer) => {
    const {body,resolve}=pending[index];
    resolve({ok:true,body:new ReadableStream({start(controller){
      controller.enqueue(new TextEncoder().encode(`event: meta\ndata: ${JSON.stringify({conversation_id:body.conversation_id,request_id:`r${index}`})}\n\nevent: final\ndata: ${JSON.stringify({payload:{conversation_id:body.conversation_id,request_id:`r${index}`,status:"ok",answer}})}\n\n`));
      controller.close();
    }})});
  };
  agent.startNewChat();
  const a = agent.getNavigation().activeConversationId;
  const first = agent.askQuestion("Question A");
  agent.startNewChat();
  const b = agent.getNavigation().activeConversationId;
  const second = agent.askQuestion("Question B");
  assert.equal(pending[0].body.conversation_id,a);
  assert.equal(pending[1].body.conversation_id,b);
  complete(1,"Answer B"); await second;
  assert.equal(agent.runtimeFor(a).inFlight,true);
  assert.equal(agent.runtimeFor(b).inFlight,false);
  complete(0,"Answer A"); await first;
  assert.equal(agent.getNavigation().activeConversationId,b);
  for (const [id,answer] of [[a,"Answer A"],[b,"Answer B"]]) {
    assert.equal(agent.loadHistoryState().conversations.find(c=>c.id===id).turns[0].payload.answer,answer);
  }
});

test("legacy availability answers recover the pair after a clarification", async () => {
  const agent = await loadAgentModule();
  agent.startNewChat();
  const id = agent.getNavigation().activeConversationId;
  const scope = {player_id:811,teammate_id:822,season:"2025-26",phase:"Both",start:null,end:null,metrics:["pts"],aggregation:"average"};
  agent.persistHistoryTurn("Avery Finch without Blake Reed",{conversation_id:id,availability_scope:scope,availability_evidence:{snapshot_id:"fixture"},player_profile:{player:{player_id:811,player_name:"Avery Finch"}}});
  agent.persistHistoryTurn("each player",{conversation_id:id,status:"clarification_required",answer:"Which players?"});
  const context = agent.buildAskBody("How many games did each player play?").previous_context;
  assert.equal(context.availability_scope.teammate_id,822);
  assert.deepEqual(context.scope.phases,["Both"]);
  assert.equal(context.question,"Avery Finch without Blake Reed");
});

test("empty tabs keep distinct IDs and drafts across page reload", async () => {
  const storage = createStorage();
  const input = new FakeElement("textarea");
  const elements={"[data-agent-question]":input,"[data-agent-empty]":new FakeElement(),"[data-agent-answer]":new FakeElement()};
  let agent=await loadAgentModule({storage,elements});
  agent.startNewChat();
  const a=agent.getNavigation().activeConversationId;
  input.value="Draft A";
  agent.startNewChat();
  const b=agent.getNavigation().activeConversationId;
  input.value="Draft B";
  await agent.restoreConversation(a);
  agent=await loadAgentModule({storage,elements});
  agent.initHistory();
  assert.equal(agent.getNavigation().activeConversationId,a);
  assert.equal(input.value,"Draft A");
  await agent.restoreConversation(b);
  assert.equal(input.value,"Draft B");
  assert.equal(agent.buildAskBody("follow-up").previous_context,undefined);
  agent.persistHistoryTurn("New analytical question",{conversation_id:b,status:"ok",answer:"Result"});
  assert.equal(agent.loadHistoryState().conversations.find(c=>c.id===b).title,"New analytical question");
});

test("game inclusion audit is expandable and escapes source details", async () => {
  const agent = await loadAgentModule();
  const html = agent.renderTable({ title: 'Game inclusion details', collapsible: true,
    columns: [{ label: 'Decision' }], rows: [['<limited>']], description: 'Every game counted once.' });
  assert.match(html, /<details><summary>Game inclusion details<\/summary>/);
  assert.match(html, /&lt;limited&gt;/);
  assert.match(html, /Every game counted once/);
  assert.match(html, /<\/details>/);
});

test("history return restores an answer completed while hidden and clears busy controls", async () => {
  const submit = new FakeElement('button');
  const elements = {'[data-agent-empty]':new FakeElement(),'[data-agent-answer]':new FakeElement(),'[data-agent-status]':new FakeElement(),'[data-agent-submit]':submit};
  const agent = await loadAgentModule({elements});
  agent.startNewChat();
  let finish;
  globalThis.fetch = async (url, options) => {
    if (!options?.body) return {ok:true,json:async()=>({conversations:[]})};
    const body=JSON.parse(options.body);
    return new Promise(resolve=>{finish=()=>resolve({ok:true,body:new ReadableStream({start(controller){
      controller.enqueue(new TextEncoder().encode(`event: final\ndata: ${JSON.stringify({payload:{conversation_id:body.conversation_id,status:'ok',answer:'Completed while hidden'}})}\n\n`));
      controller.close();
    }})});});
  };
  const task=agent.askQuestion('An analysis');
  agent.showHistory(true);
  finish(); await task;
  assert.equal(submit.disabled,true);
  await agent.showHistory(false);
  assert.equal(submit.disabled,false);
  assert.equal(submit.textContent,'Ask');
  assert.match(elements['[data-agent-answer]'].children[0].querySelector('.agent-turn-answer').innerHTML,/Completed while hidden/);
});

test("blank tabs cannot evict answered chats from the capped history", async () => {
  const storage=createStorage();
  const elements={'[data-agent-empty]':new FakeElement(),'[data-agent-answer]':new FakeElement()};
  const agent=await loadAgentModule({storage,elements});
  agent.startNewChat();
  const id=agent.getNavigation().activeConversationId;
  agent.persistHistoryTurn('Keep this answer',{conversation_id:id,status:'ok',answer:'Evidence'});
  for(let i=0;i<35;i++) agent.startNewChat();
  const saved=agent.loadHistoryState().conversations;
  assert.equal(saved.length,1);
  assert.equal(saved[0].id,id);
  const reloaded=await loadAgentModule({storage,elements});
  reloaded.initHistory();
  assert.equal(reloaded.loadHistoryState().conversations[0].turns[0].payload.answer,'Evidence');
});

test("split follow-ups retain canonical scope in browser history", async () => {
  const agent = await loadAgentModule();
  agent.startNewChat();
  const id = agent.getNavigation().activeConversationId;
  const split = "Compare Avery Finch points home vs away 2024-25 Regular Season from 2024-09-01 through 2025-08-31 average";
  agent.persistHistoryTurn("Compare Avery Finch home vs away", {
    conversation_id: id, status: "ok",
    conversation_context: { analysis_type: "player_split", question: "Compare Avery Finch home vs away", split_question: split,
      players: [{player_id: 811, player_name: "Avery Finch"}], metrics: ["pts"], scope: {} },
  });
  const context = agent.buildAskBody("What about assists?").previous_context;
  assert.equal(context.analysis_type, "player_split");
  assert.equal(context.split_question, split);
});

test("award answers keep the winner statement and safe source link visible", async () => {
  const agent = await loadAgentModule();
  const target = new FakeElement();
  agent.renderAnswerPayload({award_evidence: {season: "2025-26"},
    answer: "Example Player won the award. [NBA award record](https://www.nba.com/news/history-rookie-of-the-year-winners).\n\nPoints: 20 per game.",
    semantic_evidence: {kind: "performance_overview"}}, target);
  assert.match(target.innerHTML, /Example Player won the award/);
  assert.match(target.innerHTML, /href="https:\/\/www.nba.com\/news\/history-rookie-of-the-year-winners"/);
  assert.match(target.innerHTML, /Points: 20 per game/);
  assert.doesNotMatch(agent.renderAnswerMarkdown("[bad](javascript:alert(1))"), /<a /);
  assert.doesNotMatch(agent.renderAnswerMarkdown('[bad](https://example.test/\"onclick=\"alert(1))'), /href="[^"]*"onclick=/);
});

test("award recovery sends a bounded reference without trusting saved source facts", async () => {
  const agent = await loadAgentModule();
  agent.startNewChat();
  const id = agent.getNavigation().activeConversationId;
  agent.persistHistoryTurn("Who won Finals MVP?", {
    conversation_id: id, status: "ok",
    conversation_context: {
      players: [{player_id: 811, player_name: "Avery Finch"}],
      scope: {season: "2024-25", phases: ["Finals"]},
      award_evidence: {award_key: "finals_mvp", season: "2024-25", performance_phase: "Finals", source_url: "https://example.com/untrusted", player_id: 999},
    },
  });
  const context = agent.buildAskBody("Show his performance").previous_context;
  assert.deepEqual(context.award_reference, {award_key: "finals_mvp", season: "2024-25", performance_phase: "Finals"});
  assert.equal(context.award_evidence, undefined);
  assert.equal(context.award_reference.source_url, undefined);
});
