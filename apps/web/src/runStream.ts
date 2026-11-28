/**
 * Runs one question against POST /api/v1/chat and keeps folding its events into a
 * RunView, replaying from GET /api/v1/runs/{id}/events after a dropped stream.
 * Returns a user-facing failure message, or null when the stream ended normally.
 */

import { ApiError } from "./api.js";
import { resumeReport, startReport, type AskMode, type ChatStart, type ReportEvent } from "./reports.js";
import { applyEvent, EMPTY_RUN, newRequestId, type RunView } from "./runView.js";

const MAX_RECONNECTS = 5;

export async function executeRun(
  token: string,
  question: string,
  mode: AskMode,
  signal: AbortSignal,
  onView: (view: RunView) => void,
  conversationId: string | null = null,
  onStart?: (start: ChatStart) => void,
): Promise<string | null> {
  let view: RunView = EMPTY_RUN;
  const onEvent = (event: ReportEvent) => {
    view = applyEvent(view, event);
    onView(view);
  };
  try {
    const { runId, ended } = await startReport(
      token,
      question,
      newRequestId(),
      onEvent,
      signal,
      mode,
      conversationId,
      onStart,
    );
    if (runId && !view.runId) {
      view = { ...view, runId };
      onView(view);
    }
    let state = ended;
    for (let attempt = 1; state === "dropped" && attempt <= MAX_RECONNECTS; attempt++) {
      const id = view.runId;
      if (!id || signal.aborted) break;
      view = { ...view, reconnecting: true };
      onView(view);
      await new Promise((r) => setTimeout(r, Math.min(1000 * attempt, 4000)));
      try {
        state = await resumeReport(token, id, view.lastId, onEvent, signal);
      } catch (err) {
        if (err instanceof ApiError) throw err;
        state = "dropped";
      }
    }
    if (state === "dropped" && !signal.aborted) {
      return "Lost the connection to the server. The run may still finish; try again later.";
    }
    return null;
  } catch (err) {
    if (signal.aborted) return null;
    if (err instanceof ApiError) {
      return err.status === 503
        ? "Reporting is not configured on this server."
        : err.status === 401
          ? "Your session has ended. Sign in again."
          : err.message;
    }
    return "Could not reach the API.";
  }
}

/**
 * One chat turn: the server's orchestrator picks the handler (no source is chosen in the
 * UI); the exchange is saved to ``conversationId`` (or a new conversation, reported through
 * ``onStart``). A published SQL report is reported exactly once, when its stream has ended
 * (not when it was aborted). The Chat page and the regression tests use this function.
 */
export async function executeChatTurn(
  token: string,
  question: string,
  conversationId: string | null,
  signal: AbortSignal,
  onView: (view: RunView) => void,
  onReportPublished: (runId: string, title: string) => void,
  onStart?: (start: ChatStart) => void,
): Promise<string | null> {
  let last: RunView | null = null;
  const failure = await executeRun(
    token,
    question,
    "auto",
    signal,
    (view) => {
      last = view;
      onView(view);
    },
    conversationId,
    onStart,
  );
  const final = last as RunView | null;
  if (!signal.aborted && final?.table && final.done?.outcome === "published" && final.runId) {
    onReportPublished(final.runId, final.title ?? "Verified report");
  }
  return failure;
}

/**
 * Reopen a saved answer: replay its run from the first event. The server re-checks the
 * employee's current access while replaying, so revoked content is not shown again.
 */
export async function replayRun(
  token: string,
  runId: string,
  signal: AbortSignal,
  onView: (view: RunView) => void,
): Promise<string | null> {
  let view: RunView = { ...EMPTY_RUN, runId };
  const onEvent = (event: ReportEvent) => {
    view = applyEvent(view, event);
    onView(view);
  };
  try {
    let state: "done" | "dropped" = "dropped";
    for (let attempt = 0; state === "dropped" && attempt <= MAX_RECONNECTS; attempt++) {
      if (signal.aborted) return null;
      if (attempt > 0) await new Promise((r) => setTimeout(r, Math.min(1000 * attempt, 4000)));
      try {
        state = await resumeReport(token, runId, view.lastId, onEvent, signal);
      } catch (err) {
        if (err instanceof ApiError) throw err;
        state = "dropped";
      }
    }
    return state === "dropped" && !signal.aborted ? "Could not load this answer. Try again later." : null;
  } catch (err) {
    if (signal.aborted) return null;
    if (err instanceof ApiError) {
      return err.status === 404
        ? "This answer is no longer available to you."
        : err.status === 401
          ? "Your session has ended. Sign in again."
          : err.message;
    }
    return "Could not reach the API.";
  }
}
