// M8 model status button: Connected only after the backend confirmed the pinned model and
// authenticated gateway access; every other state says what is wrong.
import assert from "node:assert/strict";
import { test } from "node:test";
import type { ModelStatus } from "../src/api.js";
import { statusButton } from "../src/modelStatusView.js";

const PINNED = { model: "Qwen/Qwen3-8B-AWQ", revision: "4da05a8edb55c6046cce958586c33b61da07bb79" };

function status(over: Partial<ModelStatus>): ModelStatus {
  return { mode: "gateway", verified: false, authenticated: false, chat: null, engine: null, error: null, label: null, checked_at: 0, ...over };
}

test("connected only with verified pinned model and authenticated gateway access", () => {
  const ok = statusButton({ kind: "status", status: status({ verified: true, authenticated: true, chat: PINNED }) });
  assert.equal(ok.label, "Connected · Qwen3-8B-AWQ");
  assert.equal(ok.tone, "ok");
  assert.match(ok.title, /4da05a8edb55c6046cce958586c33b61da07bb79/);
  // Served revision verified but the token was not accepted: not connected.
  const noAuth = statusButton({ kind: "status", status: status({ verified: true, authenticated: false, chat: PINNED }) });
  assert.equal(noAuth.label, "Model offline");
});

test("offline, fake, checking, backend and session states", () => {
  const off = statusButton({ kind: "status", status: status({ error: "unavailable" }) });
  assert.equal(off.label, "Model offline");
  assert.match(off.title, /could not be reached/);
  assert.match(statusButton({ kind: "status", status: status({ error: "auth_failed" }) }).title, /rejected the server's token/);
  assert.equal(statusButton({ kind: "status", status: status({ mode: "fake" }) }).label, "FAKE model");
  const qwenDown = statusButton({ kind: "status", status: status({ error: "chat_server_down" }) });
  assert.equal(qwenDown.label, "Model offline");
  assert.match(qwenDown.title, /Qwen chat server is not running/);
  assert.equal(statusButton({ kind: "checking" }).label, "Checking…");
  assert.equal(statusButton({ kind: "backend_offline" }).label, "Backend offline");
  assert.equal(statusButton({ kind: "session_ended" }).label, "Session ended");
});
