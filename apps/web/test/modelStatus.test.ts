// M8 model status button: Connected only after the backend confirmed the pinned model and
// authenticated gateway access; every other state says what is wrong.
import assert from "node:assert/strict";
import { test } from "node:test";
import type { ModelStatus } from "../src/api.js";
import { canSend, statusButton } from "../src/modelStatusView.js";

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
  assert.equal(noAuth.label, "Real model offline");
});

test("offline, fake, checking, backend and session states", () => {
  const off = statusButton({ kind: "status", status: status({ error: "unavailable" }) });
  assert.equal(off.label, "Real model offline");
  assert.match(off.title, /could not be reached/);
  assert.match(statusButton({ kind: "status", status: status({ error: "auth_failed" }) }).title, /rejected the server's token/);
  // The employee-facing chat is real-model only: the fake double reads as offline.
  assert.equal(statusButton({ kind: "status", status: status({ mode: "fake" }) }).label, "Real model offline");
  assert.equal(statusButton({ kind: "status", status: status({ mode: "fake" }) }, true).label, "FAKE model (test build)");
  const qwenDown = statusButton({ kind: "status", status: status({ error: "chat_server_down" }) });
  assert.equal(qwenDown.label, "Real model offline");
  assert.match(qwenDown.title, /Qwen chat server is not running/);
  assert.equal(statusButton({ kind: "checking" }).label, "Checking…");
  assert.equal(statusButton({ kind: "backend_offline" }).label, "Backend offline");
  assert.equal(statusButton({ kind: "session_ended" }).label, "Session ended");
});

test("sending is allowed only while the backend verified the pinned real model", () => {
  const connected = { kind: "status", status: status({ verified: true, authenticated: true, chat: PINNED }) } as const;
  assert.equal(canSend(connected), true);
  for (const blocked of [
    { kind: "status", status: status({ error: "unavailable" }) },
    { kind: "status", status: status({ error: "not_configured" }) },
    { kind: "status", status: status({ error: "chat_server_down" }) },
    { kind: "status", status: status({ verified: true, authenticated: false, chat: PINNED }) },
    { kind: "status", status: status({ mode: "fake" }) },
    { kind: "checking" },
    { kind: "backend_offline" },
    { kind: "session_ended" },
  ] as const) {
    assert.equal(canSend(blocked), false, JSON.stringify(blocked));
  }
  // Only an automated-test build may use the labelled fake double.
  assert.equal(canSend({ kind: "status", status: status({ mode: "fake" }) }, true), true);
  assert.equal(canSend({ kind: "status", status: status({ error: "unavailable" }) }, true), false);
});
