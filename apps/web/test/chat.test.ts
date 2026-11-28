// Chat page state: run events fold into what a turn shows, conversations stay in
// memory per session, and each turn is an independent run.
import assert from "node:assert/strict";
import { test } from "node:test";
import { activeConversation, canRevalidate, chatReducer, INITIAL_CHAT, titleFor, type ChatState } from "../src/chatState.js";
import type { ReportEvent } from "../src/reports.js";
import { applyEvent, citationKind, clarifyView, EMPTY_RUN, runKind, type RunView } from "../src/runView.js";

function must<T>(value: T | null | undefined): T {
  assert.ok(value !== null && value !== undefined);
  return value;
}

function fold(events: ReportEvent[]): RunView {
  return events.reduce(applyEvent, EMPTY_RUN);
}

const status = (id: number, step: string): ReportEvent => ({
  id,
  event: "status",
  data: { run_id: "r1", step, label: step, state: "completed", inference: "fake", inference_label: "FAKE INFERENCE" },
});

test("a refusal is a refusal, not an error or an empty answer", () => {
  const view = fold([
    status(1, "accepted"),
    status(2, "retrieval"),
    {
      id: 3,
      event: "done",
      data: {
        outcome: "abstained",
        abstention: { code: "no_sources", message: "I couldn't find information in documents you can access that answers this." },
        verification: null,
        message_id: null,
        inference: "fake",
      },
    },
  ]);
  assert.equal(runKind(view, null), "refusal");
  assert.equal(view.runId, "r1");
  assert.equal(view.fakeLabel, "FAKE INFERENCE");
  assert.equal(view.lastId, 3);
});

test("a published document answer keeps sentences and citations in order", () => {
  const view = fold([
    status(1, "accepted"),
    { id: 2, event: "token", data: { part: "text", text: "Collections lagged.", citations: ["c1"] } },
    { id: 3, event: "citation", data: { citation_id: "c1", document_id: "d1", title: "Q1 commentary", version: 1 } },
    { id: 4, event: "done", data: { outcome: "published", abstention: null, verification: { label: "answer_quotes_verified_dev_checked" } } },
  ]);
  assert.equal(runKind(view, null), "answer");
  assert.deepEqual(view.sentences, [{ text: "Collections lagged.", citations: ["c1"] }]);
  assert.equal(citationKind(must(view.citations[0])), "document");
});

test("a published SQL report is a report and its citations open evidence", () => {
  const view = fold([
    { id: 1, event: "token", data: { part: "table", title: "Fulfilled sales, Q1 2026", table: { columns: [], rows: [] }, definition: null } },
    { id: 2, event: "citation", data: { citation_id: "c2", evidence_id: "e1", row: 0, column: "net_sales", label: "Net sales" } },
    { id: 3, event: "done", data: { outcome: "published", abstention: null, verification: { label: "figures_verified_narrative_dev_checked" } } },
  ]);
  assert.equal(runKind(view, null), "report");
  assert.equal(citationKind(must(view.citations[0])), "evidence");
});

test("stream errors and client failures render as errors; unfinished runs as running", () => {
  assert.equal(runKind(fold([{ id: null, event: "error", data: { code: "pack_disabled", message: "x" } }]), null), "error");
  assert.equal(runKind(EMPTY_RUN, "Could not reach the API."), "error");
  assert.equal(runKind(fold([status(1, "accepted")]), null), "running");
  // a repeated step replaces the earlier state instead of duplicating it
  assert.equal(fold([status(1, "sql"), status(2, "sql")]).steps.length, 1);
});

test("conversations: ask creates one, new chat returns to the empty screen, select switches", () => {
  let s: ChatState = INITIAL_CHAT;
  s = chatReducer(s, { type: "ask", conversationId: "c1", turnId: "t1", question: "First question?", view: EMPTY_RUN });
  assert.equal(s.activeId, "c1");
  assert.equal(activeConversation(s)?.title, "First question?");
  s = chatReducer(s, { type: "ask", conversationId: "c1", turnId: "t2", question: "Second?", view: EMPTY_RUN });
  assert.equal(activeConversation(s)?.turns.length, 2);
  s = chatReducer(s, { type: "new" });
  assert.equal(activeConversation(s), null);
  s = chatReducer(s, { type: "ask", conversationId: "c2", turnId: "t3", question: "Other", view: EMPTY_RUN });
  assert.deepEqual(s.conversations.map((c) => c.id), ["c2", "c1"]); // newest first
  s = chatReducer(s, { type: "select", id: "c1" });
  assert.equal(activeConversation(s)?.turns[1]?.question, "Second?");
  s = chatReducer(s, { type: "select", id: "missing" });
  assert.equal(s.activeId, "c1");
});

