// M8: a chat turn never chooses a source (the server routes it) and is saved to the
// employee's conversation, whose server id comes back in X-Conversation-ID.

import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import type { ChatStart } from "../src/reports.js";
import { executeChatTurn } from "../src/runStream.js";

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
});

function sse(...frames: string[]): ReadableStream<Uint8Array> {
  const bytes = new TextEncoder().encode(frames.join(""));
  return new ReadableStream({
    start(c) {
      c.enqueue(bytes);
      c.close();
    },
  });
}

test("the chat sends mode auto plus the saved conversation, and reports the server's ids", async () => {
  const bodies: Record<string, unknown>[] = [];
  globalThis.fetch = (async (_url: RequestInfo | URL, init?: RequestInit) => {
    bodies.push(JSON.parse(String(init?.body)) as Record<string, unknown>);
    return new Response(
      sse(
        'id: 1\nevent: status\ndata: {"step":"accepted","state":"completed","label":"Accepted","handler":"general","handler_label":"General"}\n\n',
        'id: 2\nevent: done\ndata: {"outcome":"published","message_id":null,"verification":null,"abstention":null,"inference":"fake"}\n\n',
      ),
      { status: 200, headers: { "X-Run-ID": "run-1", "X-Conversation-ID": "conv-9", "X-Handler": "general" } },
    );
  }) as typeof fetch;

  const starts: ChatStart[] = [];
  let label: string | null = null;
  const failure = await executeChatTurn(
    "tok",
    "hello",
    "conv-9",
    new AbortController().signal,
    (v) => {
      label = v.handlerLabel;
    },
    () => {},
    (s) => starts.push(s),
  );
  assert.equal(failure, null);
  assert.deepEqual(bodies[0] && { mode: bodies[0].mode, conversation_id: bodies[0].conversation_id }, {
    mode: "auto",
    conversation_id: "conv-9",
  });
  assert.deepEqual(starts, [{ runId: "run-1", conversationId: "conv-9", handler: "general" }]);
  assert.equal(label, "General");
});

test("a new chat sends no conversation id; a foreign one surfaces the server's 404", async () => {
  const bodies: Record<string, unknown>[] = [];
  globalThis.fetch = (async (_url: RequestInfo | URL, init?: RequestInit) => {
    bodies.push(JSON.parse(String(init?.body)) as Record<string, unknown>);
    return new Response(JSON.stringify({ error: { code: "not_found", message: "Not found.", request_id: null } }), {
      status: 404,
      headers: { "Content-Type": "application/json" },
    });
  }) as typeof fetch;
  const starts: ChatStart[] = [];
  const failure = await executeChatTurn("tok", "hello", null, new AbortController().signal, () => {}, () => {}, (s) =>
    starts.push(s),
  );
  assert.equal("conversation_id" in (bodies[0] ?? {}), false);
  assert.equal(failure, "Not found.");
  assert.deepEqual(starts, []);
});
