// Chat page state: run events fold into what a turn shows, conversations stay in
// memory per session, and each turn is an independent run.
import assert from "node:assert/strict";
import { test } from "node:test";
import { activeConversation, chatReducer, INITIAL_CHAT, titleFor, type ChatState } from "../src/chatState.js";
import type { ReportEvent } from "../src/reports.js";
import { applyEvent, citationKind, EMPTY_RUN, runKind, type RunView } from "../src/runView.js";

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
  s = chatReducer(s, { type: "ask", conversationId: "c1", turnId: "t1", question: "First question?", mode: "documents", view: EMPTY_RUN });
  assert.equal(s.activeId, "c1");
  assert.equal(activeConversation(s)?.title, "First question?");
  s = chatReducer(s, { type: "ask", conversationId: "c1", turnId: "t2", question: "Second?", mode: "auto", view: EMPTY_RUN });
  assert.equal(activeConversation(s)?.turns.length, 2);
  s = chatReducer(s, { type: "new" });
  assert.equal(activeConversation(s), null);
  s = chatReducer(s, { type: "ask", conversationId: "c2", turnId: "t3", question: "Other", mode: "report", view: EMPTY_RUN });
  assert.deepEqual(s.conversations.map((c) => c.id), ["c2", "c1"]); // newest first
  s = chatReducer(s, { type: "select", id: "c1" });
  assert.equal(activeConversation(s)?.turns[1]?.question, "Second?");
  s = chatReducer(s, { type: "select", id: "missing" });
  assert.equal(s.activeId, "c1");
});

test("turn updates go to the right turn even when another conversation is active", () => {
  let s: ChatState = INITIAL_CHAT;
  s = chatReducer(s, { type: "ask", conversationId: "c1", turnId: "t1", question: "Slow", mode: "auto", view: EMPTY_RUN });
  s = chatReducer(s, { type: "new" });
  s = chatReducer(s, { type: "ask", conversationId: "c2", turnId: "t2", question: "Fast", mode: "auto", view: EMPTY_RUN });
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
