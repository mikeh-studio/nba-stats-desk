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
    querySelectorAll() {
      return [];
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
