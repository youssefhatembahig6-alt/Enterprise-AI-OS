/**
 * The chat header's model status button (M8): what to show for the latest check of
 * GET /api/v1/model/status. "Connected" only when the backend confirmed gateway mode,
 * the pinned model and revision, and that the gateway accepted the server's token.
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
  protocol_error: "The model gateway sent an unexpected response.",
};

export function shortModelName(name: string | null | undefined): string {
  return (name ?? "model").split("/").pop() ?? "model";
}

export function statusButton(outcome: CheckOutcome): StatusButton {
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
        return {
          label: "FAKE model",
          tone: "warn",
          title: "The server uses the labelled development double, not a model. Click to check again.",
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
        label: "Model offline",
        tone: "crit",
        title: `${REASONS[s.error ?? ""] ?? "The model gateway is not available."} Click to check again.`,
      };
    }
  }
}