test("turn updates go to the right turn even when another conversation is active", () => {
  let s: ChatState = INITIAL_CHAT;
  s = chatReducer(s, { type: "ask", conversationId: "c1", turnId: "t1", question: "Slow", view: EMPTY_RUN });
  s = chatReducer(s, { type: "new" });
  s = chatReducer(s, { type: "ask", conversationId: "c2", turnId: "t2", question: "Fast", view: EMPTY_RUN });
  const view = fold([status(1, "accepted")]);
  s = chatReducer(s, { type: "view", conversationId: "c1", turnId: "t1", view });
  s = chatReducer(s, { type: "finish", conversationId: "c1", turnId: "t1", failure: null });
  const c1 = must(s.conversations.find((c) => c.id === "c1"));
  assert.equal(must(c1.turns[0]).view.steps.length, 1);
  assert.equal(must(c1.turns[0]).running, false);
  assert.equal(must(must(s.conversations.find((c) => c.id === "c2")).turns[0]).running, true);
});

test("long questions get a short title", () => {
  const title = titleFor("  What were net (tax-exclusive) fulfilled sales for Q1 2026, in USD?  ");
  assert.ok(title.length <= 48);
  assert.ok(title.endsWith("…"));
});

// --- M8: automatic routing labels and saved chats ---------------------------------------

test("the handler label comes from the run's first event and survives later events", () => {
  const first: ReportEvent = {
    id: 1,
    event: "status",
    data: { step: "accepted", state: "completed", label: "Accepted", handler: "documents", handler_label: "Documents" },
  };
  const view = fold([first, status(2, "retrieval"), status(3, "answer")]);
  assert.equal(view.handler, "documents");
  assert.equal(view.handlerLabel, "Documents");
});

test("a clarifying question is its own kind, not a refusal", () => {
  const live = fold([
    { id: 1, event: "status", data: { step: "routing", state: "completed", label: "Needs detail", handler: "clarify", handler_label: "Clarifying question" } },
    {
      id: 2,
      event: "done",
      data: { outcome: "abstained", message_id: null, verification: null, inference: null, handler: "clarify", abstention: { code: "clarification_needed", message: "Which one?" } },
    },
  ]);
  assert.equal(runKind(live, null), "clarify");
  const saved = clarifyView("Which one?");
  assert.equal(runKind(saved, null), "clarify");
  assert.equal(saved.handlerLabel, "Clarifying question");
  assert.equal(saved.done?.abstention?.message, "Which one?");
  const refused = fold([
    { id: 1, event: "done", data: { outcome: "abstained", message_id: null, verification: null, inference: null, abstention: { code: "no_sources", message: "None." } } },
  ]);
  assert.equal(runKind(refused, null), "refusal");
});

test("saved chats: the server list fills Recents, a new chat is bound to its server id", () => {
  let s: ChatState = INITIAL_CHAT;
  s = chatReducer(s, { type: "ask", conversationId: "local-1", turnId: "t1", question: "hello", view: EMPTY_RUN });
  s = chatReducer(s, {
    type: "saved",
    items: [
      { serverId: "srv-a", title: "Older chat" },
      { serverId: "srv-b", title: "Oldest chat" },
    ],
  });
  // The chat still waiting for its first reply stays on top with its turns.
  assert.deepEqual(s.conversations.map((c) => c.id), ["local-1", "srv-a", "srv-b"]);
  assert.equal(must(s.conversations[1]).loaded, false);
  s = chatReducer(s, { type: "bind", conversationId: "local-1", serverId: "srv-new" });
  assert.equal(must(s.conversations[0]).serverId, "srv-new");
  // A later list without it (read before it was saved) keeps it; one with it merges.
  s = chatReducer(s, { type: "saved", items: [{ serverId: "srv-a", title: "Older chat" }] });
  assert.deepEqual(s.conversations.map((c) => c.id), ["local-1", "srv-a"]);
  s = chatReducer(s, {
    type: "saved",
    items: [
      { serverId: "srv-new", title: "hello" },
      { serverId: "srv-a", title: "Older chat" },
    ],
  });
  assert.deepEqual(s.conversations.map((c) => c.id), ["local-1", "srv-a"]);
  assert.equal(must(s.conversations[0]).turns.length, 1);
});

test("saved chats: opening loads the turns once; asking moves the chat to the top", () => {
  let s: ChatState = chatReducer(INITIAL_CHAT, {
    type: "saved",
    items: [
      { serverId: "srv-a", title: "A" },
      { serverId: "srv-b", title: "B" },
    ],
  });
  s = chatReducer(s, { type: "select", id: "srv-b" });
  s = chatReducer(s, {
    type: "opened",
    conversationId: "srv-b",
    turns: [{ id: "s-1", question: "Old question", view: clarifyView("Which one?"), failure: null, running: false }],
  });
  const b = must(activeConversation(s));
  assert.equal(b.loaded, true);
  assert.equal(must(b.turns[0]).question, "Old question");
  s = chatReducer(s, { type: "ask", conversationId: "srv-b", turnId: "t2", question: "Follow-up", view: EMPTY_RUN });
  assert.deepEqual(s.conversations.map((c) => c.id), ["srv-b", "srv-a"]);
  assert.equal(must(s.conversations[0]).serverId, "srv-b"); // appends to the saved chat
});

