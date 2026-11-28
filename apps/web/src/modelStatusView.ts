/**
 * The chat header's model status button (M8): what to show for the latest check of
 * GET /api/v1/model/status. "Connected" only when the backend confirmed gateway mode,
 * the pinned model and revision, and an authenticated readiness check of the actual Qwen
 * chat server behind the gateway (token accepted, vLLM up and serving the model).
 *
 * The employee-facing chat is real-model only: every other state, including a server that
 * runs the fake development double, reads "Real model offline", and `canSend` lets the
 * chat send a new question only while the real model is connected. Automated UI tests may
 * build with `allowFake` (VITE_TEST_ALLOW_FAKE_MODEL=true) to use the labelled fake model
 * as a test double; normal builds never do.
 * Type-only imports, so it runs under node:test.
 */

import type { ModelStatus } from "./api";

export type CheckOutcome =
  | { kind: "checking" }
  | { kind: "status"; status: ModelStatus }
  | { kind: "backend_offline" }
  | { kind: "session_ended" };

export interface StatusButton {
  label: string;
  tone: "ok" | "warn" | "crit" | "unknown";
  title: string;
}

const REASONS: Record<string, string> = {
  not_configured: "No model gateway is configured on the server.",
  unavailable: "The model gateway could not be reached (is the Colab runtime and its connection up?).",
  timeout: "The model gateway did not answer in time.",
  auth_failed: "The model gateway rejected the server's token (did Colab restart with a new token?).",
  revision_mismatch: "The gateway is not serving the pinned model and revision.",
  chat_server_down: "The gateway answers, but its Qwen chat server is not running or not ready.",
  protocol_error: "The model gateway sent an unexpected response.",
};

export function shortModelName(name: string | null | undefined): string {
  return (name ?? "model").split("/").pop() ?? "model";
}

/** Whether the chat may send a new question: the backend verified the pinned real model
 * with an authenticated readiness check (or, in a test build only, the fake double). */
export function canSend(outcome: CheckOutcome, allowFake = false): boolean {
  if (outcome.kind !== "status") return false;
  const s = outcome.status;
  if (s.mode === "fake") return allowFake;
  return Boolean(s.verified && s.authenticated && s.chat?.model && s.chat.revision);
}

export function statusButton(outcome: CheckOutcome, allowFake = false): StatusButton {
  switch (outcome.kind) {
    case "checking":
      return { label: "Checking…", tone: "unknown", title: "Checking the model connection…" };
    case "backend_offline":
      return {
        label: "Backend offline",
        tone: "crit",
        title: "The API is not responding. Click to check again.",
      };
    case "session_ended":
      return {
        label: "Session ended",
        tone: "warn",
        title: "Your session ended. Sign in again to check the model.",
      };
    case "status": {
      const s = outcome.status;
      if (s.mode === "fake") {
        return allowFake
          ? {
              label: "FAKE model (test build)",
              tone: "warn",
              title: "Automated-test build: the server uses the labelled development double. Click to check again.",
            }
          : {
              label: "Real model offline",
              tone: "crit",
              title: "The server is not connected to the real model, so new questions are paused. Your saved chats stay available. Click to check again.",
            };
      }
      if (s.verified && s.authenticated && s.chat?.model && s.chat.revision) {
        return {
          label: `Connected · ${shortModelName(s.chat.model)}`,
          tone: "ok",
          title: `${s.chat.model}@${s.chat.revision} — pinned revision verified, gateway accepted the server's token. Click to check again.`,
        };
      }
      return {
        label: "Real model offline",
        tone: "crit",
        title: `${REASONS[s.error ?? ""] ?? "The model gateway is not available."} New questions are paused; your saved chats stay available. Click to check again.`,
      };
    }
  }
}
