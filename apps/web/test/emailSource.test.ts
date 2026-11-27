// Regression (PR #21 review): the report a user explicitly picks for email stays the
// draft source even when another run finishes afterwards. Runs the real chat-turn and
// draft clients (executeChatTurn, actions.create) against a mocked fetch: report B's
// stream is held open, report A finishes and is selected, then B completes.
import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import { actions } from "../src/actions.js";
import {
  emailSourceReducer,
  INITIAL_EMAIL_SOURCE,
  newerThanPinned,
  type EmailSourceAction,
  type EmailSourceState,
} from "../src/emailSource.js";
import { executeChatTurn } from "../src/runStream.js";

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
});

function sse(runId: string): string {
  const frames: [string, Record<string, unknown>][] = [
    ["status", { run_id: runId, step: "accepted", label: "Request accepted", state: "completed", inference: "fake" }],
    ["token", { run_id: runId, part: "table", title: "Fulfilled sales, Q1 2026", table: { columns: [], rows: [] }, definition: null }],
    ["done", { run_id: runId, outcome: "published", abstention: null, verification: { label: "figures_verified_narrative_dev_checked" }, inference: "fake" }],
  ];
  return frames.map(([event, data], i) => `id: ${i + 1}\nevent: ${event}\ndata: ${JSON.stringify(data)}\n\n`).join("");
}

/** A chat stream whose body is written only when `release()` is called. */
function heldStream(runId: string): { response: Response; release: () => void } {
  let release = () => {};
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      release = () => {
        controller.enqueue(new TextEncoder().encode(sse(runId)));
        controller.close();
      };
    },
  });
  return { response: new Response(body, { status: 200, headers: { "X-Run-ID": runId } }), release };
}

interface Harness {
  drafts: { source_run_id: string; recipient_user_id: string }[];
  releaseB: () => void;
}

function mockApi(): Harness {
  const b = heldStream("run-B");
  const harness: Harness = { drafts: [], releaseB: b.release };
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const body = init?.body ? (JSON.parse(String(init.body)) as Record<string, string>) : {};
    if (url.endsWith("/api/v1/chat")) {
      if (body.question === "report B") return b.response;
      return new Response(sse("run-A"), { status: 200, headers: { "X-Run-ID": "run-A" } });
    }
    if (url.endsWith("/api/v1/actions/email-drafts") && init?.method === "POST") {
      harness.drafts.push(body as Harness["drafts"][number]);
      return new Response(JSON.stringify({ action_id: "d1", source_run_id: body.source_run_id }), { status: 201 });
    }
    throw new Error(`unexpected request ${init?.method ?? "GET"} ${url}`);
  }) as typeof fetch;
  return harness;
}

function store() {
  let state: EmailSourceState = INITIAL_EMAIL_SOURCE;
  return {
    get: () => state,
    dispatch: (a: EmailSourceAction) => {
      state = emailSourceReducer(state, a);
    },
  };
}

test("a delayed report B finishing after A is selected does not change the draft source", async () => {
  const api = mockApi();
  const s = store();
  const published = (runId: string, title: string) => s.dispatch({ type: "published", runId, title });
  const signal = new AbortController().signal;

  // B is asked first and keeps streaming; A is asked next and finishes.
  const turnB = executeChatTurn("tok", "report B", "report", signal, () => {}, published);
  await executeChatTurn("tok", "report A", "report", signal, () => {}, published);
  assert.equal(s.get().source?.runId, "run-A");

  // The user clicks "Email this report for approval" under A.
  s.dispatch({ type: "select", runId: "run-A", title: "Fulfilled sales, Q1 2026" });

  // B completes while the draft form is open.
  api.releaseB();
  assert.equal(await turnB, null);
  assert.equal(s.get().latest?.runId, "run-B", "B did publish");
  assert.equal(s.get().source?.runId, "run-A", "the pinned source stays A");
  assert.equal(newerThanPinned(s.get())?.runId, "run-B", "the page can offer B as a switch");

  // Draft creation (what ActionsPanel does with its `source`) uses A's run ID.
  const source = s.get().source;
  assert.ok(source);
  await actions.create("tok", source.runId, "user-finance-reader");
  assert.deepEqual(api.drafts, [{ source_run_id: "run-A", recipient_user_id: "user-finance-reader" }]);
});

test("without an explicit choice the newest finished report is the source", async () => {
  const api = mockApi();
  const s = store();
  const published = (runId: string, title: string) => s.dispatch({ type: "published", runId, title });
  const signal = new AbortController().signal;
  const turnB = executeChatTurn("tok", "report B", "report", signal, () => {}, published);
  await executeChatTurn("tok", "report A", "report", signal, () => {}, published);
  api.releaseB();
  await turnB;
  assert.equal(s.get().source?.runId, "run-B");
  assert.equal(s.get().pinned, false);
});

test("picking another report re-pins; sign-out clears", () => {
  let st = emailSourceReducer(INITIAL_EMAIL_SOURCE, { type: "select", runId: "run-A", title: "T" });
  st = emailSourceReducer(st, { type: "published", runId: "run-C", title: "T" });
  assert.equal(st.source?.runId, "run-A");
  st = emailSourceReducer(st, { type: "select", runId: "run-C", title: "T" });
  assert.equal(st.source?.runId, "run-C");
  assert.equal(st.source?.title, "T · report run-C");
  assert.equal(newerThanPinned(st), null);
  assert.deepEqual(emailSourceReducer(st, { type: "clear" }), INITIAL_EMAIL_SOURCE);
});

test("an aborted turn never publishes", async () => {
  mockApi();
  const s = store();
  const controller = new AbortController();
  controller.abort();
  await executeChatTurn("tok", "report A", "report", controller.signal, () => {}, (runId, title) =>
    s.dispatch({ type: "published", runId, title }),
  );
  assert.equal(s.get().source, null);
});