test("saved chats: a deleted chat leaves Recents and closes when open", () => {
  let s: ChatState = chatReducer(INITIAL_CHAT, {
    type: "saved",
    items: [
      { serverId: "srv-a", title: "A" },
      { serverId: "srv-b", title: "B" },
    ],
  });
  s = chatReducer(s, { type: "select", id: "srv-a" });
  s = chatReducer(s, { type: "removed", conversationId: "srv-a" });
  assert.equal(s.activeId, null);
  assert.deepEqual(s.conversations.map((c) => c.id), ["srv-b"]);
  s = chatReducer(s, { type: "select", id: "srv-b" });
  s = chatReducer(s, { type: "removed", conversationId: "srv-a" });
  assert.equal(s.activeId, "srv-b");
});

test("reopening a chat hides every saved answer until it is replayed, every time", () => {
  const cited = fold([
    { id: 1, event: "status", data: { step: "accepted", state: "completed", label: "Accepted", run_id: "run-1", handler: "documents", handler_label: "Documents" } },
    { id: 2, event: "token", data: { part: "text", text: "Collections lagged revenue.", citations: ["c1"] } },
    { id: 3, event: "citation", data: { citation_id: "c1", document_id: "d1", title: "Q1 2026 finance commentary", quote: "370 open invoices" } },
    { id: 4, event: "done", data: { outcome: "published", message_id: "m", verification: null, abstention: null, inference: "fake" } },
  ]);
  let s: ChatState = chatReducer(INITIAL_CHAT, { type: "saved", items: [{ serverId: "srv-a", title: "Why?" }] });
  const turns = [
    { id: "s-1", question: "Why did collections lag?", view: cited, failure: null, running: false },
    { id: "s-2", question: "the numbers?", view: clarifyView("Which one?"), failure: null, running: false },
  ];
  s = chatReducer(s, { type: "opened", conversationId: "srv-a", turns });
  s = chatReducer(s, { type: "select", id: "srv-a" });
  for (let reopen = 0; reopen < 2; reopen++) {
    assert.equal(canRevalidate(must(activeConversation(s))), true);
    s = chatReducer(s, { type: "revalidating", conversationId: "srv-a" });
    const [answer, clarify] = must(activeConversation(s)).turns;
    // The cited answer: no text, no citation, no result; only the question and label remain.
    assert.deepEqual(must(answer).view.sentences, []);
    assert.deepEqual(must(answer).view.citations, []);
    assert.equal(must(answer).view.done, null);
    assert.equal(must(answer).view.runId, "run-1");
    assert.equal(must(answer).view.handlerLabel, "Documents");
    assert.equal(must(answer).question, "Why did collections lag?");
    assert.equal(must(answer).reopening, true);
    assert.equal(runKind(must(answer).view, null), "running");
    // While it is being re-checked, the chat cannot be revalidated again concurrently by a
    // live ask, but a second reopen is allowed (it restarts the replay).
    assert.equal(canRevalidate(must(activeConversation(s))), true);
    // The clarifying question (no run, no company content) is unchanged.
    assert.equal(must(clarify).view.done?.abstention?.message, "Which one?");
    // The replay ends with the server's refusal: the old answer does not come back.
    s = chatReducer(s, {
      type: "view",
      conversationId: "srv-a",
      turnId: "s-1",
      view: applyEvent(must(answer).view, { id: null, event: "error", data: { code: "access_revoked", message: "You no longer have access to this report." } }),
    });
    s = chatReducer(s, { type: "finish", conversationId: "srv-a", turnId: "s-1", failure: null });
    const after = must(must(activeConversation(s)).turns[0]);
    assert.equal(runKind(after.view, null), "error");
    assert.deepEqual(after.view.sentences, []);
    assert.deepEqual(after.view.citations, []);
    assert.equal(after.reopening, false);
  }
});

test("a chat with an answer still streaming is not revalidated", () => {
  let s: ChatState = chatReducer(INITIAL_CHAT, { type: "ask", conversationId: "local-1", turnId: "t1", question: "hello", view: EMPTY_RUN });
  s = chatReducer(s, { type: "bind", conversationId: "local-1", serverId: "srv-1" });
  assert.equal(canRevalidate(must(activeConversation(s))), false);
  s = chatReducer(s, { type: "finish", conversationId: "local-1", turnId: "t1", failure: null });
  assert.equal(canRevalidate(must(activeConversation(s))), true);
  const unsaved = chatReducer(INITIAL_CHAT, { type: "ask", conversationId: "local-2", turnId: "t2", question: "x", view: EMPTY_RUN });
  assert.equal(canRevalidate(must(activeConversation(unsaved))), false);
});
