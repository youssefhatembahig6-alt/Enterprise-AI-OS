/**
 * Runs one question against POST /api/v1/chat and keeps folding its events into a
 * RunView, replaying from GET /api/v1/runs/{id}/events after a dropped stream.
 * Returns a user-facing failure message, or null when the stream ended normally.
 */

import { ApiError } from "./api";
import { resumeReport, startReport, type AskMode, type ReportEvent } from "./reports";
import { applyEvent, EMPTY_RUN, newRequestId, type RunView } from "./runView";

const MAX_RECONNECTS = 5;

export async function executeRun(
  token: string,
  question: string,
  mode: AskMode,
  signal: AbortSignal,
  onView: (view: RunView) => void,
): Promise<string | null> {
  let view: RunView = EMPTY_RUN;
  const onEvent = (event: ReportEvent) => {
    view = applyEvent(view, event);
    onView(view);
  };
  try {
    const { runId, ended } = await startReport(token, question, newRequestId(), onEvent, signal, mode);
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
