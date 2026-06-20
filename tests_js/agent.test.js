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
      return this.children.find((child) => child.className === "agent-turn-answer") || null;
    }
    return null;
  }

  querySelectorAll(selector) {
    if (selector !== "[data-history-conversation-id]") return [];
    return [...this._innerHTML.matchAll(/data-history-conversation-id="([^"]+)"/g)].map(
      (match) => {
        const button = new FakeElement("button");
        button.dataset.historyConversationId = match[1];
        return button;
      }
    );
  }

  addEventListener(eventName, callback) {
    const listeners = this.listeners.get(eventName) || [];
    listeners.push(callback);
    this.listeners.set(eventName, listeners);
  }

  dispatch(eventName, event = {}) {
    (this.listeners.get(eventName) || []).forEach((callback) => callback(event));
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

async function loadAgentModule({ storage = createStorage(), elements = {} } = {}) {
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

test("renderAnswerMarkdown repairs inline headings and keeps Markdown structure", async () => {
  const agent = await loadAgentModule();

  const html = agent.renderAnswerMarkdown(
    "Intro ## Title ### Context - **PTS:** 30\n\n---\n1. `AST`: 7"
  );

  assert.match(html, /<h3>Title<\/h3>/);
  assert.match(html, /<h4>Context<\/h4>/);
  assert.match(html, /<ul><li><strong>PTS:<\/strong> 30<\/li><\/ul>/);
  assert.match(html, /<hr \/>/);
  assert.match(html, /<ol><li><code>AST<\/code>: 7<\/li><\/ol>/);
});

test("renderAnswerMarkdown strips Markdown pipe tables from Answer prose", async () => {
  const agent = await loadAgentModule();

  const html = agent.renderAnswerMarkdown(
    "Intro\n\n| Metric | Value |\n|---|---|\n| PTS | 30 |\n\n### Takeaway\nGood."
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
  let updated = saved.conversations.find((item) => item.conversation_id === "c-a");
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

test("restoring a conversation repaints latest and older turn side panels", async () => {
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
              answer: "Old answer",
              tables: [{ title: "Old Table", columns: [], rows: [] }],
            },
          },
          {
            request_id: "r-new",
            question: "New question",
            timestamp: "2026-06-19T00:01:00Z",
            payload: {
              answer: "New answer",
              tables: [{ title: "New Table", columns: [], rows: [] }],
            },
          },
        ],
      },
    ],
  });

  agent.restoreConversation("c-restore");

  assert.equal(elements["[data-agent-empty]"].hidden, true);
  assert.equal(elements["[data-agent-answer]"].hidden, false);
  assert.equal(elements["[data-agent-answer]"].children.length, 2);
  assert.equal(elements["[data-agent-status]"].textContent, "Restored");
  assert.match(elements["[data-agent-tables]"].innerHTML, /New Table/);

  elements["[data-agent-answer]"].children[0].dispatch("click");
  assert.match(elements["[data-agent-tables]"].innerHTML, /Old Table/);
});
